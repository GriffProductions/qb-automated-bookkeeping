"""LLM extraction: document text/bytes + few-shot history → ExtractedTransaction.

Production path uses Gemini via LangChain ``with_structured_output``.  When no
API key / credentials are configured (local dev, CI), a deterministic
heuristic fallback parses vendor/amount/date from the text so the pipeline
and tests run without cloud access.
"""

from __future__ import annotations

import logging
import re
import datetime as _dt
from datetime import date
from pathlib import Path

from qb_automation.config import settings
from qb_automation.config.company_registry import get_company, match_filename_to_company
from qb_automation.models.document_schema import ExtractedTransaction
from qb_automation.services.convention import doc_phrase_for, verify_conformance
from qb_automation.services.naming import SEP, build_filename, normalize_separators
from qb_automation.services.pdf_indexer import extract_text_from_pdf, relevant_examples

log = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are a bookkeeping assistant that names financial PDFs and extracts
structured metadata.  Follow the user's historical naming convention exactly.
Segments are separated by ' — ' (space + EM DASH U+2014 + space) — NEVER a
plain hyphen:

  YYYY-MM-DD DocPhrase [ref] — [PayMethod] — Vendor — [Unit] — [detail] — [amount] — Tag

Monthly statements use YYYY-MM.  Statements (bank/owner/ledger) omit amount.
DocType is one of: Payment, Invoice, Bill-and-Payment, Mortgage, Tax, Insurance.
- Invoice: a bill received, not yet paid.  - Payment: proof of payment / receipt / confirmation / statements.
- Bill-and-Payment: a single PDF containing both the bill and its payment proof.
- Mortgage: mortgage statements.  - Tax: FTB/IRS/property-tax items.
- Insurance: policies, renewals, certificates.

Rules:
- date is the document/transaction date in YYYY-MM-DD.
- amount is the total (largest transaction total, NOT rates/percentages), positive; refunds/credits negative.
- unit_class captures unit tags like "Unit 200", "Unit 96", or ARC clinic
  codes SCD / NKC / LCD / MHD when present, else null.
- SPELL-OUT RULE: facility abbreviations (HIP, ARC, RIG, SIP, …) appear ONLY
  as the trailing company Tag.  As vendor or associated company, always spell
  out the full name (Healthcare Investment Properties, American Renal Care …).
- header_memo is a one-line human summary; ledger_memo is the QB line memo.
- suggested_filename follows the convention above WITHOUT the .pdf extension,
  uses ' — ' separators, and must not contain <>:"/\\|?*.
- SPECIAL CASES (always apply):
  * ARC outgoing invoices (American Renal Care bills a clinic): head stays
    "DATE Invoice", ref is "Invoice <N> to <Full Clinic Name>", NO vendor
    segment, tag is always ARC.
  * GlobalCare invoices: unit slot is the SPELLED-OUT facility
    ("GlobalCare — Northridge Kidney Center"), tag always ARC.
  * Checks: date is the CHECK date; head is "DATE Check <N> & Bill" (or & Invoice).
  * Statements (HOA/bank/owner) are never "Payment & Bill" — head keeps the
    statement kind ("DATE HOA Statement") even when autopay text appears.
  * Bills/invoices default to the INVOICE/BILL date, never due/payment dates.
    Payment-confirmation date applies only with real payment proof, and then
    the head is "DATE Payment Confirmation and Invoice <N>".
   * Mortgage amount is the OUTSTANDING PRINCIPAL balance, not the amount due.
Return ONLY the structured object.

- GlobalCare PDFs are invoices with backup pages, NEVER payment
  confirmations: DocType Invoice, date and amount from the invoice itself.
- HOA-vendor docs (West Creek, Valencia Fairways, Scenic Hills, …) are
  "HOA Statement" even when the word invoice appears.
- Single-page invoices are Invoice unless a check or explicit payment
  receipt is present; remit-stub wording doesn't count.
- LADWP bills are Invoice with "Electricity & water" detail.
- A check page plus renewal certificate → "DATE Check <N> & Renewal Certificate".

When a vendor/account/class roster is provided below, vendor MUST be one of
the listed vendors (exact spelling — if the document names a vendor not on
the list, use the closest listed match only when unambiguous, else the
document's spelling) and unit_class MUST be one of the listed classes.
"""

_MONEY_RE = re.compile(r"(?<![\d.,])(?P<sign>-?)\$?\s?(?P<num>\d[\d,]*\.\d{2})(?!\d)")
_DATE_YMD_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_TOTAL_HINT_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (r"amount\s+due", r"total\s+(amount\s+)?due", r"balance\s+due",
              r"grand\s+total", r"please\s+pay", r"\btotal\b.{0,10}\$")
)

# Canonical vendor casing — matched case-insensitively, emitted as written here.
KNOWN_VENDORS: dict[str, str] = {
    "burrtec": "Burrtec",
    "state farm": "State Farm",
    "daniel stegall": "Daniel Stegall",
    "mr. cooper": "Mr. Cooper",
    "newrez": "NewRez",
    "pnc bank": "PNC Bank",
    "onity": "Onity Mortgage",
    "culligan": "Culligan of Sylmar",
    "peace realty": "Peace Realty",
    "brad management": "BRAD Management",
    "globalcare": "GlobalCare",
    "briscoe": "Briscoe Economics Group",
    "franchise tax board": "Franchise Tax Board",
    "clark county": "Clark County Treasurer",
    "f&m": "F&M Bank",
    "scenic hills": "Scenic Hills",
    "siena villas": "Siena Villas HOA",
    "buena vida": "Buena Vida",
    "valencia fairways": "Valencia Fairways",
    "ballard rosenberg": "Ballard Rosenberg",
    "at&t": "AT&T",
    "ladwp": "LADWP",
    "dwp": "LADWP",
    "scv water": "SCV Water",
    "republic": "Republic Services",
    "so cal gas": "SoCal Gas",
    "socalgas": "SoCal Gas",
    "southern california edison": "SCE",
    "rayne": "Rayne Water",
    "k&s air": "K&S Air Conditioning",
    "premier trust": "Premier Trust",
    "utopia": "Utopia Management",
    "lvvwd": "Las Vegas Valley Water District",
    "las vegas valley water": "Las Vegas Valley Water District",
    "west creek": "West Creek and West Hills Community Association",
    "southern nevada health": "Southern Nevada Health District",
}

# Vendor whose bills belong to a different company than the text suggests.
# (LADWP bills display Healthcare Investment Properties but book to SIP.)
VENDOR_COMPANY_OVERRIDE: dict[str, str] = {"ladwp": "sip"}

# Vendors whose PDFs are always plain bills even when stub/autopay/backup
# language mimics payment proof.  GlobalCare "confirmations" are invoices
# with backup pages — date/amount come from the invoice itself.
VENDOR_DOC_OVERRIDE: dict[str, str] = {
    "ladwp": "Invoice",
    "globalcare": "Invoice",
}

# Vendor → detail memo used in filenames and QB line memos.
VENDOR_MEMO: dict[str, str] = {"ladwp": "Electricity & water"}

# HOA vendors: their "invoices" are HOA statements — head stays
# "DATE HOA Statement" even when the word invoice appears.
HOA_VENDORS = frozenset({
    "west creek and west hills community association",
    "(hoa) west creek / west hills",
    "valencia fairways",
    "(hoa) valencia fairways",
    "scenic hills",
    "siena villas hoa",
    "buena vida",
    "(hoa) summerlin north",
})

# Strong payment proof: single-page invoices need one of these (not mere
# remit-stub / "payment due" wording) to count as Bill-and-Payment.
_STRONG_PAY_RE = re.compile(
    r"(?i)paid in full|payment confirmation|payment receipt|amount paid|"
    r"thank you for|remittance|for your payment")

# Insurance renewal certificates accompanying a check.
_RENEWAL_CERT_RE = re.compile(r"(?i)renewal\s+certificat|certificat\w*\s+renewal")

# A check IMAGE is present even when its number is illegible (rotated scan,
# sideways photo).  Distinct from _CHECK_RE (number required): these fire on
# check stock language.  The "check your balance" verb sense is excluded by
# requiring image/back/front/number/payee context — never a bare "check".
_CHECK_PRESENCE_RES = (
    re.compile(r"(?i)pay to the order of"),
    re.compile(r"(?i)\bpay to\b\s+[A-Z]"),
    re.compile(r"(?i)\bcheck\b\s*(image|images|back|front|copy|copies)\b"),
    re.compile(r"(?i)(front|back)\s+of\s+(the\s+)?check\b"),
    re.compile(r"(?i)\bcheck\s*(#|no\.?|number)\b"),
)


def _find_spaced_check_no(text: str) -> str | None:
    """Check number when OCR spaces its digits (``2 6 7 2`` on check scans).

    Groups consecutive pure-digit tokens within 80 chars after a
    ``check``/``checks`` mention: ``2 6 7 2`` → ``2672``, while phone
    fragments (``1-800-224-7021``), dates (``11-35/1210``), amounts
    (``1,139.74``), MICR runs (10+ digits) and 19xx/20xx years are skipped
    because their tokens are never pure 3–6 digit runs.  First valid group wins.
    """
    window = text[:3000]
    for m in re.finditer(r"(?i)\bchecks?\b", window):
        tail = window[m.end():m.end() + 80]
        tokens = tail.split()
        i = 0
        while i < len(tokens):
            if re.fullmatch(r"\d+", tokens[i]):
                group = tokens[i]
                j = i + 1
                while j < len(tokens) and re.fullmatch(r"\d+", tokens[j]):
                    group += tokens[j]
                    j += 1
                if 3 <= len(group) <= 6 and not (
                        len(group) == 4 and group[:2] in ("19", "20")):
                    return group
                i = j
            else:
                i += 1
    return None

# ARC clinic codes → spelled-out facility names (unit slot + "to" refs).
CLINIC_FULL_NAMES: dict[str, str] = {
    "LCD": "Laurel Canyon Dialysis",
    "NKC": "Northridge Kidney Center",
    "MHD": "Mission Hills Dialysis",
    "SCD": "Santa Clarita Dialysis",
}
_CLINIC_KEYWORDS: dict[str, str] = {
    "laurel": "Laurel Canyon Dialysis",
    "northridge": "Northridge Kidney Center",
    "north ridge": "Northridge Kidney Center",
    "santa clarita": "Santa Clarita Dialysis",
    "mission hills": "Mission Hills Dialysis",
}

_MORTGAGE_RES = tuple(re.compile(p, re.IGNORECASE) for p in
                      (r"\bmortgage\b", r"mr\.?\s+cooper", r"\bnewrez\b", r"\bpnc\s+bank\b", r"\bonity\b"))
_INSURANCE_RES = tuple(re.compile(p, re.IGNORECASE) for p in
                       (r"\bstate\s+farm\b", r"\brenewal\b", r"\bpolicy\b", r"\btravelers\b"))
# Note: generic "tax due"/"tax payment" deliberately excluded — they fire on
# payment-terms lines ("Tax due - 2") inside non-tax invoices.
_TAX_RES = tuple(re.compile(p, re.IGNORECASE) for p in
                 (r"\bftb\b", r"franchise\s+tax\s+board", r"\birs\b", r"property\s+tax",
                  r"\btax\s+bill\b", r"\btax\s+refund\b", r"\btax\s+year\b",
                  r"form\s+1120", r"\b1099\b", r"form\s+940"))
_BILL_WORDS = re.compile(r"\b(invoice|inv\b|bill)\b", re.IGNORECASE)
_PAY_WORDS = re.compile(r"\b(payment|receipt|confirmation|paid|check\b)\b", re.IGNORECASE)
_REF_RE = re.compile(
    r"(?i)\b(invoice|inv|check|bill|policy)\b\s*"
    r"(?:#|number\b|no\.?\b|n(?=\d))?\s*"
    r"(?=[A-Za-z0-9\-]*\d)([A-Za-z0-9][A-Za-z0-9\-]{1,24})")
_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
           "jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_MON_RE = re.compile(rf"(?i)\b({_MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b")
_MON_TO_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_PAY_METHOD_RE = re.compile(
    r"(?i)\b(visa|bofa|bon|chase|f&m|pnc)\s*[\d*]{3,6}\b")

# Real payment proof (stubs, confirmations, receipts, checks) — NOT mere
# mentions of "payment"/"payments" (autopay enrollment, "payment due").
# Real payment proof (stubs, confirmations, receipts, checks, thank-you emails)
# — NOT mere mentions of "payment"/"payments" (autopay enrollment, "payment due").
_PAY_PROOF_RE = re.compile(
    r"(?i)\b(payment confirmation|payment receipt|payment stub|paid in full|"
    r"amount paid|payment date|payment received|remittance|confirmation|receipt|"
    r"for your payment|thank you for|\bpaid\b|\bcheck\b)\b")
# Stub instructions ("Make Check Payable To", "Check box …") and the verb
# "check" ("to check your balance") are not payment proof.
_PROOF_STRIP_RE = re.compile(
    r"(?i)make\s+checks?\s+payable\s+to[^.\n]*|check\s+box\s+for[^.\n]*|"
    r"to\s+check\s+(your\s+)?(current\s+)?balance|to\s+check\b")


def _has_pay_proof(text: str) -> bool:
    scrubbed = _PROOF_STRIP_RE.sub(" ", text)
    return bool(_PAY_PROOF_RE.search(scrubbed))
_STATEMENT_KIND_RES = (
    ("HOA", re.compile(r"(?i)\bhoa\b|homeowners?\s+association")),
    ("Bank", re.compile(r"(?i)\bbank\s+statement\b")),
    ("Owner", re.compile(r"(?i)\bowner\s+statement\b")),
)
_CHECK_RE = re.compile(r"(?i)\bcheck\s*(?:#|no\.?|number)?\s*(\d{3,6})\b")
# Strict invoice-number token: "Invoice # 564", "Invoice Number 0620-…",
# "Invoice N1160151767".  Bare "INVOICE 23421" (street addresses like
# "INVOICE 23421 Lyons Avenue") must NOT match — hence no bare form.
_INV_NO_RE = re.compile(
    r"(?i)\binvoice\s*(?P<ref>(?:#|number|no\.?)\s*[A-Za-z0-9][A-Za-z0-9\-]*"
    r"|N\d[A-Za-z0-9\-]*)")
_PRINCIPAL_RE = re.compile(r"(?i)(outstanding\s+principal|principal\s+balance)")


def _clean_inv_ref(raw: str) -> str:
    """"Invoice Number 0620-…" → "0620-…"; keeps N-prefix ("N1160151767")."""
    return re.sub(r"^(?:#|number|no\.?)\s*", "", raw, flags=re.IGNORECASE).strip()


def _find_invoice_no(text: str) -> str | None:
    """First invoice token containing a digit ("Number Invoice" headers skipped)."""
    for m in _INV_NO_RE.finditer(text[:2000]):
        cleaned = _clean_inv_ref(m.group("ref"))
        if re.search(r"\d", cleaned):
            return cleaned
    return None
_DATE_LABEL_RE = re.compile(
    r"(?i)(invoice\s+date|bill\s+date|statement\s+date|date\s+of\s+(?:invoice|bill)|"
    r"check\s+date|payment\s+date)\s*:?\s*(\d{1,2}/\d{1,2}/20\d{2})")
_TO_CLINIC_RE = re.compile(
    r"(?i)\bto\s*:\s*([A-Z][A-Za-z .&'\-]{2,50}?)(?:,|\n|LLC|INC|$)")
_ARC_LETTERHEAD_RE = re.compile(r"(?i)american\s+renal\s+care")


def _parse_mdy(s: str) -> date | None:
    m = _DATE_MDY_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _parse_mon(s: str) -> date | None:
    m = _DATE_MON_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MON_TO_NUM[m.group(1)[:3].lower()], int(m.group(2)))
    except ValueError:
        return None


def _mdy_near(
    text: str,
    anchor_rx: "re.Pattern[str]",
    window: int = 60,
    exclude_rx: "re.Pattern[str] | None" = None,
) -> date | None:
    """First MDY whose surrounding window matches the anchor (e.g. check context)."""
    for m in _DATE_MDY_RE.finditer(text):
        lo, hi = max(0, m.start() - window), m.end() + window
        context = text[lo:hi]
        if anchor_rx.search(context) and not (exclude_rx and exclude_rx.search(context)):
            parsed = _parse_mdy(m.group(0))
            if parsed:
                return parsed
    return None


# A "due" context disqualifies a date — but only real due-date phrasing.
# "Due to Manager" (owed-to) and "Amount Due $X" (amount lines) are NOT dates.
_DUE_RX = re.compile(r"(?i)\bdue\s+(date|on|by)\b|past\s+due|payment\s+due")


def _in_due_window(text: str, match: "re.Match[str]", window: int = 50) -> bool:
    lo, hi = max(0, match.start() - window), match.end() + window
    return bool(_DUE_RX.search(text[lo:hi]))


def _find_date(
    source_name: str,
    text: str,
    *,
    is_check: bool = False,
    has_pay_proof: bool = False,
    is_bill: bool = False,
    is_mortgage: bool = False,
) -> date:
    """Date priority: check date → payment date (proof only, never mortgages)
    → labeled invoice/bill/statement date → filename day → first non-due MDY
    (bills) → any MDY → month names → today.  Doc-derived dates beat the
    filename (which may be a prior rename); bills never take due dates."""
    # 1. Check docs run on the check date.
    if is_check:
        got = _mdy_near(text, re.compile(r"(?i)\bcheck\b"))
        if got:
            return got
    # 3. Payment-confirmation date: real payment proof (never a due date,
    # never a mortgage coupon).  Scores MDY + month-name candidates by nearby
    # proof language; a bare "payment due" window disqualifies.
    if has_pay_proof and not is_mortgage:
        pay_rx = re.compile(
            r"(?i)payment\s+date|\bpaid\b|confirmation|receipt|remittance|"
            r"for your payment|thank you for")
        bill_rx = re.compile(r"(?i)\binvoice\b|\bbill\b|\bdue\b")
        cands: list[tuple[int, str]] = []
        for pat in (_DATE_MDY_RE, _DATE_MON_RE):
            for m in pat.finditer(text):
                lo, hi = max(0, m.start() - 80), m.end() + 80
                near40 = text[max(0, m.start() - 40):m.end() + 40]
                if pay_rx.search(text[lo:hi]) and not bill_rx.search(near40):
                    cands.append((m.start(), m.group(0)))
        for _, s in sorted(cands):
            parsed = _parse_mdy(s) or _parse_mon(s)
            if parsed:
                return parsed
    # 4. Explicitly labeled invoice/bill/statement dates.
    m = _DATE_LABEL_RE.search(text[:4000])
    if m:
        parsed = _parse_mdy(m.group(2))
        if parsed:
            # A labeled *due* date is never the document date.
            if "due" not in m.group(1).lower():
                return parsed
    # 5. Month-name dates ("Sep 9, 2026", "September 13, 2026") — doc-derived,
    # so they outrank the filename too.  Due-date windows never qualify.
    # Document-context dates (Prepared/Statement/Dated) beat period starts.
    combined = (source_name or "") + " " + text[:4000]
    doc_rx = re.compile(r"(?i)prepared|statement|dated|issued|effective\s+date|as\s+of")
    for only_doc in (True, False):
        for m in _DATE_MON_RE.finditer(combined):
            if _in_due_window(combined, m, window=40):
                continue
            if only_doc:
                lo = max(0, m.start() - 60)
                if not doc_rx.search(combined[lo:m.start()]):
                    continue
            try:
                return date(int(m.group(3)), _MON_TO_NUM[m.group(1)[:3].lower()], int(m.group(2)))
            except ValueError:
                continue
    # 6. Exact day-precision date the user already put in a filename.
    # (Ranked below doc-derived dates: the filename may be a prior rename.)
    for haystack in (source_name,):
        m = _DATE_YMD_RE.search(haystack or "")
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    # 7. Bills/invoices: first MDY outside a due-date window.
    if is_bill:
        for m in _DATE_MDY_RE.finditer(text[:3000]):
            if not _in_due_window(text, m):
                parsed = _parse_mdy(m.group(0))
                if parsed:
                    return parsed
    else:
        got = _parse_mdy(text[:1500])
        if got:
            return got
    return date.today()


def _nearest_after(
    text: str, anchor_rx: "re.Pattern[str]", window: int = 80
) -> float | None:
    """Money value nearest AFTER the LAST anchor occurrence.

    Proximity (not line) based, so it works on whitespace-collapsed OCR text:
    "Outstanding Principal $184.94", "Amount Due $394.00".  Zero amounts
    ($0.00 coupon fields) never qualify as totals.
    """
    last: float | None = None
    for a in anchor_rx.finditer(text):
        m = _MONEY_RE.search(text, a.end(), min(len(text), a.end() + window))
        if m:
            try:
                val = float(m.group("num").replace(",", ""))
                if m.group("sign") == "-":
                    val = -val
                if val != 0:
                    last = val
            except ValueError:
                continue
    return last


def _max_near_total(text: str) -> float | None:
    """Largest money near any total anchor (detail subtotals lose to grand totals)."""
    best: float | None = None
    for a in re.finditer(r"(?i)\btotal\b(?!\s+(credits|payments))", text):
        seg = text[max(0, a.start() - 20):a.end() + 80]
        for m in _MONEY_RE.finditer(seg):
            try:
                val = float(m.group("num").replace(",", ""))
                if m.group("sign") == "-":
                    val = -val
                if val != 0 and (best is None or abs(val) > abs(best)):
                    best = val
            except ValueError:
                continue
    return best


def _find_amount(text: str, *, is_mortgage: bool = False) -> float:
    """Amount resolution order: mortgage principal → amount-due proximity →
    total/balance proximity → largest value.  Proximity-first so OCR-collapsed
    text (no line breaks) resolves the same as clean text."""
    # OCR spacing artifacts ("2 2,369.73" = one split number): join before
    # any amount math.  The comma requirement keeps dates ("SEP 02 2026")
    # and spaced check digits ("2 6 7 2") untouched.
    text = re.sub(r"(?<=\d) (?=\d,\d)", "", text)
    if is_mortgage:
        got = _nearest_after(text, _PRINCIPAL_RE)
        if got is not None:
            return got
        # Principal unreadable (garbled OCR): the coupon amount-due is the
        # honest fallback — never "total paid year to date".
        got = _nearest_after(text, re.compile(r"(?i)\bamount\s+due\b"))
        if got is not None:
            return got
        pool: list[float] = []
        for m in _MONEY_RE.finditer(text):
            try:
                val = float(m.group("num").replace(",", ""))
                pool.append(-val if m.group("sign") == "-" else val)
            except ValueError:
                continue
        return max(pool, key=abs) if pool else 0.0
    got = _nearest_after(text, re.compile(r"(?i)\bamount\s+due\b"))
    if got is not None:
        return got
    got = _nearest_after(text, re.compile(r"(?i)\bamount\s+due\b"))
    if got is not None:
        return got
    # Refund instruments ("WARRANT AMOUNT $X") outrank summary totals.
    got = _nearest_after(text, re.compile(r"(?i)\bwarrant(\s+amount)?\b"))
    if got is not None:
        return got
    got = _nearest_after(text, re.compile(r"(?i)\brefund\b"))
    if got is not None:
        return got
    # "Total <Charges|New Charges|…>" — largest wins (detail subtotals lose to
    # grand totals); never "total credits/payments" (summary lines).
    got = _max_near_total(text)
    if got is not None:
        return got
    got = _nearest_after(text, re.compile(
        r"(?i)total\s+(amount\s+)?due|balance\s+due|grand\s+total|please\s+pay"))
    if got is not None:
        return got
    pool: list[float] = []
    for m in _MONEY_RE.finditer(text):
        try:
            val = float(m.group("num").replace(",", ""))
            pool.append(-val if m.group("sign") == "-" else val)
        except ValueError:
            continue
    return max(pool, key=abs) if pool else 0.0


def _canon_clinic(name: str) -> str | None:
    lowered = name.lower()
    for keyword, full in _CLINIC_KEYWORDS.items():
        if keyword in lowered:
            return full
    return None


def _arc_outgoing(text: str) -> tuple[str | None, str | None]:
    """ARC letterhead billing a clinic → (invoice_no, clinic_full_name).

    Special naming case: "Invoice 112 to Northridge Kidney Center", tag ARC.
    """
    if not _ARC_LETTERHEAD_RE.search(text[:1500]):
        return None, None
    m = _TO_CLINIC_RE.search(text[:2000])
    clinic = _canon_clinic(m.group(1)) if m else None
    if clinic is None:
        return None, None
    inv = _INV_NO_RE.search(text[:2000])
    return (_clean_inv_ref(inv.group("ref")) if inv else None), clinic


def _arc_facility_full(text: str, source_name: str) -> str | None:
    """Spelled-out facility for GlobalCare-style docs.

    The uploader filename wins ties — matching the code OR the spelled-out
    name (prior renames may have replaced codes with full names).
    """
    if source_name:
        for code, full in CLINIC_FULL_NAMES.items():
            if re.search(rf"(?<!\w){code}(?!\w)", source_name, re.IGNORECASE):
                return full
        via_name = _canon_clinic(source_name)
        if via_name:
            return via_name
    codes = [c for c in CLINIC_FULL_NAMES if re.search(rf"\b{c}\b", text)]
    if len(codes) == 1:
        return CLINIC_FULL_NAMES[codes[0]]
    return None


def _detect_statement_kind(lowered: str) -> str | None:
    for kind, rx in _STATEMENT_KIND_RES:
        if rx.search(lowered):
            return kind
    return None


def _classify_doc_type(
    lowered: str,
    *,
    has_pay_proof: bool = False,
    has_invoice_no: bool = False,
    statement_kind: str | None = None,
) -> str:
    # Invoice number + payment proof outranks Insurance (service "policies").
    if has_invoice_no and has_pay_proof and "mortgage" not in lowered:
        return "Bill-and-Payment"
    if any(rx.search(lowered) for rx in _MORTGAGE_RES):
        return "Mortgage"
    # "Privacy Policy" footers are not insurance policies.
    lowered_nopriv = re.sub(r"(?i)privacy\s+policy", "", lowered)
    if any(rx.search(lowered_nopriv) for rx in _INSURANCE_RES):
        return "Insurance"
    if any(rx.search(lowered) for rx in _TAX_RES):
        return "Tax"
    if _BILL_WORDS.search(lowered) and has_pay_proof:
        return "Bill-and-Payment"
    if _BILL_WORDS.search(lowered):
        return "Invoice"
    return "Payment"


def _find_vendor(lowered: str, text: str) -> str:
    """Vendor detection with the spell-out rule.

    Facility abbreviations (HIP, ARC, …) are only valid as the trailing tag —
    as a vendor they resolve to the full name via FACILITY_LONG_NAMES.
    Matching is tiered: long phrases match even in whitespace-collapsed OCR
    text; short tokens require word boundaries (so "ship" never yields HIP).
    """
    from qb_automation.config.company_registry import FACILITY_LONG_NAMES

    candidates: list[tuple[str, str]] = [
        *FACILITY_LONG_NAMES.items(), *KNOWN_VENDORS.items(),
    ]
    # Earliest mention wins: vendor letterhead precedes the BILL TO block in
    # virtually all invoices, so position beats dictionary order.
    best: tuple[int, str] | None = None
    for key, canonical in candidates:
        tokens = key.split()
        if len(tokens) > 1:
            pat = r"(?<!\w)" + r"\s+".join(re.escape(t) for t in tokens) + r"(?!\w)"
        elif len(key) < 6:
            pat = r"(?<!\w)" + re.escape(key) + r"(?!\w)"
        else:
            pat = re.escape(key)  # long single tokens double as substrings
        m = re.search(pat, lowered)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canonical)
    if best is not None:
        return best[1]
    # Spaceless fallback for long phrases in OCR-collapsed text
    spaceless = re.sub(r"[\s_\-]+", "", lowered)
    for key, canonical in candidates:
        flat = re.sub(r"[\s_\-]+", "", key)
        if len(flat) >= 10 and flat in spaceless:
            return canonical
    return "Unknown Vendor"


def _find_pay_method(text: str) -> str | None:
    m = _PAY_METHOD_RE.search(text)
    return m.group(0).strip() if m else None


def _find_ref(text: str) -> str | None:
    m = _REF_RE.search(text[:2000])
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    return None


def _invoice_amount(text: str) -> float | None:
    """Amount on the invoice itself: first money after an invoice anchor.

    Multi-page docs (GlobalCare + backup) carry summary/table totals that
    dwarf the invoice — anchoring to strict "Invoice [#] N" matches first
    keeps the invoice total; bare "invoice" words (table headers, footers)
    are only a last resort.
    """
    # Same OCR-spacing scrub as _find_amount ("2 2,369.73" → "22,369.73").
    text = re.sub(r"(?<=\d) (?=\d,\d)", "", text)
    head = text[:4000]

    def _after(pat: "re.Pattern[str]") -> float | None:
        for m in pat.finditer(head):
            end = min(len(text), m.end() + 400)
            # Prefer a total-anchored value ("TOTAL DUE $X"); else the first
            # non-rate money.  Rates ("2.25 %") are never totals, and window
            # max would swallow backup-table summaries on multi-page docs.
            for rx in _TOTAL_HINT_RES:
                a = rx.search(text, m.end(), end)
                if a:
                    mm = _MONEY_RE.search(text, a.end(), min(len(text), a.end() + 80))
                    if mm:
                        try:
                            val = float(mm.group("num").replace(",", ""))
                            return -val if mm.group("sign") == "-" else val
                        except ValueError:
                            continue
            for mm in _MONEY_RE.finditer(text, m.end(), end):
                tail = text[mm.end():mm.end() + 8]
                if re.match(r"\s*%", tail):
                    continue
                try:
                    val = float(mm.group("num").replace(",", ""))
                    return -val if mm.group("sign") == "-" else val
                except ValueError:
                    continue
        return None

    # Strict "Invoice [#] N" anchors first; bare "invoice" words (table
    # headers, footers) only as a last resort.
    got = _after(_INV_NO_RE)
    if got is not None:
        return got
    return _after(re.compile(r"(?i)\binvoice\b"))


def _heuristic_extract(text: str, company_key: str, source_name: str = "",
                     n_pages: int = 0,
                     overrides: dict[str, str] | None = None) -> ExtractedTransaction:
    """Offline fallback: regex-based extraction, convention-compliant filename.

    ``n_pages`` is the PDF page count (0 = unknown, e.g. raw-text callers);
    single-page invoices need strong payment proof to count as paid.
    ``overrides`` (date/amount/doctype/check) wins over detection.
    """
    company = get_company(company_key)
    lowered = text.lower()
    ov = overrides or {}

    is_mortgage = bool(any(rx.search(lowered) for rx in _MORTGAGE_RES))
    statement_kind = _detect_statement_kind(lowered)
    check_m = _CHECK_RE.search(text[:3000])
    check_no = check_m.group(1) if check_m else None
    if check_no is None:
        check_no = _find_spaced_check_no(text)
    if ov.get("check"):
        if not re.fullmatch(r"\d{1,8}", ov["check"]):
            raise ValueError(f"Bad --set check={ov['check']!r} (want digits)")
        check_no = ov["check"]
    # Rotated/sideways check scans: the image is there but the number won't
    # parse.  Presence still routes to CheckAddRq with a numberless head.
    check_present = check_no is not None or any(
        rx.search(text[:3000]) for rx in _CHECK_PRESENCE_RES
    )
    has_pay_proof = _has_pay_proof(text)
    inv_no = _find_invoice_no(text)
    is_bill = bool(_BILL_WORDS.search(lowered))
    vendor = _find_vendor(lowered, text)
    vkey = vendor.lower()

    # HOA vendors are statements even when the word "invoice" appears.
    if vkey in HOA_VENDORS and statement_kind is None:
        statement_kind = "HOA"

    # ARC outgoing special case: "Invoice 112 to Northridge Kidney Center".
    arc_inv_no, arc_clinic = _arc_outgoing(text)

    is_globalcare = vkey == "globalcare"
    txn_date = _find_date(
        source_name, text,
        is_check=check_no is not None or check_present,
        # GlobalCare backups carry payment-like language; the invoice date rules.
        has_pay_proof=has_pay_proof and not is_globalcare,
        is_bill=is_bill,
        is_mortgage=is_mortgage,
    )
    if ov.get("date"):
        try:
            txn_date = _dt.date.fromisoformat(ov["date"])
        except ValueError:
            raise ValueError(f"Bad --set date={ov['date']!r} (want YYYY-MM-DD)") from None
    amount = _find_amount(text, is_mortgage=is_mortgage)
    if ov.get("amount"):
        try:
            amount = float(ov["amount"])
        except ValueError:
            raise ValueError(f"Bad --set amount={ov['amount']!r} (want number)") from None
    if is_globalcare and not is_mortgage:
        # Invoice total, not a backup-table summary total.
        inv_amount = _invoice_amount(text)
        if inv_amount is not None:
            amount = inv_amount
    doc_type = _classify_doc_type(
        lowered, has_pay_proof=has_pay_proof,
        has_invoice_no=inv_no is not None, statement_kind=statement_kind)
    # A bare statement (no payment proof, no check, no invoice#) is never
    # Bill-and-Payment — HOA autopay blurbs don't count as proof.
    if statement_kind and not has_pay_proof and not check_no and not inv_no:
        doc_type = "Payment"
    # Vendor doc overrides (LADWP/GlobalCare bills are never confirmations).
    if check_no is None and vkey in VENDOR_DOC_OVERRIDE:
        doc_type = VENDOR_DOC_OVERRIDE[vkey]  # type: ignore[assignment]
    # HOA vendors are statements, full stop.
    if vkey in HOA_VENDORS:
        doc_type = "Payment"
        statement_kind = statement_kind or "HOA"
    # Single-page invoices: remit-stub/"payment due" wording is not proof.
    if (n_pages == 1 and inv_no is not None and check_no is None
            and doc_type == "Bill-and-Payment"):
        scrubbed = _PROOF_STRIP_RE.sub(" ", text)
        if not _STRONG_PAY_RE.search(scrubbed):
            doc_type = "Invoice"
    if ov.get("doctype"):
        from qb_automation.models.document_schema import DocType as _DocType

        if ov["doctype"] not in ("Payment", "Invoice", "Bill-and-Payment",
                                 "Mortgage", "Tax", "Insurance"):
            raise ValueError(f"Bad --set doctype={ov['doctype']!r}")
        doc_type = ov["doctype"]  # type: ignore[assignment]

    # Explicit vendor→company routing (LADWP books to SIP despite HIP text).
    if vendor.lower() in VENDOR_COMPANY_OVERRIDE:
        override_key = VENDOR_COMPANY_OVERRIDE[vendor.lower()]
        if override_key != company_key:
            company_key = override_key
            company = get_company(company_key)
    company_name = company.display_name if company else company_key
    tag = company.tag if company else company_key

    unit = None
    m_unit = re.search(r"unit\s+(\d+[A-Za-z]?)", text, re.IGNORECASE)
    if m_unit:
        unit = f"Unit {m_unit.group(1)}"
    elif company_key == "arc" and "globalcare" in lowered:
        # GlobalCare docs carry the SPELLED-OUT facility, tag stays ARC.
        unit = _arc_facility_full(text, source_name)
    else:
        codes = [c for c in ("SCD", "NKC", "LCD", "MHD") if re.search(rf"\b{c}\b", text)]
        if len(codes) == 1:
            unit = codes[0]
        elif source_name:
            # Backup tables mention several clinics — the uploader's filename
            # designates the owning one, so it wins ties.
            for c in ("SCD", "NKC", "LCD", "MHD"):
                if re.search(rf"(?<!\w){c}(?!\w)", source_name, re.IGNORECASE):
                    unit = c
                    break
            if unit is None and codes:
                unit = codes[0]
        elif codes:
            unit = codes[0]

    amount_seg = f"{amount:.2f}" if amount else None
    if arc_inv_no is not None or arc_clinic is not None:
        # "2026-09-13 Invoice — Invoice 23421 to Northridge Kidney Center — … — ARC"
        ref = f"Invoice {arc_inv_no} to {arc_clinic}" if arc_inv_no else f"Invoice to {arc_clinic}"
        head = f"{txn_date.isoformat()} Invoice"
        vendor_seg: str | None = None
        unit_seg: str | None = None
        vendor = arc_clinic or vendor
    elif check_no is not None or check_present:
        if ov.get("kind"):
            kind_word = ov["kind"]
        elif _RENEWAL_CERT_RE.search(text[:3000]):
            kind_word = "Renewal Certificate"
        elif "renewal" in (source_name or "").lower():
            # Uploader-labeled renewal (certificate OCR often degrades).
            kind_word = "Renewal Certificate"
        else:
            kind_word = "Invoice" if re.search(r"(?i)\binvoice\b", text) else "Bill"
        check_seg = f"Check {check_no}" if check_no else "Check"
        head = f"{txn_date.isoformat()} {check_seg} & {kind_word}"
        ref, vendor_seg, unit_seg = None, vendor, unit
    elif doc_type == "Bill-and-Payment" and inv_no is not None:
        head = f"{txn_date.isoformat()} Payment Confirmation and Invoice {inv_no}"
        ref, vendor_seg, unit_seg = None, vendor, unit
    elif doc_type == "Payment" and statement_kind in ("HOA", "Bank", "Owner"):
        head = f"{txn_date.isoformat()} {statement_kind} Statement"
        ref, vendor_seg, unit_seg = _find_ref(
            text + " " + Path(source_name).name if source_name else text), vendor, unit
    else:
        head_word = doc_phrase_for(doc_type)
        if (doc_type == "Invoice" and inv_no is None
                and re.search(r"(?i)\bbill\b", text)):
            # A bill that says "bill" with no invoice number heads as Bill;
            # numbered ones keep "Invoice" + the number as ref.
            head_word = "Bill"
        head = f"{txn_date.isoformat()} {head_word}"
        # Prefer the strict invoice number ("Invoice 306200895222") over a
        # loose ref match that can grab backup-table junk ("Invoice 103").
        ref = f"Invoice {inv_no}" if (doc_type == "Invoice" and inv_no) else None
        if ref is None:
            ref = _find_ref(text + " " + Path(source_name).name if source_name else text)
        if ref is not None:
            # Fold "DATE Invoice — Invoice 566" into "DATE Invoice 566".
            first, _, rest = ref.partition(" ")
            if rest and head.lower().endswith(first.lower()):
                head = f"{head} {rest}"
                ref = None
        vendor_seg, unit_seg = vendor, unit
    pay_method = _find_pay_method(text)
    # Memo detail (e.g. LADWP "Electricity & water") rides its own segment
    # right after the vendor.  Memo case is preserved verbatim — it is the
    # one segment allowed lowercase initial words.
    detail = VENDOR_MEMO.get(vkey)
    suggested = build_filename(head, ref, pay_method, vendor_seg, detail, unit_seg,
                               amount_seg, tag)
    issues = verify_conformance(suggested + ".pdf")
    if issues:
        log.debug("Heuristic filename conformance notes for %s: %s", source_name, issues)
    detail = VENDOR_MEMO.get(vkey)
    ledger_memo = f"{vendor} {amount:.2f}" if detail is None else f"{vendor} {detail} {amount:.2f}"
    return ExtractedTransaction(
        company_name=company_name,
        date=txn_date,
        vendor=vendor,
        amount=amount,
        doc_type=doc_type,  # type: ignore[arg-type]
        unit_class=unit,
        check_no=check_no,
        check_present=check_present and check_no is None,
        header_memo=f"{doc_type} from {vendor} on {txn_date.isoformat()}",
        ledger_memo=ledger_memo,
        suggested_filename=suggested,
    )


def _load_profile_block(company_key: str) -> str:
    """Best-effort company profile context; empty string when profiles unbuilt."""
    try:
        from qb_automation.services.company_profile import format_profile_for_prompt, load_profiles

        profiles = load_profiles()
        profile = profiles.get(company_key)
        if profile is None:
            return ""
        return format_profile_for_prompt(profile)
    except Exception as exc:  # noqa: BLE001 - context is optional, never fatal
        log.debug("Could not load company profile for %s: %s", company_key, exc)
        return ""


def _call_gemini(
    text: str, company_key: str, examples_text: str, profile_text: str = ""
) -> ExtractedTransaction | None:
    """Try LangChain Gemini structured output; return None when unavailable."""
    if not (settings.GOOGLE_API_KEY or settings.GCP_PROJECT):
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GOOGLE_API_KEY or None,
        )
        structured = llm.with_structured_output(ExtractedTransaction)
        company = get_company(company_key)
        context_block = (
            f"Naming context for this company (observed patterns — imitate them):\n"
            f"{profile_text}\n\n" if profile_text else ""
        )
        try:
            from qb_automation.services.resolvers import llm_context_block
            qb_lists = llm_context_block(company_key)
        except Exception:  # noqa: BLE001 - roster context optional, never fatal
            qb_lists = ""
        prompt = (
            f"{SYSTEM_PROMPT}\n\nCompany: {company.display_name if company else company_key}\n"
            f"{context_block}"
            + (f"Vendor/account/class roster:\n{qb_lists}\n" if qb_lists else "")
            + (
            f"Historical naming examples for this company:\n{examples_text}\n\n"
            f"Document text:\n{text[:10000]}\n\nExtract the transaction."
            )
        )
        result = structured.invoke(prompt)
        if isinstance(result, ExtractedTransaction):
            return result
        return ExtractedTransaction.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - fall back to heuristic
        log.warning("Gemini extraction failed, using heuristic fallback: %s", exc)
        return None


def extract_transaction(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    pdf_bytes: bytes | None = None,
    company_key: str = "",
    use_llm: bool = True,
    overrides: dict[str, str] | None = None,
    orientations: dict[int, int] | None = None,
) -> ExtractedTransaction:
    """Main entry point: raw text / PDF path / PDF bytes → ExtractedTransaction.

    ``overrides`` (date/amount/doctype/check) applies to the heuristic path;
    LLM results return as-is.  ``orientations`` is filled with winning
    auto-orient angles per OCR'd page when given.
    """
    from pypdf import PdfReader

    n_pages = 0  # 0 = unknown (raw-text callers); single-page rule needs 1.
    if text is None:
        if pdf_path is not None:
            text = extract_text_from_pdf(Path(pdf_path), orientations=orientations)
            try:
                n_pages = len(PdfReader(str(pdf_path)).pages)
            except Exception:  # noqa: BLE001 - page count is advisory only
                log.debug("Could not count pages for %s", pdf_path)
        elif pdf_bytes is not None:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                tmp.write(pdf_bytes)
                tmp.flush()
                text = extract_text_from_pdf(Path(tmp.name), orientations=orientations)
                try:
                    n_pages = len(PdfReader(tmp.name).pages)
                except Exception:  # noqa: BLE001 - advisory only
                    log.debug("Could not count pages for PDF bytes")
        else:
            raise ValueError("Provide text, pdf_path, or pdf_bytes")
    text = text or ""
    source_name = str(pdf_path) if pdf_path else ""

    if not company_key and source_name:
        matched = match_filename_to_company(Path(source_name).name)
        if matched:
            company_key = matched.key
    if not company_key:
        raise ValueError("company_key is required (could not infer from filename)")

    examples = relevant_examples(company_key, k=settings.FEW_SHOT_K)
    examples_text = "\n".join(
        f"- {e.file_name}  || excerpt: {e.text_excerpt[:300]}" for e in examples
    ) or "(no historical examples yet)"

    if use_llm:
        llm_result = _call_gemini(text, company_key, examples_text, _load_profile_block(company_key))
        if llm_result is not None:
            return llm_result
    return _heuristic_extract(text, company_key, source_name, n_pages=n_pages,
                              overrides=overrides)
