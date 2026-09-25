"""Registry of company entities mapped to their storage divisions.

Discovery (2026-09-24, live filesystem scan):
  - Real Estate root: 18 property subfolders (+ workbench + backups)
  - Dialysis root:      9 entity subfolders (+ workbench + backups)
  - Total discovered:  27 operating entities.

The brief specifies 28.  The registry below contains the 27 discovered
entities verbatim plus one extensible ``UNASSIGNED`` placeholder so downstream
code, counts, and UIs built around "28" keep working.  Replace the placeholder
with the real 28th entity when confirmed (likely an additional ARC facility
or a new acquisition) — no other code changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Division(str, Enum):
    REAL_ESTATE = "real_estate"
    DIALYSIS = "dialysis"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class Company:
    key: str            # stable machine key, e.g. "victoria_120"
    display_name: str   # human name, e.g. "Valencia - Victoria 120"
    division: Division
    folder_fragment: str  # substring matched against the on-disk folder name
    tag: str            # short filename tag, e.g. "Victoria 120"


REAL_ESTATE_COMPANIES: list[Company] = [
    Company("brea_granada_3037", "Brea - Granada Circle 3037", Division.REAL_ESTATE, "Granada Circle 3037", "Granada Circle"),
    Company("vegas_ivypoint_1920", "Las Vegas - Ivy Point Lane 1920", Division.REAL_ESTATE, "Ivy Point Lane 1920", "Ivy Point"),
    Company("vegas_lenox_9837", "Las Vegas - Lenox Crest Place 9837", Division.REAL_ESTATE, "Lenox Crest Place 9837", "Lenox Crest"),
    Company("vegas_pompei_10341", "Las Vegas - Pompei Place 10341", Division.REAL_ESTATE, "Pompei Place 10341", "Pompei Place"),
    Company("vegas_singingwind_10240", "Las Vegas - Singing Wind Place 10240", Division.REAL_ESTATE, "Singing Wind Place 10240", "Singing Wind"),
    Company("vegas_sondrio_1920", "Las Vegas - Sondrio Drive 1920", Division.REAL_ESTATE, "Sondrio Drive 1920", "Sondrio Drive"),
    Company("sandiego_lebon_217", "San Diego - Lebon Drive 217", Division.REAL_ESTATE, "Lebon Drive 217", "Lebon Drive"),
    Company("valencia_paz_28754", "Valencia - Calle De La Paz Drive 28754", Division.REAL_ESTATE, "Calle De La Paz", "Calle De La Paz"),
    Company("valencia_dm_200_201", "Valencia - Del Monte 200 & 201", Division.REAL_ESTATE, "Del Monte 200", "Del Monte 200201"),
    Company("valencia_dm_239_10", "Valencia - Del Monte 239 & 10", Division.REAL_ESTATE, "Del Monte 239", "Del Monte 23910"),
    Company("valencia_dm_285_351", "Valencia - Del Monte 285 & 351", Division.REAL_ESTATE, "Del Monte 285", "Del Monte 285351"),
    Company("valencia_dm_59_62", "Valencia - Del Monte 59 & 62", Division.REAL_ESTATE, "Del Monte 59", "Del Monte 5962"),
    Company("valencia_masters_24655", "Valencia - Masters Cup Way 24655", Division.REAL_ESTATE, "Masters Cup Way 24655", "Masters Cup Way"),
    Company("valencia_northbrooke_23347", "Valencia - Northbrooke Lane 23347", Division.REAL_ESTATE, "Northbrooke Lane 23347", "Northbrooke"),
    Company("valencia_seco_127", "Valencia - Seco Canyon Road 127", Division.REAL_ESTATE, "Seco Canyon Road 127", "Seco"),
    Company("valencia_tiburon_24701", "Valencia - Tiburon Street 24701", Division.REAL_ESTATE, "Tiburon Street 24701", "Tiburon"),
    Company("valencia_victoria_120", "Valencia - Victoria 120", Division.REAL_ESTATE, "Victoria 120", "Victoria 120"),
    Company("valencia_victoria_63_96", "Valencia - Victoria 63 & 96", Division.REAL_ESTATE, "Victoria 63", "Victoria 6396"),
]

DIALYSIS_COMPANIES: list[Company] = [
    Company("arc", "ARC - American Renal Care", Division.DIALYSIS, "ARC", "ARC"),
    Company("hip", "HIP - Healthcare Investment Properties", Division.DIALYSIS, "HIP", "HIP"),
    Company("hms", "HMS - Healthcare Management Services", Division.DIALYSIS, "HMS", "HMS"),
    Company("rig_mhd", "RIG MHD - Renal Investment Group Mission Hills Dialysis", Division.DIALYSIS, "RIG MHD", "RIG MHD"),
    Company("rig", "RIG - Renal Investment Group", Division.DIALYSIS, "RIG", "RIG"),
    Company("rms", "RMS - Renal Management Services", Division.DIALYSIS, "RMS", "RMS"),
    Company("rt", "RT - Renal Therapeutics", Division.DIALYSIS, "RT", "RT"),
    Company("sip", "SIP - Sylmar Investment Properties", Division.DIALYSIS, "SIP", "SIP"),
    Company("spd", "SPD - Sylmar Pacoima Dialysis", Division.DIALYSIS, "SPD", "SPD"),
]

# Placeholder keeping the registry at the specified size of 28 until the
# 28th operating entity is confirmed.  Not matched to any folder.
PLACEHOLDER_COMPANY = Company(
    "tbd_28", "TBD - Unassigned (28th entity placeholder)",
    Division.UNASSIGNED, "__NO_SUCH_FOLDER__", "TBD",
)

COMPANIES: list[Company] = [
    *REAL_ESTATE_COMPANIES, *DIALYSIS_COMPANIES, PLACEHOLDER_COMPANY,
]

assert len(COMPANIES) == 28, f"registry must hold 28 entries, has {len(COMPANIES)}"

_BY_KEY = {c.key: c for c in COMPANIES}
_BY_TAG_LOWER = {c.tag.lower(): c for c in COMPANIES}


def get_company(key: str) -> Company | None:
    return _BY_KEY.get(key)


# Naming rule: abbreviations (HIP, ARC, …) live ONLY in the trailing company
# tag.  Anywhere else — vendor slot, associated-company mentions, memos — the
# facility name is spelled out in full.  Keys are lowercase match tokens.
FACILITY_LONG_NAMES: dict[str, str] = {
    "arc": "American Renal Care",
    "american renal care": "American Renal Care",
    "hip": "Healthcare Investment Properties",
    "healthcare investment properties": "Healthcare Investment Properties",
    "hms": "Healthcare Management Services",
    "healthcare management services": "Healthcare Management Services",
    "rig mhd": "Renal Investment Group Mission Hills Dialysis",
    "mhd": "Mission Hills Dialysis",
    "mission hills dialysis": "Mission Hills Dialysis",
    "rig": "Renal Investment Group",
    "renal investment group": "Renal Investment Group",
    "rms": "Renal Management Services",
    "renal management services": "Renal Management Services",
    "rt": "Renal Therapeutics",
    "renal therapeutics": "Renal Therapeutics",
    "sip": "Sylmar Investment Properties",
    "sylmar investment properties": "Sylmar Investment Properties",
    "spd": "Sylmar Pacoima Dialysis",
    "sylmar pacoima dialysis": "Sylmar Pacoima Dialysis",
    "lcd": "Laurel Canyon Dialysis",
    "laurel canyon dialysis": "Laurel Canyon Dialysis",
    "nkc": "Northridge Kidney Center",
    "northridge kidney center": "Northridge Kidney Center",
    "north ridge kidney center": "Northridge Kidney Center",
    "scd": "Santa Clarita Dialysis",
    "santa clarita dialysis": "Santa Clarita Dialysis",
}


def _norm(s: str) -> str:
    """Casefold + fold dash variants so ' — '/' – '/' - ' all match."""
    from qb_automation.services.naming import normalize_separators

    return normalize_separators(s).casefold()


def match_folder_to_company(folder_name: str) -> Company | None:
    """Match an on-disk subfolder name to a Company (fragment match)."""
    lowered = _norm(folder_name)
    # Longest fragment first so "RIG MHD" wins over "RIG", "Victoria 63" over "Victoria".
    for c in sorted(COMPANIES, key=lambda c: -len(c.folder_fragment)):
        if _norm(c.folder_fragment) in lowered:
            return c
    return None


def match_filename_to_company(filename: str) -> Company | None:
    """Match a filename to a Company, tolerant of uploader shorthand.

    Tiers (first hit wins, longest-first within tier):
    1. Tag with word boundaries (fixes "rt" matching inside "moRTgage").
    2. Spaceless containment for long tags ("CalleDeLaPaz" → Calle De La Paz).
    3. Curated aliases (mhd, sip, pompei…) anchored at string start or a
       non-letter boundary.
    """
    import re as _re

    lowered = _norm(filename)
    spaceless = _re.sub(r"[\s_\-]+", "", lowered)

    def _boundary_hit(needle: str, haystack: str) -> bool:
        return _re.search(r"(?<!\w)" + _re.escape(needle) + r"(?!\w)", haystack) is not None

    # Tier 1: boundary-aware tag match
    for c in sorted(COMPANIES, key=lambda c: -len(c.tag)):
        if _boundary_hit(_norm(c.tag), lowered):
            return c
    # Tier 2: spaceless match, long tags only (short ones false-positive)
    for c in sorted(COMPANIES, key=lambda c: -len(c.tag)):
        flat = _re.sub(r"[\s_\-]+", "", _norm(c.tag))
        if len(flat) >= 6 and flat in spaceless:
            return c
    # Tier 3: aliases
    for alias, key in sorted(COMPANY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        for m in _re.finditer(_re.escape(alias), spaceless):
            start = m.start()
            if start == 0 or not spaceless[start - 1].isalpha():
                company = _BY_KEY.get(key)
                if company is not None:
                    return company
    return None


# Short-form aliases seen in uploader filenames / email subjects.
# Values are Company keys from the registry above.
COMPANY_ALIASES: dict[str, str] = {
    "rigmhd": "rig_mhd",
    "lenoxcrest": "vegas_lenox_9837",
    "singingwind": "vegas_singingwind_10240",
    "pompei": "vegas_pompei_10341",
    "ivypoint": "vegas_ivypoint_1920",
    "sondrio": "vegas_sondrio_1920",
    "lebon": "sandiego_lebon_217",
    "masterscupway": "valencia_masters_24655",
    "masters": "valencia_masters_24655",
    "northbrooke": "valencia_northbrooke_23347",
    "tiburon": "valencia_tiburon_24701",
    "seco": "valencia_seco_127",
    "granada": "brea_granada_3037",
    "callede": "valencia_paz_28754",
    "delmonte200201": "valencia_dm_200_201",
    "delmonte23910": "valencia_dm_239_10",
    "delmonte285351": "valencia_dm_285_351",
    "delmonte5962": "valencia_dm_59_62",
    "victoria6396": "valencia_victoria_63_96",
    "victoria120": "valencia_victoria_120",
    "mhd": "rig_mhd",
    "lcd": "arc",
    "nkc": "arc",
    "scd": "arc",
    "arc": "arc",
    "hip": "hip",
    "hms": "hms",
    "rig": "rig",
    "rms": "rms",
    "sip": "sip",
    "spd": "spd",
    "laurel": "arc",
    "northridge": "arc",
    "santaclarita": "arc",
    "missionhills": "rig_mhd",
}


# Live QuickBooks folder names (OneDrive "Company Files — QB" tree) → key.
# These are shorter/LLC-suffixed variants the fragment matcher misses
# ("Pompei Place" vs fragment "Pompei Place 10341"), matched with boundaries.
QB_FOLDER_ALIASES: dict[str, str] = {
    "valencia paz": "valencia_paz_28754",
    "brea granada": "brea_granada_3037",
    "ivy point": "vegas_ivypoint_1920",
    "lenox crest": "vegas_lenox_9837",
    "pompei place": "vegas_pompei_10341",
    "singing wind": "vegas_singingwind_10240",
    "sondrio drive": "vegas_sondrio_1920",
    "san diego lebon": "sandiego_lebon_217",
    "valencia masters": "valencia_masters_24655",
    "valencia northbrooke": "valencia_northbrooke_23347",
    "valencia seco": "valencia_seco_127",
    "valencia tiburon": "valencia_tiburon_24701",
    "del monte 5962": "valencia_dm_59_62",
    "american renal care": "arc",
    "healthcare investment properties": "hip",
    "healthcare management services": "hms",
    "renal investment group mhd": "rig_mhd",
    "mission hills": "rig_mhd",
    "renal investment group": "rig",
    "renal management services": "rms",
    "renal therapeutics": "rt",
    "sylmar investment properties": "sip",
    "sylmar pacoima dialysis": "spd",
}


def match_qb_folder_to_company(folder_name: str) -> Company | None:
    """Match a live-QB company folder (falls back to fragment matching)."""
    import re as _re

    lowered = _norm(folder_name)
    for alias, key in sorted(QB_FOLDER_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if _re.search(r"(?<!\w)" + _re.escape(alias) + r"(?!\w)", lowered):
            company = _BY_KEY.get(key)
            if company is not None:
                return company
    return match_folder_to_company(folder_name)
