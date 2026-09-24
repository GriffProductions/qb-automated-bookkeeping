"""LLM extraction: document text/bytes + few-shot history → ExtractedTransaction.

Production path uses Gemini via LangChain ``with_structured_output``.  When no
API key / credentials are configured (local dev, CI), a deterministic
heuristic fallback parses vendor/amount/date from the text so the pipeline
and tests run without cloud access.
"""

from __future__ import annotations

import logging
import re
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
Return ONLY the structured object.
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
    r"(?i)\b(invoice|inv|check|bill|policy|account)\b\s*(?:#|n[o.]*)?\s*"
    r"(?=[A-Za-z0-9-]*\d)([A-Za-z0-9][A-Za-z0-9-]{1,24})")
_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
           "jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_MON_RE = re.compile(rf"(?i)\b({_MONTHS})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b")
_MON_TO_NUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_PAY_METHOD_RE = re.compile(
    r"(?i)\b(visa|bofa|bon|chase|f&m|pnc)\s*[\d*]{3,6}\b")


def _find_date(source_name: str, text: str) -> date:
    """Prefer the user filename date, then doc dates, else today."""
    for haystack in (source_name, text[:1500]):
        m = _DATE_YMD_RE.search(haystack or "")
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    m = _DATE_MDY_RE.search(text[:1500])
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_MON_RE.search((source_name or "") + " " + text[:1500])
    if m:
        try:
            return date(int(m.group(3)), _MON_TO_NUM[m.group(1)[:3].lower()], int(m.group(2)))
        except ValueError:
            pass
    return date.today()


def _find_amount(text: str) -> float:
    """Prefer totals on 'amount due/total/balance' lines; else the max money value."""
    amounts: list[tuple[float, str]] = []
    for line in text.splitlines() or [text]:
        for m in _MONEY_RE.finditer(line):
            try:
                val = float(m.group("num").replace(",", ""))
                if m.group("sign") == "-":
                    val = -val
                amounts.append((val, line))
            except ValueError:
                continue
    if not amounts:
        # Whitespace-collapsed text has no lines — scan whole blob
        for m in _MONEY_RE.finditer(text):
            try:
                val = float(m.group("num").replace(",", ""))
                amounts.append((-val if m.group("sign") == "-" else val, ""))
            except ValueError:
                continue
    if not amounts:
        return 0.0
    # Prefer amounts on total/amount-due lines; among those (or overall), take
    # the largest — this skips rates like "2.25 %" and running balances.
    hinted = [val for val, line in amounts
              if any(rx.search(line) for rx in _TOTAL_HINT_RES)]
    pool = hinted or [val for val, _ in amounts]
    return max(pool, key=abs)


def _classify_doc_type(lowered: str) -> str:
    if any(rx.search(lowered) for rx in _MORTGAGE_RES):
        return "Mortgage"
    if any(rx.search(lowered) for rx in _INSURANCE_RES):
        return "Insurance"
    if any(rx.search(lowered) for rx in _TAX_RES):
        return "Tax"
    if _BILL_WORDS.search(lowered) and _PAY_WORDS.search(lowered):
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


def _heuristic_extract(text: str, company_key: str, source_name: str = "") -> ExtractedTransaction:
    """Offline fallback: regex-based extraction, convention-compliant filename."""
    company = get_company(company_key)
    company_name = company.display_name if company else company_key
    tag = company.tag if company else company_key

    txn_date = _find_date(source_name, text)
    amount = _find_amount(text)
    lowered = text.lower()
    doc_type = _classify_doc_type(lowered)
    vendor = _find_vendor(lowered, text)

    unit = None
    m_unit = re.search(r"unit\s+(\d+[A-Za-z]?)", text, re.IGNORECASE)
    if m_unit:
        unit = f"Unit {m_unit.group(1)}"
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

    head = f"{txn_date.isoformat()} {doc_phrase_for(doc_type)}"
    ref = _find_ref(text + " " + Path(source_name).name if source_name else text)
    pay_method = _find_pay_method(text)
    amount_seg = f"{amount:.2f}" if amount else None
    suggested = build_filename(head, ref, pay_method, vendor, unit, amount_seg, tag)
    issues = verify_conformance(suggested + ".pdf")
    if issues:
        log.debug("Heuristic filename conformance notes for %s: %s", source_name, issues)
    return ExtractedTransaction(
        company_name=company_name,
        date=txn_date,
        vendor=vendor,
        amount=amount,
        doc_type=doc_type,  # type: ignore[arg-type]
        unit_class=unit,
        header_memo=f"{doc_type} from {vendor} on {txn_date.isoformat()}",
        ledger_memo=f"{vendor} {amount:.2f}",
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
        prompt = (
            f"{SYSTEM_PROMPT}\n\nCompany: {company.display_name if company else company_key}\n"
            f"{context_block}"
            f"Historical naming examples for this company:\n{examples_text}\n\n"
            f"Document text:\n{text[:10000]}\n\nExtract the transaction."
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
) -> ExtractedTransaction:
    """Main entry point: raw text / PDF path / PDF bytes → ExtractedTransaction."""
    if text is None:
        if pdf_path is not None:
            text = extract_text_from_pdf(Path(pdf_path))
        elif pdf_bytes is not None:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                tmp.write(pdf_bytes)
                tmp.flush()
                text = extract_text_from_pdf(Path(tmp.name))
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
    return _heuristic_extract(text, company_key, source_name)
