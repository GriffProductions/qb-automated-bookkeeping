"""Per-company naming profiles — corpus-derived context for the LLM.

Hard rules can't cover 27 companies' quirks, so Python's job here is *not* to
decide names.  It observes them: this module walks the filename corpus (fast —
no PDF bytes are read) and distills, per company, the patterns the user
actually follows: tag variants, head-phrase vocabulary, vendor roster, unit
tags, pay-method accounts, amount/date habits, and representative examples.

The resulting ``data/company_profiles.json`` is injected into the Gemini
prompt by :mod:`llm_extractor` alongside 3–5 few-shot examples.  The LLM
reasons from that context; :mod:`convention.verify_conformance` only gates
the output (accept / send to human review), never generates it.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from qb_automation.config import settings
from qb_automation.config.company_registry import Division, match_folder_to_company
from qb_automation.services.convention import parse_filename

log = logging.getLogger(__name__)


@dataclass
class CompanyProfile:
    company_key: str
    company_name: str
    file_count: int = 0
    tags_seen: dict[str, int] = field(default_factory=dict)
    doc_phrases: dict[str, int] = field(default_factory=dict)
    vendors_seen: dict[str, int] = field(default_factory=dict)
    units_seen: dict[str, int] = field(default_factory=dict)
    pay_methods: dict[str, int] = field(default_factory=dict)
    with_amount_rate: float = 0.0
    month_only_rate: float = 0.0
    sample_names: list[str] = field(default_factory=list)


def collect_filename_corpus() -> list[tuple[str, str, str]]:
    """Walk both roots (incl. workbenches) → ``(company_key, file_name, folder)``.

    Company is resolved from the file's own directory first, then ancestors.
    Filename-only: no PDF is opened, so this runs in seconds.
    """
    corpus: list[tuple[str, str, str]] = []
    for root in (settings.REAL_ESTATE_ROOT, settings.DIALYSIS_ROOT):
        if not root.exists():
            continue
        for pdf in sorted(root.rglob("*.pdf")):
            company = None
            for ancestor in (pdf.parent, *pdf.parent.parents):
                if ancestor == root:
                    break
                company = match_folder_to_company(ancestor.name)
                if company is not None:
                    break
            if company is None or company.division == Division.UNASSIGNED:
                continue
            corpus.append((company.key, pdf.name, str(pdf.parent)))
    return corpus


def build_profiles(
    corpus: list[tuple[str, str, str]] | None = None,
    samples_per_company: int = 8,
) -> dict[str, CompanyProfile]:
    """Aggregate parsed filenames into one profile per company."""
    from qb_automation.config.company_registry import COMPANIES

    corpus = corpus if corpus is not None else collect_filename_corpus()
    by_company: dict[str, list[str]] = {}
    for key, name, _folder in corpus:
        by_company.setdefault(key, []).append(name)

    display = {c.key: c.display_name for c in COMPANIES}
    profiles: dict[str, CompanyProfile] = {}
    for key, names in by_company.items():
        tags: Counter[str] = Counter()
        phrases: Counter[str] = Counter()
        vendors: Counter[str] = Counter()
        units: Counter[str] = Counter()
        paymethods: Counter[str] = Counter()
        with_amount = month_only = 0
        for name in names:
            p = parse_filename(name)
            if p.tag:
                tags[p.tag] += 1
            if p.doc_phrase:
                phrases[p.doc_phrase] += 1
            if p.vendor:
                vendors[p.vendor] += 1
            if p.unit:
                units[p.unit] += 1
            if p.pay_method:
                paymethods[p.pay_method] += 1
            if p.amount is not None:
                with_amount += 1
            if p.month_only:
                month_only += 1
        n = len(names)
        # Representative samples: longest names (richest structure) first, deduped.
        samples = sorted(set(names), key=len, reverse=True)[:samples_per_company]
        profiles[key] = CompanyProfile(
            company_key=key,
            company_name=display.get(key, key),
            file_count=n,
            tags_seen=dict(tags.most_common(6)),
            doc_phrases=dict(phrases.most_common(12)),
            vendors_seen=dict(vendors.most_common(15)),
            units_seen=dict(units.most_common(10)),
            pay_methods=dict(paymethods.most_common(8)),
            with_amount_rate=round(with_amount / n, 2) if n else 0.0,
            month_only_rate=round(month_only / n, 2) if n else 0.0,
            sample_names=samples,
        )
    return profiles


def save_profiles(profiles: dict[str, CompanyProfile], path: Path | None = None) -> Path:
    path = path or (settings.DATA_DIR / "company_profiles.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in profiles.items()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d company profiles to %s", len(payload), path)
    return path


def load_profiles(path: Path | None = None) -> dict[str, CompanyProfile]:
    path = path or (settings.DATA_DIR / "company_profiles.json")
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: CompanyProfile(**v) for k, v in raw.items()}


def _top(d: dict[str, int], n: int = 8) -> str:
    return ", ".join(f"{k} ({v})" for k, v in list(d.items())[:n]) or "—"


def format_profile_for_prompt(p: CompanyProfile) -> str:
    """Compact profile block for the Gemini prompt — context, not rules."""
    return (
        f"Company: {p.company_name} (key {p.company_key}), {p.file_count} historical files.\n"
        f"Tags used: {_top(p.tags_seen, 4)}. "
        f"Dates: {'sometimes YYYY-MM for statements' if p.month_only_rate > 0.05 else 'always YYYY-MM-DD'}. "
        f"Amount present in ~{int(p.with_amount_rate * 100)}% of names.\n"
        f"Typical head phrases: {_top(p.doc_phrases)}.\n"
        f"Vendors seen: {_top(p.vendors_seen, 12)}.\n"
        f"Units/classes seen: {_top(p.units_seen, 8)}. "
        f"Pay-method accounts: {_top(p.pay_methods, 6)}.\n"
        f"Representative names:\n" + "\n".join(f"- {s}" for s in p.sample_names)
    )
