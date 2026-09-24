"""
Filtering and relevance scoring for UAE Job Radar.

Titles and keyword weights live in config.yaml (plain word lists, no regex needed).
The red/green-flag patterns below are regexes; edit them here if you need to.
"""
from __future__ import annotations

import re

UAE_RE = re.compile(
    r"(?<![a-z])(u\.?a\.?e\.?|united arab emirates|dubai|abu dhabi|sharjah|ajman|"
    r"ras al[\s-]?khaimah|fujairah|umm al[\s-]?quwain|al ain|difc|adgm|masdar)(?![a-z])",
    re.I,
)
REGIONAL_RE = re.compile(r"(?<![a-z])(middle east|mena|gcc|gulf)(?![a-z])", re.I)
CITY_RE = re.compile(
    r"(?<![a-z])(dubai|abu dhabi|sharjah|ajman|al ain|ras al[\s-]?khaimah|fujairah)(?![a-z])", re.I
)
AMBIGUOUS_LOCATION_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)   # Workday's "3 Locations"
UAE_COUNTRY_CODES = {"ae", "are"}

SENIOR_HIGH = re.compile(r"(?<![a-z])(staff|principal|distinguished|architect)(?![a-z])", re.I)
LEAD = re.compile(r"(?<![a-z])lead(?![a-z])", re.I)
JUNIOR = re.compile(r"(?<![a-z])(junior|jr)(?![a-z])", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?![a-z])(?=(.{0,50}))", re.I | re.S)
YEARS_CONTEXT = re.compile(r"^\W*(of|in|professional|relevant|hands|industry|experience|exp|working|commercial)", re.I)

# (flag shown in alerts, score change, pattern searched in title + description)
FLAG_RULES = [
    ("no visa/relocation", -10, re.compile(
        r"no (visa|relocation)|relocation (is )?not (provided|offered|available)|"
        r"not (able to )?(offer|provide|sponsor) (a )?(visa|relocation)|without (visa|relocation) support", re.I)),
    ("visa/relocation", 10, re.compile(
        r"relocat|visa (sponsor|support|provided|assistance|included|will be provided)|sponsorship|"
        r"flights? (provided|paid|covered)|accommodation (provided|support|allowance)", re.I)),
    ("UAE residents only", -30, re.compile(
        r"(uae|dubai|abu dhabi)[- ](based )?(residents?|candidates?) only|only (uae|dubai|abu dhabi)[- ]based|"
        r"must (already |currently )?(be )?(based|residing|living|located|resident) in (the )?(uae|dubai|abu dhabi)|"
        r"currently (based|residing|living|located) in (the )?(uae|dubai|abu dhabi)|local candidates only|"
        r"in[- ]country (candidates|applicants)", re.I)),
    ("immediate joiner", -15, re.compile(
        r"immediate(ly)? (joiner|joining|start|availab)|join(ing)? immediately|"
        r"notice period of (less than |up to |maximum |max )?(30 days|one month|1 month|2 weeks)", re.I)),
    ("Arabic required", -15, re.compile(
        r"arabic (language )?(is )?(required|mandatory|essential|a must)|"
        r"fluen(t|cy) in (both )?(english and )?arabic|native arabic|arabic[- ]speak", re.I)),
    ("Emirati-only", -60, re.compile(
        r"(uae|emirati) nationals? only|only (uae|emirati) nationals|for (uae|emirati) nationals|"
        r"emiratis? only|emiratisation|emiratization|nafis", re.I)),
]


def words_regex(words):
    """['full stack', 'next.js'] -> one case-insensitive whole-word regex.
    Spaces and hyphens are interchangeable/optional: 'full stack' matches 'full-stack' and 'fullstack'."""
    parts = []
    for word in words or []:
        word = str(word).strip().lower()
        if not word:
            continue
        escaped = re.escape(word).replace(r"\ ", r"[\s\-]?").replace(r"\-", r"[\s\-]?")
        parts.append(escaped)
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


def location_type(job, settings) -> str | None:
    """'uae', 'regional' (Remote - Middle East/MENA/GCC) or None."""
    if (job.country_code or "").lower() in UAE_COUNTRY_CODES or UAE_RE.search(job.location or ""):
        return "uae"
    if (settings.get("location") or {}).get("include_regional_remote", True) and REGIONAL_RE.search(job.location or ""):
        return "regional"
    return None


def is_ambiguous_location(job) -> bool:
    return bool(AMBIGUOUS_LOCATION_RE.match(job.location or ""))


def required_years(text: str):
    """Best guess at the headline 'X+ years of experience' requirement."""
    values = []
    for m in YEARS.finditer(text or ""):
        n = int(m.group(1))
        tail = re.split(r"[.;\n•]", m.group(2))[0]       # stay inside the same sentence
        if 1 <= n <= 20 and (YEARS_CONTEXT.match(tail) or "experience" in tail.lower()):
            values.append(n)
    return max(values) if values else None


class Scorer:
    def __init__(self, settings: dict):
        titles = settings.get("titles") or {}
        self.categories = []
        for name, spec in (titles.get("categories") or {}).items():
            rx = words_regex(spec.get("words"))
            if rx:
                self.categories.append((name, int(spec.get("points", 30)), rx))
        self.exclude = words_regex(titles.get("exclude"))
        keywords = settings.get("keywords") or {}
        self.boosts = [(words_regex([k]), int(v)) for k, v in (keywords.get("boosts") or {}).items()]
        self.boost_cap = int(keywords.get("cap", 25))
        self.your_years = float((settings.get("seniority") or {}).get("your_years", 5))

    def title_match(self, title: str):
        """(category, base points) for a wanted title, else None."""
        if not title or (self.exclude and self.exclude.search(title)):
            return None
        best = None
        for name, points, rx in self.categories:
            if rx.search(title) and (best is None or points > best[1]):
                best = (name, points)
        return best

    def score(self, job, category, loc_type):
        score = category[1]
        flags = []
        description = job.description or ""
        text = f"{job.title}\n{description}"

        score += min(sum(points for rx, points in self.boosts if rx and rx.search(description)), self.boost_cap)
        score += 15 if loc_type == "uae" else 5 if loc_type == "regional" else 0

        if SENIOR_HIGH.search(job.title):
            score -= 15
        elif LEAD.search(job.title):
            score -= 5
        if JUNIOR.search(job.title):
            score -= 10

        years = required_years(description)
        if years is not None:
            if years >= self.your_years + 3:
                score -= 20
                flags.append(f"{years}+ yrs asked")
            elif years >= self.your_years + 1.5:
                score -= 8
                flags.append(f"{years}+ yrs asked")
            elif years <= self.your_years + 0.5:
                score += 5

        no_relocation = False
        for name, points, rx in FLAG_RULES:
            if name == "visa/relocation" and no_relocation:
                continue
            if rx.search(text):
                score += points
                flags.append(name)
                if name == "no visa/relocation":
                    no_relocation = True

        hints = getattr(job, "hints", None) or {}
        if hints.get("visa") and not no_relocation and "visa/relocation" not in flags:
            score += 10
            flags.append("visa/relocation")     # aggregator's own reading of the description
        if job.unlisted:
            flags.append("unlisted role")
        if loc_type == "regional":
            flags.append("regional/remote")
        return max(0, min(100, score)), flags


_COMPANY_NOISE = re.compile(
    r"(?<![a-z])(llc|l\.l\.c|fz[\s-]?llc|fze|fzco|dmcc|inc|ltd|limited|group|technologies|technology|middle east|mena)(?![a-z])",
    re.I,
)


def fingerprint(job) -> str:
    """company|title|city - catches the same role posted twice (e.g. two boards, reposts)."""
    company = re.sub(r"[^a-z0-9]", "", _COMPANY_NOISE.sub(" ", job.company.lower()))
    title = re.sub(r"(?<![a-z])sr\.?(?=\s)", "senior", job.title.lower())
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    m = CITY_RE.search(job.location or "")
    city = m.group(1).lower().replace(" ", "") if m else "uae"
    return f"{company}|{title}|{city}"
