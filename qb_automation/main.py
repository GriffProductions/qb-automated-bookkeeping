"""CLI runner: index historical PDFs or process workbench / single files.

Examples (PowerShell):
  python -m qb_automation.main index
  python -m qb_automation.main process-one --pdf "path/to/file.pdf" --company-key valencia_victoria_120
  python -m qb_automation.main process-workbench --limit 10 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from qb_automation.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("qb_automation")


def cmd_index(_args: argparse.Namespace) -> int:
    from qb_automation.services.pdf_indexer import build_index

    index = build_index()
    print(f"Indexed {len(index.entries)} PDFs -> {settings.HISTORICAL_INDEX_PATH}")
    return 0


def _stage_and_emit(txn, pdf: Path, dry_run: bool) -> None:
    from qb_automation.services.qbxml_builder import build_qbxml

    payload = build_qbxml(txn)
    safe_name = txn.suggested_filename + ".pdf"
    staged_pdf = settings.STAGING_DIR / txn.company_name / safe_name
    qbxml_path = settings.QBXML_OUT_DIR / txn.company_name / (txn.suggested_filename + ".qbxml")
    print(f"[{txn.doc_type}] {txn.company_name} | {txn.vendor} {txn.amount:.2f} -> {safe_name}")
    print(f"  qbxml: {payload.request_type}")
    if dry_run:
        return
    staged_pdf.parent.mkdir(parents=True, exist_ok=True)
    qbxml_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, staged_pdf)
    qbxml_path.write_text(payload.qbxml, encoding="utf-8")


def cmd_process_one(args: argparse.Namespace) -> int:
    from qb_automation.services.llm_extractor import extract_transaction
    from qb_automation.services.validator import validate_transaction

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 2
    txn = extract_transaction(pdf_path=pdf, company_key=args.company_key, use_llm=not args.no_llm)
    txn = validate_transaction(txn.model_dump(mode="json"))
    _stage_and_emit(txn, pdf, dry_run=args.dry_run)
    return 0


def cmd_process_workbench(args: argparse.Namespace) -> int:
    from qb_automation.config.company_registry import match_folder_to_company
    from qb_automation.services.llm_extractor import extract_transaction
    from qb_automation.services.validator import validate_transaction

    processed = 0
    for root in (settings.REAL_ESTATE_ROOT, settings.DIALYSIS_ROOT):
        wb = next((c for c in root.iterdir() if c.is_dir() and "Workbench" in c.name), None)
        if wb is None:
            log.warning("No workbench under %s", root)
            continue
        pdfs = sorted(wb.rglob("*.pdf"))
        for pdf in pdfs:
            if processed >= args.limit:
                break
            # Company from parent folder name, fallback to filename tag
            company = match_folder_to_company(pdf.parent.name)
            if company is None:
                from qb_automation.config.company_registry import match_filename_to_company

                company = match_filename_to_company(pdf.name)
            if company is None:
                log.warning("Skipping (no company match): %s", pdf)
                continue
            try:
                txn = extract_transaction(pdf_path=pdf, company_key=company.key, use_llm=not args.no_llm)
                txn = validate_transaction(txn.model_dump(mode="json"))
                _stage_and_emit(txn, pdf, dry_run=args.dry_run)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                log.error("Failed %s: %s", pdf, exc)
    print(f"Processed {processed} workbench file(s){' (dry-run)' if args.dry_run else ''}.")
    return 0


def cmd_profiles(_args: argparse.Namespace) -> int:
    from qb_automation.services.company_profile import build_profiles, save_profiles

    profiles = build_profiles()
    path = save_profiles(profiles)
    total = sum(p.file_count for p in profiles.values())
    print(f"Built {len(profiles)} company profiles from {total} filenames -> {path}")
    for key in sorted(profiles):
        p = profiles[key]
        print(f"  {key}: {p.file_count} files, amount~{int(p.with_amount_rate*100)}%")
    return 0


def cmd_build_testkit(args: argparse.Namespace) -> int:
    from qb_automation.services.testkit import write_kit

    out = Path(args.out)
    manifest = write_kit(out, n=args.n, per_batch=args.per_batch, seed=args.seed)
    print(f"Kit: {manifest['n']} samples in {len(list(out.glob('batch_*.md')))} batches -> {out}")
    reasons: dict[str, int] = {}
    for s in manifest["samples"]:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    print("Mix:", reasons)
    return 0


def cmd_score_testkit(args: argparse.Namespace) -> int:
    from qb_automation.services.testkit import print_report, score_kit

    summary = score_kit(Path(args.kit), Path(args.replies))
    print_report(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qb_automation", description="Automated bookkeeping pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="Scan historical subfolders -> data/historical_index.json")
    sub.add_parser("profiles", help="Distill filename corpus -> data/company_profiles.json (LLM context)")

    one = sub.add_parser("process-one", help="Extract + build qbXML for a single PDF")
    one.add_argument("--pdf", required=True)
    one.add_argument("--company-key", required=True, help="e.g. valencia_victoria_120, arc, spd")
    one.add_argument("--dry-run", action="store_true")
    one.add_argument("--no-llm", action="store_true", help="Force heuristic extractor (no cloud call)")

    wb = sub.add_parser("process-workbench", help="Batch process Stephen's Workbench PDFs")
    wb.add_argument("--limit", type=int, default=20)
    wb.add_argument("--dry-run", action="store_true")
    wb.add_argument("--no-llm", action="store_true")

    kit = sub.add_parser("build-testkit", help="Build Gemini-app browser pilot kit")
    kit.add_argument("--out", default="data/browser_test_kit")
    kit.add_argument("--n", type=int, default=24)
    kit.add_argument("--per-batch", type=int, default=4)
    kit.add_argument("--seed", type=int, default=7)

    score = sub.add_parser("score-testkit", help="Score pasted Gemini replies vs answer key")
    score.add_argument("--kit", default="data/browser_test_kit")
    score.add_argument("--replies", required=True, help="File with pasted Gemini replies")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "profiles":
        return cmd_profiles(args)
    if args.cmd == "process-one":
        return cmd_process_one(args)
    if args.cmd == "process-workbench":
        return cmd_process_workbench(args)
    if args.cmd == "build-testkit":
        return cmd_build_testkit(args)
    if args.cmd == "score-testkit":
        return cmd_score_testkit(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
