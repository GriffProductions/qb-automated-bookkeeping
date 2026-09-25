"""Browser test-kit builder + scorer (Gemini-app accuracy pilot).

No cloud setup: the user attaches real workbench PDFs in gemini.google.com,
pastes context blocks from the generated kit, and pastes Gemini's replies
back for scoring here.

Layout of the generated kit (``data/browser_test_kit/``)::

    README.md            how to run the chat session
    00_setup_prompt.md   convention + strict answer template (paste first)
    batch_01.md …        4 docs each: what to attach + context + few-shots
    answer_key.json      DOC-XX → actual filename (never paste this)
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from qb_automation.config import settings
from qb_automation.config.company_registry import Division, get_company, match_folder_to_company
from qb_automation.services.company_profile import CompanyProfile, load_profiles
from qb_automation.services.convention import parse_filename
from qb_automation.services.naming import filenames_equal

log = logging.getLogger(__name__)

MONEY_TOKEN_RE = re.compile(r"-?[\d,]+\.\d{2}")

ANSWER_TEMPLATE = """DOC-{doc_id}:
company_name: <company display name>
date: <YYYY-MM-DD>
vendor: <vendor>
amount: <number, negative for refunds>
doc_type: <Payment|Invoice|Bill-and-Payment|Mortgage|Tax|Insurance>
unit_class: <e.g. Unit 200, LCD, or NONE>
header_memo: <one-line summary>
ledger_memo: <line memo>
suggested_filename: <name WITHOUT .pdf, segments joined by ' — '>
"""

SETUP_PROMPT = """You are a bookkeeping assistant that renames financial PDFs EXACTLY the way I do.

MY NAMING CONVENTION — segments joined by ' — ' (space + EM DASH U+2014 + space, NEVER a plain hyphen):

  YYYY-MM-DD DocPhrase [ref] — [PayMethod] — Vendor — [Unit] — [detail] — [amount] — Tag.pdf

Rules you must follow:
- DATE is YYYY-MM-DD (monthly statements use YYYY-MM). DocPhrase examples: Bank Statement,
  Owner Statement, HOA Statement, Mortgage Statement, Payment Confirmation, Bill, Invoice <num>,
  Check <num>, "BofA 2265 Payment", LLC Statement Of Information, Cash Flow Statement, General Ledger.
- Middle segments: vendor, pay-method account (BofA 2265, Visa 4267, BoN 0556, Chase 6615 …),
  unit tags (Unit 200) or clinic codes (SCD/NKC/LCD/MHD), detail. Amount (1234.56, negatives
  allowed) sits second-to-last WHEN PRESENT — bank/owner/ledger statements omit it.
- Last segment is always the short company tag. Amount is the total (never rates like 2.25 %).
- Mortgage amount is the OUTSTANDING PRINCIPAL balance, never the amount due.
- SPELL-OUT RULE: facility abbreviations (HIP, ARC, RIG, SIP) appear ONLY as the
  trailing tag. As vendor or associated company, spell out the full name.
- ARC SPECIAL CASES: American Renal Care billing a clinic reads
  "DATE Invoice — Invoice <N> to <Full Clinic Name> — <amount> — ARC" (no vendor
  segment). GlobalCare invoices read "DATE … — Invoice <N> — GlobalCare —
  <Spelled-Out Facility> — <amount> — ARC".
- Checks run on the CHECK date: "DATE Check <N> & Bill" (or & Invoice).
- Statements stay statements ("DATE HOA Statement") — autopay blurbs are not payment proof.
- Bills/invoices default to the INVOICE/BILL date, never due dates. Payment date
  applies only with real payment proof ("DATE Payment Confirmation and Invoice <N>").
- I will attach PDFs and give per-company context + example names. IMITATE them.
- Reply with EXACTLY one block per document, no explanations, no extra text:

""" + ANSWER_TEMPLATE.replace("{doc_id}", "XX")


@dataclass
class KitSample:
    doc_id: str
    company_key: str
    company_name: str
    division: str
    pdf_path: str
    file_name: str
    reason: str  # why selected: edge-… or diversity
    staged_path: str = ""  # kit-local copy (files/batch_NN/DOC-XX.pdf)


def collect_workbench_pdfs() -> list[Path]:
    pdfs: list[Path] = []
    for root in (settings.REAL_ESTATE_ROOT, settings.DIALYSIS_ROOT):
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir() and "workbench" in child.name.lower():
                pdfs.extend(sorted(child.rglob("*.pdf")))
    return sorted(pdfs)


def _company_of(pdf: Path):
    for ancestor in (pdf.parent, *pdf.parent.parents):
        company = match_folder_to_company(ancestor.name)
        if company is not None and company.division != Division.UNASSIGNED:
            return company
    return None


def _embedded_text_len(pdf: Path, cap: int = 600) -> int:
    """Length of embedded text WITHOUT OCR — short ≈ scanned/image-only."""
    try:
        from qb_automation.services.pdf_indexer import extract_text_from_pdf

        return len(extract_text_from_pdf(pdf, max_chars=cap, enable_ocr=False))
    except Exception:  # noqa: BLE001
        return 0


def select_samples(
    n: int = 24,
    seed: int = 7,
    scan_probes: int = 40,
) -> list[KitSample]:
    """Stratified pick: edge cases first, then diversity fill. Deterministic."""
    rng = random.Random(seed)
    pdfs = collect_workbench_pdfs()
    rows: list[tuple[Path, object, object]] = []
    for pdf in pdfs:
        company = _company_of(pdf)
        if company is None:
            continue
        rows.append((pdf, company, parse_filename(pdf.name)))
    rng.shuffle(rows)

    picked: list[tuple[Path, object, object, str]] = []
    taken: set[str] = set()

    def take(pdf, company, parsed, reason) -> bool:
        if pdf.name in taken:
            return False
        taken.add(pdf.name)
        picked.append((pdf, company, parsed, reason))
        return True

    # 1. Edge cases (the files hard rules choke on)
    for pdf, company, p in rows:
        if len(picked) >= 8:
            break
        if p.amount is not None and p.amount < 0 and sum(1 for x in picked if "negative" in x[3]) < 2:
            take(pdf, company, p, "edge-negative-amount")
    for pdf, company, p in rows:
        if len([x for x in picked if "month-only" in x[3]]) >= 2:
            break
        if p.month_only:
            take(pdf, company, p, "edge-month-only-date")
    for pdf, company, p in rows:
        if len([x for x in picked if "multi-amount" in x[3]]) >= 2:
            break
        if len(MONEY_TOKEN_RE.findall(pdf.name)) >= 2:
            take(pdf, company, p, "edge-multi-amount")
    # 2. Likely scans: shortest embedded text among statements
    statements = [r for r in rows
                  if r[0].name not in taken and "statement" in (r[2].doc_phrase or "")]
    rng.shuffle(statements)
    probed = sorted(statements[:scan_probes], key=lambda r: _embedded_text_len(r[0]))
    for pdf, company, p in probed[:2]:
        take(pdf, company, p, "edge-likely-scan")

    # 3. Diversity fill: max 2 per company, prefer unseen head phrases.
    # Dialysis workbench is small (25 vs 151 files) — drain it first so the
    # pilot covers both divisions instead of drowning in Real Estate.
    seen_phrases: set[str] = {str(x[2].doc_phrase) for x in picked}
    company_counts: dict[str, int] = {}
    for x in picked:
        company_counts[x[1].key] = company_counts.get(x[1].key, 0) + 1
    fill_order = sorted(rows, key=lambda r: 0 if r[1].division == Division.DIALYSIS else 1)
    for pdf, company, p in fill_order:
        if len(picked) >= n:
            break
        if pdf.name in taken:
            continue
        if company_counts.get(company.key, 0) >= 2 and str(p.doc_phrase) in seen_phrases:
            continue
        if take(pdf, company, p, "diversity"):
            company_counts[company.key] = company_counts.get(company.key, 0) + 1
            seen_phrases.add(str(p.doc_phrase))
    # 4. Top-up if still short (tiny companies / heavy filters)
    for pdf, company, p in rows:
        if len(picked) >= n:
            break
        take(pdf, company, p, "top-up")

    samples = [
        KitSample(
            doc_id=f"{i + 1:02d}",
            company_key=c.key,
            company_name=c.display_name,
            division=c.division.value,
            pdf_path=str(pdf),
            file_name=pdf.name,
            reason=reason,
        )
        for i, (pdf, c, _p, reason) in enumerate(picked[:n])
    ]
    return samples


def _mini_context(sample: KitSample, profiles: dict[str, CompanyProfile]) -> str:
    p = profiles.get(sample.company_key)
    examples = [s for s in (p.sample_names if p else []) if s != sample.file_name][:3]
    if p is None:
        return f"Company: {sample.company_name} (no profile — follow the convention)."
    top_vendors = ", ".join(list(p.vendors_seen)[:6]) or "—"
    units = ", ".join(list(p.units_seen)[:6]) or "none seen"
    pay = ", ".join(list(p.pay_methods)[:4]) or "—"
    tags = ", ".join(list(p.tags_seen)[:3])
    lines = [
        f"Company: {p.company_name} | tag: {tags} | {p.file_count} files seen",
        f"Vendors here: {top_vendors}. Units: {units}. Pay via: {pay}.",
        f"Amount present ~{int(p.with_amount_rate * 100)}% of names.",
        "Name it like these:",
        *["- " + e for e in examples],
    ]
    return "\n".join(lines)


def _doc_text_excerpt(sample: KitSample, chars: int = 1500) -> str:
    """Embedded text for the paste-only option (no OCR — scans come back empty)."""
    try:
        from qb_automation.services.pdf_indexer import extract_text_from_pdf

        return extract_text_from_pdf(Path(sample.pdf_path), max_chars=chars, enable_ocr=False)
    except Exception:  # noqa: BLE001
        return ""


def write_kit(
    out_dir: Path,
    n: int = 24,
    per_batch: int = 4,
    seed: int = 7,
    stage_pdfs: bool = True,
    inline_text: bool = True,
    text_chars: int = 1500,
) -> dict[str, object]:
    """Write the full kit. Returns manifest dict (also saved as manifest.json).

    Staged copies live in ``files/batch_NN/DOC-XX.pdf`` (neutral names leak
    nothing about the expected answer).  ``files/`` is git-ignored; the kit
    markdown + answer key are the portable artifacts.
    """
    import shutil

    from qb_automation.services.naming import SEP  # noqa: F401 - doc anchor

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles()
    samples = select_samples(n=n, seed=seed)

    (out_dir / "00_setup_prompt.md").write_text(
        "# Paste this FIRST as one message\n\n" + SETUP_PROMPT, encoding="utf-8")

    batches = [samples[i:i + per_batch] for i in range(0, len(samples), per_batch)]
    for bi, batch in enumerate(batches, start=1):
        parts = [
            f"# Batch {bi:02d} — ONE message: drag in the {len(batch)} PDFs from "
            f"`files/batch_{bi:02d}/` (Ctrl+A), then paste everything below\n"
        ]
        for s in batch:
            staged = f"files/batch_{bi:02d}/DOC-{s.doc_id}.pdf"
            s.staged_path = staged
            if stage_pdfs:
                dest = out_dir / staged
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s.pdf_path, dest)
            parts += [
                f"## DOC-{s.doc_id} — attached as DOC-{s.doc_id}.pdf",
                _mini_context(s, profiles),
                "",
            ]
            if inline_text:
                excerpt = _doc_text_excerpt(s, text_chars)
                if excerpt:
                    parts += [f"Document text: {excerpt}", ""]
                else:
                    parts += ["Document text: (none embedded — this one NEEDS its PDF attached)", ""]
        (out_dir / f"batch_{bi:02d}.md").write_text("\n".join(parts), encoding="utf-8")

    answer_key = {s.doc_id: s.file_name for s in samples}
    (out_dir / "answer_key.json").write_text(
        json.dumps(answer_key, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "n": len(samples),
        "seed": seed,
        "samples": [asdict(s) for s in samples],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    readme = (
        "# Browser renaming pilot — how to run\n\n"
        "EASY MODE (recommended): each `files/batch_NN/` folder holds that batch's PDFs\n"
        "as DOC-01.pdf … (neutral names, copied from your drives). Per batch: open the\n"
        "folder, Ctrl+A, drag into ONE gemini.google.com message, paste the batch .md.\n\n"
        "LAZY MODE (no attaching): just paste each batch .md — document text is inline.\n"
        "Works for everything except the 1–2 docs marked NEEDS-its-PDF-attached (scans).\n\n"
        "1. Paste `00_setup_prompt.md` as the first message.\n"
        "2. Six batch messages (easy or lazy mode).\n"
        "3. Copy ALL of Gemini's replies into `gemini_replies.md` in this folder.\n"
        "4. Score: `python -m qb_automation.main score-testkit --kit data/browser_test_kit "
        "--replies data/browser_test_kit/gemini_replies.md`\n\n"
        "NEVER paste `answer_key.json` into the chat — it is the hidden answer sheet.\n"
    )
    (out_dir / "README.md").write_text(readme, encoding="utf-8")
    return manifest


FIELD_WEIGHTS = {"date": 20, "doc_phrase": 15, "vendor": 20, "unit": 10, "amount": 20, "tag": 15}


def parse_replies(replies_text: str) -> dict[str, str]:
    """Extract DOC-XX → suggested_filename from pasted Gemini replies."""
    found: dict[str, str] = {}
    current: str | None = None
    for line in replies_text.splitlines():
        m = re.match(r"\s*DOC-(\d{2})\s*:", line)
        if m:
            current = m.group(1)
            continue
        if current:
            m2 = re.match(r"\s*suggested_filename\s*:\s*(.+)", line, re.IGNORECASE)
            if m2:
                found[current] = m2.group(1).strip().removesuffix(".pdf").strip()
                current = None
    return found


def score_kit(kit_dir: Path, replies_path: Path) -> dict[str, object]:
    """Score pasted replies vs the hidden key. Returns summary + per-doc rows."""
    kit_dir, replies_path = Path(kit_dir), Path(replies_path)
    key = json.loads((kit_dir / "answer_key.json").read_text(encoding="utf-8"))
    manifest = json.loads((kit_dir / "manifest.json").read_text(encoding="utf-8"))
    got = parse_replies(replies_path.read_text(encoding="utf-8"))

    rows: list[dict[str, object]] = []
    for s in manifest["samples"]:
        doc_id, truth = s["doc_id"], key[s["doc_id"]]
        guess = got.get(doc_id, "")
        if not guess:
            rows.append({"doc_id": doc_id, "status": "missing", "score": 0,
                         "truth": truth, "guess": ""})
            continue
        exact = filenames_equal(guess + ".pdf", truth)
        pt, pg = parse_filename(truth), parse_filename(guess + ".pdf")
        fields = {
            "date": pt.date == pg.date,
            "doc_phrase": pt.doc_phrase == pg.doc_phrase,
            "vendor": (pt.vendor or "").casefold() == (pg.vendor or "").casefold(),
            "unit": (pt.unit or "") == (pg.unit or ""),
            "amount": pt.amount == pg.amount,
            "tag": (pt.tag or "").casefold() == (pg.tag or "").casefold(),
        }
        field_score = sum(w for f, w in FIELD_WEIGHTS.items() if fields[f])
        rows.append({"doc_id": doc_id, "status": "exact" if exact else "partial",
                     "score": 100 if exact else field_score, "fields": fields,
                     "truth": truth, "guess": guess + ".pdf",
                     "company": s["company_key"], "reason": s["reason"]})
    answered = [r for r in rows if r["status"] != "missing"]
    return {
        "n": len(rows),
        "answered": len(answered),
        "exact": sum(1 for r in rows if r["status"] == "exact"),
        "exact_rate": round(sum(1 for r in rows if r["status"] == "exact") / len(rows), 3) if rows else 0,
        "mean_field_score": round(sum(r["score"] for r in answered) / len(answered), 1) if answered else 0,
        "rows": rows,
    }


def print_report(summary: dict[str, object]) -> None:
    print(f"Exact: {summary['exact']}/{summary['n']} ({summary['exact_rate']:.0%}) | "
          f"mean field score: {summary['mean_field_score']} | answered: {summary['answered']}")
    for r in summary["rows"]:
        mark = "OK " if r["status"] == "exact" else ("-- " if r["status"] == "missing" else ".. ")
        print(f"{mark}DOC-{r['doc_id']} [{r.get('company','')}/{r.get('reason','')}] score={r['score']}")
        if r["status"] != "exact":
            print(f"    truth: {r['truth']}")
            print(f"    guess: {r.get('guess') or '(missing)'}")
