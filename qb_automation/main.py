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


def _parse_kv(pairs: list[str] | None, kind: str) -> dict[str, str]:
    """Parse ['vendor=X', ...] flags into {field: value}.

    Roster fields are vendor/account/class; scalar overrides are
    date (YYYY-MM-DD), amount, doctype, check (check number), and
    kind (check-head kind word, e.g. "Renewal Certificate").
    """
    valid = ("vendor", "account", "class", "date", "amount", "doctype", "check", "kind")
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            print(f"Ignoring malformed --{kind} {item!r} (want field=value).", file=sys.stderr)
            continue
        field, value = item.split("=", 1)
        field = field.strip().lower()
        if field not in valid:
            print(f"Ignoring --{kind} field {field!r} (want {'/'.join(valid)}).",
                  file=sys.stderr)
            continue
        out[field] = value.strip()
    return out


def cmd_review_one(args: argparse.Namespace) -> int:
    """Interactive single-file review: extract → card → pick/set → approve.

    Read-only unless --approve.  Prints a review card with resolved refs,
    warnings, and numbered top-3 corrections.  Exit 0 = card shown (or
    approved); 2 = bad correction value; 3 = blocked (unlisted vendor
    without --force on approve).
    """
    import json
    from datetime import datetime, timezone

    from qb_automation.config.company_registry import get_company, match_filename_to_company
    from qb_automation.services import resolvers
    from qb_automation.services.llm_extractor import extract_transaction
    from qb_automation.services.qbxml_builder import build_qbxml, validate_for_import
    from qb_automation.services.validator import ExtractionValidationError, validate_transaction

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 2
    orig_name = pdf.name
    rotation_source = "flags"
    if not args.rotate and not args.rotate_pages and settings.ROTATIONS_PATH.exists():
        try:
            import json as _json

            stored = _json.loads(settings.ROTATIONS_PATH.read_text(encoding="utf-8"))
            hit = stored.get(orig_name)
            if hit:
                args.rotate = hit.get("rotate", 0)
                args.rotate_pages = hit.get("rotate_pages", "")
                rotation_source = "memory"
        except Exception:  # noqa: BLE001 - rotation memory is advisory only
            pass
    if rotation_source == "memory":
        print(f"Rotation from memory: rotate={args.rotate} rotate-pages={args.rotate_pages!r}")
    per_page: dict[int, int] = {}
    if args.rotate_pages:
        for item in args.rotate_pages.split(","):
            try:
                num, deg = item.split(":")
                if int(deg) not in (0, 90, 180, 270):
                    raise ValueError
                per_page[int(num)] = int(deg)
            except ValueError:
                print(f"Ignoring malformed --rotate-pages {item!r} (want N:90).",
                      file=sys.stderr)
    if args.rotate or per_page:
        # Rotate a temp copy (upside-down / sideways scans); original untouched.
        import tempfile

        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(pdf))
        writer = PdfWriter()
        for i, page in enumerate(reader.pages, 1):
            deg = per_page.get(i, args.rotate)
            writer.add_page(page.rotate(deg) if deg else page)
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        writer.write(tmp)
        tmp.close()
        pdf = Path(tmp.name)
    if rotation_source == "flags" and (args.rotate or args.rotate_pages):
        # Rotation is a discovered fact about the PDF — remember it even on
        # dry runs so the next invocation applies it automatically.
        stored = json.loads(settings.ROTATIONS_PATH.read_text(encoding="utf-8")) \
            if settings.ROTATIONS_PATH.exists() else {}
        stored[orig_name] = {"rotate": args.rotate, "rotate_pages": args.rotate_pages}
        settings.ROTATIONS_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        print(f"Rotation saved to memory for {orig_name}")
    company_key = args.company_key
    if not company_key:
        matched = match_filename_to_company(orig_name)
        if matched is None:
            print(f"Could not infer company from filename; pass --company-key.", file=sys.stderr)
            return 2
        company_key = matched.key
    company = get_company(company_key)
    if company is None:
        print(f"Unknown company key: {company_key}", file=sys.stderr)
        return 2

    txn = extract_transaction(pdf_path=pdf, company_key=company_key, use_llm=not args.no_llm,
                              overrides={k: v for k, v in _parse_kv(args.set, "set").items()
                                         if k in ("date", "amount", "doctype", "check", "kind")},
                              orientations=(orientations := {}))
    txn.company_name = company.display_name
    forced_account: str | None = None

    def current_suggestions():
        return resolvers.review_suggestions(company_key, txn.vendor, txn.doc_type, txn.unit_class)

    # Explicit corrections first (--set vendor="Exact Name").
    for field, value in _parse_kv(args.set, "set").items():
        if field == "vendor":
            resolved, exact = resolvers.resolve_vendor(company_key, value)
            if not exact:
                print(f"--set vendor={value!r} is not a listed vendor.")
                for i, s in enumerate(resolvers.suggest_names(
                        resolvers.vendor_names(company_key), value), 1):
                    print(f"  {i}) {s}")
                return 2
            txn.vendor = resolved
        elif field == "account":
            hit = resolvers._validated(value, resolvers.expense_names(company_key))
            if hit is None:
                print(f"--set account={value!r} is not in the {company_key} chart.")
                for i, s in enumerate(resolvers.suggest_names(
                        resolvers.expense_names(company_key), value), 1):
                    print(f"  {i}) {s}")
                return 2
            forced_account = hit
        elif field == "class":
            if value == "":
                txn.unit_class = None
            else:
                hit = resolvers.resolve_class(company_key, value)
                if hit is None:
                    print(f"--set class={value!r} matches no {company_key} class.")
                    for i, s in enumerate(resolvers.suggest_names(
                            resolvers.class_names(company_key), value), 1):
                        print(f"  {i}) {s}")
                    return 2
                txn.unit_class = hit
        # else: scalar overrides (date/amount/doctype/check) applied at extraction.

    # Numbered picks against the card's suggestion lists.
    suggestions = current_suggestions()
    for field, raw in _parse_kv(args.pick, "pick").items():
        try:
            choice = int(raw)
        except ValueError:
            print(f"--pick {field}={raw!r} is not a number (0 = keep).", file=sys.stderr)
            return 2
        options = suggestions.get(field, [])
        if choice == 0 or not options:
            continue
        if not 1 <= choice <= len(options):
            print(f"--pick {field}={choice} out of range (1-{len(options)}, 0 = keep).",
                  file=sys.stderr)
            return 2
        picked = options[choice - 1]
        if field == "vendor":
            txn.vendor = picked
        elif field == "account":
            forced_account = picked
        else:
            txn.unit_class = picked

    try:
        txn = validate_transaction(txn.model_dump(mode="json"))
    except ExtractionValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2

    payload = build_qbxml(txn, forced_account=forced_account)
    warnings = validate_for_import(txn)
    suggestions = current_suggestions()
    vendor_name, vendor_exact = resolvers.resolve_vendor(company_key, txn.vendor)

    print(f"=== REVIEW: {orig_name} ===")
    print(f"Company : {company.display_name} ({company_key})")
    print(f"Date    : {txn.date}  Doc: {txn.doc_type}  Amount: {txn.amount:.2f}"
          + (f"  Check: {txn.check_no}" if txn.check_no else "")
          + ("  Check: present (number illegible)" if txn.check_present else ""))
    print(f"Vendor  : {txn.vendor} -> VendorRef {vendor_name}"
          + ("" if vendor_exact else "  [UNLISTED - BLOCKS IMPORT]"))
    import re as _re
    refs = _re.findall(r"<AccountRef><FullName>(.*?)</FullName>", payload.qbxml)
    print(f"Account : -> {refs[0].strip() if refs else '?'}"
          + ("  [fallback - review]" if forced_account is None and any("Account fallback" in w for w in warnings) else "")
          + ("  [your pick]" if forced_account else ""))
    print(f"Class   : {txn.unit_class}"
          + ("  [omitted - review]" if txn.unit_class and
             resolvers.resolve_class(company_key, txn.unit_class) is None else ""))
    print(f"Memo    : {txn.header_memo}")
    print(f"File    : {txn.suggested_filename}.pdf")
    for w in warnings:
        print(f"WARN    : {w}")
    if txn.check_present:
        print("WARN    : Check image detected but number illegible "
              "(rotated scan?) — import is valid as Check; supply --set check=N "
              "when known, optionally with --rotate-pages")
    if suggestions:
        print("Picks   : (--pick field=N, 0 = keep)")
        for field, options in suggestions.items():
            numbered = "  ".join(f"{i}) {o}" for i, o in enumerate(options, 1))
            print(f"  {field}: {numbered}")
    print(f"qbxml   : {payload.request_type} (not written)")
    shown_angles = {k + 1: v for k, v in orientations.items() if v}
    if shown_angles and not (args.rotate or args.rotate_pages):
        print(f"Pages auto-oriented (straightened on approve): {shown_angles}")

    if not args.approve:
        return 0
    if not vendor_exact and not args.force:
        print("BLOCKED: vendor is not in the QB vendor list. "
              "Pick a suggestion (--pick/--set) or re-run with --force.", file=sys.stderr)
        return 3
    staged_pdf = settings.STAGING_DIR / company.display_name / (txn.suggested_filename + ".pdf")
    qbxml_path = settings.QBXML_OUT_DIR / company.display_name / (txn.suggested_filename + ".qbxml")
    staged_pdf.parent.mkdir(parents=True, exist_ok=True)
    qbxml_path.parent.mkdir(parents=True, exist_ok=True)
    auto_angles = {k: v for k, v in orientations.items() if v}
    if auto_angles and not (args.rotate or args.rotate_pages):
        # Straighten auto-oriented pages so the filed copy reads upright
        # (lossless /Rotate flags; manual rotation already yields straight output).
        from qb_automation.services.ocr import apply_orientations

        straightened = apply_orientations(pdf, staged_pdf, auto_angles)
        print(f"Straightened pages for viewing: {straightened}")
    else:
        shutil.copy2(pdf, staged_pdf)
    if not staged_pdf.exists():
        print(f"ERROR: staging copy failed: {staged_pdf}", file=sys.stderr)
        return 1
    qbxml_path.write_text(payload.qbxml, encoding="utf-8")
    log_path = settings.DATA_DIR / "review_log.json"
    entry = {"pdf": orig_name, "suggested_filename": txn.suggested_filename,
             "company": company_key, "vendor": vendor_name,
             "account": refs[0].strip() if refs else "", "doc_type": txn.doc_type,
             "amount": txn.amount, "date": str(txn.date), "warnings": warnings,
             "forced": bool(forced_account or args.force),
             "rotation": {"rotate": args.rotate, "rotate_pages": args.rotate_pages},
             "approved_at": datetime.now(timezone.utc).isoformat(),
             "qbxml": str(qbxml_path)}
    log_entries = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log_entries.append(entry)
    log_path.write_text(json.dumps(log_entries, indent=2), encoding="utf-8")
    print(f"APPROVED -> {qbxml_path}")
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


def cmd_harvest_accounts(args: argparse.Namespace) -> int:
    """Read-only AccountQuery harvest. Single company (auth test) or --all batch."""
    import json

    from qb_automation.config import settings
    from qb_automation.config.company_registry import get_company
    from qb_automation.services.chart_harvest import (
        harvest_company,
        iter_company_files,
        load_charts,
        merge_charts,
    )
    from qb_automation.services.qb_connection import QBAuthRequired

    out_path = settings.CHART_OF_ACCOUNTS_PATH
    if args.open:
        from qb_automation.services.chart_harvest import harvest_open_file

        try:
            chart = harvest_open_file()
        except Exception as exc:  # noqa: BLE001
            from qb_automation.services.qb_connection import QBAuthRequired

            if isinstance(exc, QBAuthRequired):
                print(f"AUTH NEEDED: {exc}")
                return 3
            print(f"FAILED: {exc}")
            return 1
        existing = load_charts(out_path)
        merged = merge_charts(existing, [chart])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        out_path.write_text(_json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Harvested OPEN file -> {chart.company_key} ({chart.company_name}): "
              f"{len(chart.accounts)} accounts ({len(chart.expenses)} expense)")
        return 0
    if args.company:
        company = get_company(args.company)
        if company is None:
            print(f"Unknown company key: {args.company}")
            return 2
        hits = [(k, q) for k, q in iter_company_files(settings.QB_COMPANY_ROOT)
                if k == args.company]
        if not hits:
            print(f"No live .qbw found for {args.company} under {settings.QB_COMPANY_ROOT}")
            return 2
        targets = [(company.display_name, hits[0][1])]
        keys = [args.company]
        if args.qbw:
            from pathlib import Path as _P

            override = _P(args.qbw)
            if not override.exists():
                print(f"--qbw path does not exist: {override}")
                return 2
            print(f"Overriding {hits[0][1].name} -> {override.name} (currently open file)")
            targets = [(company.display_name, override)]
    else:
        pairs = iter_company_files(settings.QB_COMPANY_ROOT)
        if not pairs:
            print(f"No company files under {settings.QB_COMPANY_ROOT}")
            return 2
        already = set(load_charts(out_path)) if not args.refresh else set()
        pairs = [(k, q) for k, q in pairs if k not in already]
        if not pairs:
            print(f"All companies already harvested ({len(already)}). Use --refresh to redo.")
            return 0
        targets, keys = [], []
        for key, qbw in pairs:
            company = get_company(key)
            targets.append(((company.display_name if company else key), qbw))
            keys.append(key)

    charts, failures, auth_needed = [], [], []
    for (display, qbw), key in zip(targets, keys):
        print(f"--- {key}: {qbw.name} ---", flush=True)
        try:
            chart = harvest_company(key, display, qbw)
        except QBAuthRequired as exc:
            print(f"AUTH NEEDED ({key}): {exc}")
            auth_needed.append(key)
            continue
        except Exception as exc:  # noqa: BLE001 - batch continues past one bad file
            print(f"FAILED {key}: {exc}")
            failures.append(key)
            continue
        print(f"  {len(chart.accounts)} accounts ({len(chart.expenses)} expense)")
        charts.append(chart)
        try:
            existing = load_charts(out_path)
            merged = merge_charts(existing, [chart])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  saved {key} -> {out_path.name}")
        except Exception as exc:  # noqa: BLE001 - save failure noted, batch continues
            print(f"  SAVE FAILED {key}: {exc}")
            failures.append(key + " (save)")

    print(f"Saved {len(charts)} chart(s) -> {out_path}"
          + (f" | auth-needed: {auth_needed}" if auth_needed else "")
          + (f" | failures: {failures}" if failures else ""))
    if auth_needed:
        return 3
    return 1 if failures else 0


def _harvest_entities(args: argparse.Namespace, kind: str) -> int:
    """Shared unattended batch for vendors / classes. QB may stay closed."""
    import json

    from qb_automation.config import settings
    from qb_automation.config.company_registry import get_company
    from qb_automation.services.chart_harvest import iter_company_files, load_charts
    from qb_automation.services.entity_harvest import (
        harvest_classes,
        harvest_vendors,
        load_entities,
        merge_entities,
    )
    from qb_automation.services.qb_connection import QBAuthRequired

    is_vendor = kind == "vendors"
    out_path = settings.VENDORS_PATH if is_vendor else settings.CLASSES_PATH
    harvest_one = harvest_vendors if is_vendor else harvest_classes
    label = "vendor" if is_vendor else "class"
    plural = "vendors" if is_vendor else "classes"

    if args.open:
        from qb_automation.services.chart_harvest import (
            harvest_open_file,
            match_open_company,
        )

        # Reuse open-file session: query vendors/classes on the active file.
        from qb_automation.services import entity_harvest as eh
        from qb_automation.services.qb_connection import negotiate_version, qb_session

        with qb_session("") as (rp, ticket):
            from qb_automation.services.chart_harvest import build_company_query, parse_company_name

            version = negotiate_version(rp)
            who = str(rp.ProcessRequest(ticket, build_company_query(version)))
            qb_name = parse_company_name(who)
            key = match_open_company(qb_name) if qb_name else None
            if key is None:
                print(f"Could not map open company {qb_name!r} to registry.")
                return 2
            company = get_company(key)
            display = company.display_name if company else (qb_name or key)
            query = eh.build_vendor_query(version) if is_vendor else eh.build_class_query(version)
            response = str(rp.ProcessRequest(ticket, query))
        if is_vendor:
            item = eh.CompanyVendors(company_key=key, company_name=display,
                                     company_file=f"<open file: {qb_name}>",
                                     qbxml_version=version,
                                     vendors=eh.parse_vendor_response(response))
            from datetime import datetime, timezone
            item.harvested_at = datetime.now(timezone.utc).isoformat()
            n = len(item.vendors)
        else:
            item = eh.CompanyClasses(company_key=key, company_name=display,
                                     company_file=f"<open file: {qb_name}>",
                                     qbxml_version=version,
                                     classes=eh.parse_class_response(response))
            from datetime import datetime, timezone
            item.harvested_at = datetime.now(timezone.utc).isoformat()
            n = len(item.classes)
        existing = load_entities(out_path)
        merged = merge_entities(existing, [item])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Harvested OPEN file -> {key} ({display}): {n} {plural}")
        return 0

    if args.company:
        company = get_company(args.company)
        if company is None:
            print(f"Unknown company key: {args.company}")
            return 2
        hits = [(k, q) for k, q in iter_company_files(settings.QB_COMPANY_ROOT)
                if k == args.company]
        if not hits:
            print(f"No live .qbw found for {args.company} under {settings.QB_COMPANY_ROOT}")
            return 2
        targets = [(company.display_name, hits[0][1])]
        keys = [args.company]
    else:
        pairs = iter_company_files(settings.QB_COMPANY_ROOT)
        if not pairs:
            print(f"No company files under {settings.QB_COMPANY_ROOT}")
            return 2
        already = set(load_entities(out_path)) if not args.refresh else set()
        pairs = [(k, q) for k, q in pairs if k not in already]
        if not pairs:
            print(f"All companies already harvested ({len(already)}). Use --refresh to redo.")
            return 0
        targets, keys = [], []
        for key, qbw in pairs:
            company = get_company(key)
            targets.append(((company.display_name if company else key), qbw))
            keys.append(key)

    items, failures, auth_needed = [], [], []
    for (display, qbw), key in zip(targets, keys):
        print(f"--- {key}: {qbw.name} ---", flush=True)
        try:
            item = harvest_one(key, display, qbw)
        except QBAuthRequired as exc:
            # Auth / backup-popup escape / closed-file prompt: bank progress,
            # note the file, keep going. Never kicks the whole batch out.
            # __cause__ holds the raw COM error — print it so AUTH mislabels
            # can't hide the real failure again.
            raw = f" | raw: {exc.__cause__}" if exc.__cause__ else ""
            print(f"AUTH NEEDED ({key}): {exc}{raw}")
            auth_needed.append(key)
            continue
        except Exception as exc:  # noqa: BLE001 - batch continues past one bad file
            print(f"FAILED {key}: {exc}")
            failures.append(key)
            continue
        n = len(item.vendors) if is_vendor else len(item.classes)
        print(f"  {n} {plural}")
        items.append(item)
        try:
            existing = load_entities(out_path)
            merged = merge_entities(existing, [item])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  saved {key} -> {out_path.name}")
        except Exception as exc:  # noqa: BLE001 - save failure noted, batch continues
            print(f"  SAVE FAILED {key}: {exc}")
            failures.append(key + " (save)")

    print(f"Saved {len(items)} {plural} -> {out_path}"
          + (f" | auth-needed: {auth_needed}" if auth_needed else "")
          + (f" | failures: {failures}" if failures else ""))
    if auth_needed:
        return 3
    return 1 if failures else 0


def cmd_harvest_vendors(args: argparse.Namespace) -> int:
    return _harvest_entities(args, "vendors")


def cmd_harvest_classes(args: argparse.Namespace) -> int:
    return _harvest_entities(args, "classes")


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

    rv = sub.add_parser("review-one", help="Review card for one PDF: refs, warnings, top-3 picks")
    rv.add_argument("--pdf", required=True)
    rv.add_argument("--company-key", default="",
                    help="Registry key; inferred from filename when omitted")
    rv.add_argument("--no-llm", action="store_true", help="Force heuristic extractor (no cloud call)")
    rv.add_argument("--set", action="append", default=[],
                    help='Explicit correction field="Exact Name" (repeatable)')
    rv.add_argument("--pick", action="append", default=[],
                    help="Numbered correction field=N from the card (0 = keep, repeatable)")
    rv.add_argument("--approve", action="store_true", help="Stage PDF + write qbXML + log review")
    rv.add_argument("--force", action="store_true",
                    help="Approve even with an unlisted vendor (import may reject)")
    rv.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="Rotate all pages before extraction (e.g. upside-down scans)")
    rv.add_argument("--rotate-pages", default="",
                    help='Per-page rotation "1:90,2:180" (1-based pages, wins over --rotate)')

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

    hv = sub.add_parser("harvest-accounts", help="Read-only QB AccountQuery harvest (needs QB auth)")
    hv.add_argument("--company", default="", help="Single company key (auth test), omit for --all")
    hv.add_argument("--all", action="store_true", help="Harvest every live .qbw found")
    hv.add_argument("--qbw", default="", help="Explicit .qbw path (overrides auto-resolve, e.g. open file)")
    hv.add_argument("--open", action="store_true", help="Harvest whichever file is open in QB (no path needed)")
    hv.add_argument("--refresh", action="store_true", help="With --all: re-harvest even already-saved companies")

    vv = sub.add_parser("harvest-vendors", help="Read-only QB VendorQuery harvest (unattended after grants)")
    vv.add_argument("--company", default="", help="Single company key, omit for --all")
    vv.add_argument("--all", action="store_true", help="Harvest every live .qbw found")
    vv.add_argument("--open", action="store_true", help="Harvest whichever file is open in QB")
    vv.add_argument("--refresh", action="store_true", help="With --all: re-harvest even already-saved companies")

    cc = sub.add_parser("harvest-classes", help="Read-only QB ClassQuery harvest (unattended after grants)")
    cc.add_argument("--company", default="", help="Single company key, omit for --all")
    cc.add_argument("--all", action="store_true", help="Harvest every live .qbw found")
    cc.add_argument("--open", action="store_true", help="Harvest whichever file is open in QB")
    cc.add_argument("--refresh", action="store_true", help="With --all: re-harvest even already-saved companies")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "profiles":
        return cmd_profiles(args)
    if args.cmd == "process-one":
        return cmd_process_one(args)
    if args.cmd == "review-one":
        return cmd_review_one(args)
    if args.cmd == "process-workbench":
        return cmd_process_workbench(args)
    if args.cmd == "build-testkit":
        return cmd_build_testkit(args)
    if args.cmd == "score-testkit":
        return cmd_score_testkit(args)
    if args.cmd == "harvest-accounts":
        if not args.company and not args.all and not args.open:
            print("Specify --company KEY (auth test), --open (active file), or --all (batch).")
            return 2
        return cmd_harvest_accounts(args)
    if args.cmd == "harvest-vendors":
        if not args.company and not args.all and not args.open:
            print("Specify --company KEY, --open (active file), or --all (batch).")
            return 2
        return cmd_harvest_vendors(args)
    if args.cmd == "harvest-classes":
        if not args.company and not args.all and not args.open:
            print("Specify --company KEY, --open (active file), or --all (batch).")
            return 2
        return cmd_harvest_classes(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
