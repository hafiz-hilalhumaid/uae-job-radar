"""
ATS adapters for UAE Job Radar.

Each adapter takes one company entry from companies.yaml plus a shared Context
and returns a list of Job objects. Only public, unauthenticated endpoints that
employers' own careers pages use are called, politely (one request at a time,
with a pause between requests).
"""
from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import requests


class NotFound(Exception):
    """The board does not exist on this host (HTTP 404 or unreachable host)."""


@dataclass
class Job:
    source: str
    company: str
    native_id: str
    title: str
    location: str
    url: str
    posted_at: str = ""
    description: str = ""
    country_code: str = ""
    workplace: str = ""
    salary: str = ""
    unlisted: bool = False
    hints: dict = field(default_factory=dict)   # extra signals from aggregators (e.g. visa)


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_html(text) -> str:
    """HTML (sometimes double-escaped, as Greenhouse does) -> plain text."""
    if not text:
        return ""
    text = html.unescape(html.unescape(str(text)))
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


class Http:
    """Small polite HTTP client: retries 429/5xx with backoff, raises NotFound on 404.
    Thread-safe: every worker thread gets its own requests.Session."""

    def __init__(self, user_agent: str, timeout: int = 25, pause: float = 0.35):
        self.headers = {"User-Agent": user_agent, "Accept": "application/json, application/xml, text/xml, */*"}
        self.timeout = timeout
        self.pause = pause
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self.headers)
            self._local.session = s
        return s

    def request(self, method, url, *, params=None, json_body=None, headers=None, timeout=None, attempts=3):
        last_error = None
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=headers,
                                            timeout=timeout or self.timeout)
            except requests.ConnectionError as exc:
                # DNS failures / refused connections won't fix themselves by retrying.
                raise NotFound(f"{url}: {exc.__class__.__name__}") from exc
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"HTTP {resp.status_code} from {url}")
                time.sleep(3 * (attempt + 1) * (2 if attempts > 3 else 1))
                continue
            if resp.status_code >= 400:
                # Keep the service's own error text (e.g. Apify's reason) - it's what tells you what went wrong.
                raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}", response=resp)
            time.sleep(self.pause)
            return resp
        raise last_error or RuntimeError(f"request failed: {url}")

    def get_json(self, url, params=None, headers=None):
        return self.request("GET", url, params=params, headers=headers).json()

    def get_bytes(self, url, params=None):
        return self.request("GET", url, params=params).content

    def post_json(self, url, body, headers=None):
        return self.request("POST", url, json_body=body, headers=headers).json()


@dataclass
class Context:
    http: Http
    cache: dict      # persisted between runs (e.g. which regional host a board lives on)
    settings: dict   # parsed config.yaml


def _first_host_with_data(ctx: Context, cache_key: str, hosts: dict, order: list, fetch):
    """Try each regional host; return the first non-empty result and remember the host."""
    cached = ctx.cache.get(cache_key)
    if cached in hosts:
        order = [cached] + [h for h in order if h != cached]
    empty_result, errors = None, []
    for host in order:
        try:
            result = fetch(hosts[host])
        except (NotFound, requests.RequestException) as exc:
            errors.append(f"{host}: {exc}")
            continue
        if result:
            ctx.cache[cache_key] = host
            return result
        if empty_result is None:
            empty_result = result
    if empty_result is not None:
        return empty_result
    raise NotFound("; ".join(errors))


# --------------------------------------------------------------------------- Greenhouse
# US-region boards: documented JSON Job Board API. EU-region boards (job-boards.eu.greenhouse.io,
# used by several UAE companies) have no reachable public API host - the first live run showed
# boards-api.eu.greenhouse.io doesn't resolve and the US API 404s for them - so those are read
# from the public board page itself. Whichever works is remembered per board.
GREENHOUSE_API = "https://boards-api.greenhouse.io"
GREENHOUSE_EU_BOARD = "https://job-boards.eu.greenhouse.io"
_GH_LINK = re.compile(
    r'<a\b[^>]*?href="(?P<href>[^"]*?/jobs/(?P<id>\d{4,})[^"]*)"[^>]*>(?P<inner>.*?)</a>', re.S | re.I)
_GH_LOCATION = re.compile(r'class="[^"]*location[^"]*"[^>]*>(?P<loc>.*?)</', re.S | re.I)


def _greenhouse_api(c: dict, ctx: Context) -> list[Job]:
    data = ctx.http.get_json(f"{GREENHOUSE_API}/v1/boards/{c['id']}/jobs", params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        offices = [o.get("location") or o.get("name") or "" for o in (j.get("offices") or []) if o]
        location = ", ".join(x for x in [(j.get("location") or {}).get("name", "")] + offices if x)
        jobs.append(Job(
            source="greenhouse", company=j.get("company_name") or c["name"], native_id=str(j.get("id")),
            title=(j.get("title") or "").strip(), location=location,
            url=j.get("absolute_url") or "",
            posted_at=j.get("first_published") or j.get("updated_at") or "",
            description=clean_html(j.get("content")),
        ))
    return jobs


def _greenhouse_eu_page(c: dict, ctx: Context) -> list[Job]:
    """Read an EU-region board from its public HTML page (title + location per job link)."""
    token = c["id"]
    jobs, seen = [], set()
    for page in range(1, 11):                       # boards paginate; stop when a page adds nothing
        html_text = ctx.http.get_bytes(f"{GREENHOUSE_EU_BOARD}/{token}",
                                       params={"page": page} if page > 1 else None).decode("utf-8", "replace")
        added = 0
        for m in _GH_LINK.finditer(html_text):
            job_id = m.group("id")
            if job_id in seen:
                continue
            seen.add(job_id)
            added += 1
            parts = [p for p in (clean_html(x) for x in re.split(r"<[^>]+>", m.group("inner"))) if p]
            location = ", ".join(parts[1:])
            if not location:                        # older board layout: location sits next to the link
                near = _GH_LOCATION.search(html_text[m.end(): m.end() + 500])
                location = clean_html(near.group("loc")) if near else ""
            href = m.group("href")
            url = href if href.startswith("http") else f"{GREENHOUSE_EU_BOARD}{href if href.startswith('/') else '/' + href}"
            jobs.append(Job(source="greenhouse", company=c["name"], native_id=job_id,
                            title=parts[0] if parts else "", location=location, url=url))
        if not added:
            break
    return jobs


def greenhouse(c: dict, ctx: Context) -> list[Job]:
    token = c["id"]
    readers = {"us": _greenhouse_api, "eu_page": _greenhouse_eu_page}
    order = ["eu_page", "us"] if c.get("host") == "eu" else ["us", "eu_page"]
    cached = ctx.cache.get(f"greenhouse:{token}")
    if cached in readers:
        order = [cached] + [r for r in order if r != cached]
    errors, got_empty = [], False
    for name in order:
        try:
            jobs = readers[name](c, ctx)
        except (NotFound, requests.RequestException) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if jobs:
            ctx.cache[f"greenhouse:{token}"] = name
            return jobs
        got_empty = True
    if got_empty:
        return []                                  # board exists, just no openings right now
    raise NotFound("; ".join(errors))


def greenhouse_enrich(job: Job, c: dict, ctx: Context) -> None:
    """EU-page jobs arrive without a description: read the job page for scoring and location."""
    if not job.url:
        return
    page = ctx.http.get_bytes(job.url).decode("utf-8", "replace")
    if not job.location:
        near = _GH_LOCATION.search(page)
        if near:
            job.location = clean_html(near.group("loc"))
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    job.description = clean_html(body)[:20000]


# --------------------------------------------------------------------------- Lever
LEVER_HOSTS = {"global": "https://api.lever.co", "eu": "https://api.eu.lever.co"}


def lever(c: dict, ctx: Context) -> list[Job]:
    slug = c["id"]
    order = ["eu", "global"] if c.get("host") == "eu" else ["global", "eu"]

    def fetch(base):
        postings, skip = [], 0
        while True:
            page = ctx.http.get_json(
                f"{base}/v0/postings/{slug}", params={"mode": "json", "limit": 100, "skip": skip}
            )
            if not isinstance(page, list):
                break
            postings.extend(page)
            if len(page) < 100 or skip >= 3000:
                break
            skip += 100
        return postings

    jobs = []
    for p in _first_host_with_data(ctx, f"lever:{slug}", LEVER_HOSTS, order, fetch):
        cat = p.get("categories") or {}
        locations = cat.get("allLocations") or [cat.get("location")]
        lists_text = " ".join(
            f"{item.get('text', '')} {clean_html(item.get('content'))}" for item in (p.get("lists") or [])
        )
        created = p.get("createdAt")
        posted = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
            if isinstance(created, (int, float)) else ""
        )
        sal = p.get("salaryRange") or {}
        salary = ""
        if sal.get("min") or sal.get("max"):
            salary = f"{sal.get('currency', '')} {sal.get('min', '')}-{sal.get('max', '')} {sal.get('interval', '')}".strip()
        jobs.append(Job(
            source="lever", company=c["name"], native_id=str(p.get("id")),
            title=(p.get("text") or "").strip(),
            location=", ".join(x for x in locations if x),
            url=p.get("hostedUrl") or "", posted_at=posted,
            description=" ".join([p.get("descriptionPlain") or "", lists_text, p.get("additionalPlain") or ""]).strip(),
            country_code=(p.get("country") or "").lower(),
            workplace=p.get("workplaceType") or "", salary=salary,
        ))
    return jobs


# --------------------------------------------------------------------------- Ashby
def ashby(c: dict, ctx: Context) -> list[Job]:
    data = ctx.http.get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{c['id']}", params={"includeCompensation": "true"}
    )
    jobs = []
    for j in data.get("jobs", []):
        parts = [j.get("location") or ""]
        for sec in j.get("secondaryLocations") or []:
            parts.append(sec.get("location") or "")
            sec_country = ((sec.get("address") or {}).get("addressCountry")) or ""
            parts.append(sec_country)
        address = (j.get("address") or {}).get("postalAddress") or {}
        parts.append(address.get("addressCountry") or "")
        comp = j.get("compensation") or {}
        jobs.append(Job(
            source="ashby", company=c["name"],
            native_id=str(j.get("id") or j.get("jobUrl") or j.get("title")),
            title=(j.get("title") or "").strip(),
            location=", ".join(dict.fromkeys(p for p in parts if p)),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            posted_at=j.get("publishedAt") or "",
            description=j.get("descriptionPlain") or clean_html(j.get("descriptionHtml")),
            workplace=j.get("workplaceType") or ("Remote" if j.get("isRemote") else ""),
            salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or "",
            unlisted=j.get("isListed") is False,
        ))
    return jobs


# --------------------------------------------------------------------------- SmartRecruiters
def smartrecruiters(c: dict, ctx: Context) -> list[Job]:
    cid = c["id"]
    params = {"limit": 100, "offset": 0}
    country = c.get("country", "ae")
    if country:
        params["country"] = country
    jobs = []
    while True:
        data = ctx.http.get_json(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings", params=dict(params))
        content = data.get("content") or []
        for p in content:
            loc = p.get("location") or {}
            full = loc.get("fullLocation") or ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            jobs.append(Job(
                source="smartrecruiters", company=c["name"], native_id=str(p.get("id")),
                title=(p.get("name") or "").strip(), location=full,
                url=f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}",
                posted_at=p.get("releasedDate") or "",
                country_code=(loc.get("country") or "").lower(),
                workplace="Remote" if loc.get("remote") else "",
            ))
        params["offset"] += len(content)
        if not content or params["offset"] >= int(data.get("totalFound") or 0) or params["offset"] >= 2000:
            break
    return jobs


def smartrecruiters_enrich(job: Job, c: dict, ctx: Context) -> None:
    d = ctx.http.get_json(f"https://api.smartrecruiters.com/v1/companies/{c['id']}/postings/{job.native_id}")
    sections = (d.get("jobAd") or {}).get("sections") or {}
    job.description = " ".join(
        clean_html((sections.get(k) or {}).get("text"))
        for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription")
    ).strip()
    if d.get("postingUrl"):
        job.url = d["postingUrl"]


# --------------------------------------------------------------------------- Workable
def workable(c: dict, ctx: Context) -> list[Job]:
    account = c["id"]
    data = ctx.http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{account}", params={"details": "true"})
    jobs = []
    for j in data.get("jobs", []):
        locs = [
            ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            for loc in (j.get("locations") or [])
        ]
        if not any(locs):
            locs = [", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)]
        code = j.get("shortcode") or j.get("code") or j.get("id") or j.get("title")
        jobs.append(Job(
            source="workable", company=c["name"], native_id=str(code),
            title=(j.get("title") or "").strip(),
            location="; ".join(x for x in locs if x),
            url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{account}/j/{code}/",
            posted_at=j.get("published_on") or j.get("created_at") or "",
            description=clean_html(j.get("description")),
            country_code=(j.get("countryCode") or "").lower(),
            workplace="Remote" if j.get("telecommuting") else "",
        ))
    return jobs


# --------------------------------------------------------------------------- Recruitee
def recruitee(c: dict, ctx: Context) -> list[Job]:
    data = ctx.http.get_json(f"https://{c['id']}.recruitee.com/api/offers/")
    jobs = []
    for o in data.get("offers", []):
        location = o.get("location") or ", ".join(x for x in [o.get("city"), o.get("country")] if x)
        jobs.append(Job(
            source="recruitee", company=c["name"], native_id=str(o.get("id")),
            title=(o.get("title") or "").strip(), location=location,
            url=o.get("careers_url") or "",
            posted_at=o.get("published_at") or o.get("created_at") or "",
            description=clean_html(f"{o.get('description') or ''} {o.get('requirements') or ''}"),
            country_code=(o.get("country_code") or "").lower(),
            workplace="Remote" if o.get("remote") else "",
        ))
    return jobs


# --------------------------------------------------------------------------- Teamtailor (RSS)
def teamtailor(c: dict, ctx: Context) -> list[Job]:
    base = str(c["id"]).rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}.teamtailor.com"
    root = ET.fromstring(ctx.http.get_bytes(f"{base}/jobs.rss"))
    jobs = []
    for item in root.iter("item"):
        def text(tag, _item=item):
            el = _item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        locs = []
        for el in item.iter():
            if el.tag.split("}")[-1].lower() in ("locations", "location"):
                for sub in el.iter():
                    value = (sub.text or "").strip()
                    if value and sub.tag.split("}")[-1].lower() in ("city", "country", "name", "location", "address"):
                        locs.append(value)
        link = text("link")
        jobs.append(Job(
            source="teamtailor", company=c["name"], native_id=text("guid") or link,
            title=text("title"),
            location=", ".join(dict.fromkeys(locs)) or c.get("default_location", ""),
            url=link, posted_at=text("pubDate"), description=clean_html(text("description")),
        ))
    return jobs


# --------------------------------------------------------------------------- Workday (undocumented CXS API)
def workday(c: dict, ctx: Context) -> list[Job]:
    tenant, shard, site = c["tenant"], c["shard"], c["site"]
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Accept-Language": "en-US"}
    keywords = c.get("keywords") or ctx.settings.get("workday_keywords") or ["software"]
    found: dict[str, Job] = {}
    for kw in keywords:
        for offset in range(0, 100, 20):  # Workday returns nothing if limit > 20
            data = ctx.http.post_json(
                api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": kw}, headers=headers
            )
            posts = data.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                if not path or path in found:
                    continue
                found[path] = Job(
                    source="workday", company=c["name"], native_id=path,
                    title=(p.get("title") or "").strip(),
                    location=p.get("locationsText") or "",
                    url=f"{base}/{site}{path}",
                    posted_at=p.get("postedOn") or "",   # e.g. "Posted Today" - a label, not a date
                )
            if len(posts) < 20:
                break
    return list(found.values())


def workday_enrich(job: Job, c: dict, ctx: Context) -> None:
    base = f"https://{c['tenant']}.{c['shard']}.myworkdayjobs.com"
    d = ctx.http.get_json(
        f"{base}/wday/cxs/{c['tenant']}/{c['site']}{job.native_id}", headers={"Accept-Language": "en-US"}
    )
    info = d.get("jobPostingInfo") or {}
    job.description = clean_html(info.get("jobDescription"))
    locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    if any(locs):
        job.location = ", ".join(x for x in locs if x)
    if info.get("startDate"):
        job.posted_at = info["startDate"]
    if info.get("externalUrl"):
        job.url = info["externalUrl"]


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "recruitee": recruitee,
    "teamtailor": teamtailor,
    "workday": workday,
}

# Called only for new, title-matching jobs whose list response lacks a description or a clear location.
ENRICHERS = {
    "greenhouse": greenhouse_enrich,          # only used for EU-page jobs (API jobs have descriptions)
    "smartrecruiters": smartrecruiters_enrich,
    "workday": workday_enrich,
}


def board_url(c: dict, host: str | None = None) -> str:
    """Human-readable board URL, for checking a discovered board by eye."""
    ats, ident = c.get("ats"), c.get("id", "")
    if ats == "greenhouse":
        return f"https://job-boards.{'eu.' if host == 'eu' else ''}greenhouse.io/{ident}"
    if ats == "lever":
        return f"https://jobs.{'eu.' if host == 'eu' else ''}lever.co/{ident}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{ident}"
    if ats == "workable":
        return f"https://apply.workable.com/{ident}/"
    if ats == "recruitee":
        return f"https://{ident}.recruitee.com"
    if ats == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{ident}"
    if ats == "workday":
        return f"https://{c.get('tenant')}.{c.get('shard')}.myworkdayjobs.com/{c.get('site')}"
    return str(ident)


# How many requests may hit one ATS at the same time (keeps the radar polite when polling in parallel).
ATS_CONCURRENCY = {"greenhouse": 4, "lever": 4, "ashby": 3, "smartrecruiters": 3, "workable": 2,
                   "recruitee": 2, "teamtailor": 2, "workday": 2}


# --------------------------------------------------------------------------- board discovery
# Recognise a job-board URL and turn it into a watchlist entry. Used by `harvest`
# (Common Crawl URLs) and to auto-add boards that aggregator results point to.
_STOP = {"", "embed", "favicon.ico", "robots.txt", "sitemap.xml", "api", "v1", "jobs", "job", "j", "search",
         "careers", "signup", "login", "static", "assets", "sr-jobs", "oneclick-ui", "ni", "www", "app",
         "support", "blog", "help", "status", "docs", "developer", "developers", "partner", "partners", "career",
         "privacy", "terms", "cookies", "security", "pricing", "about", "demo", "webinars", "resources"}

_BOARD_PATTERNS = [
    ("greenhouse", re.compile(
        r"^https?://(?:boards|job-boards)(\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?(?:[^#]*&)?for=)?([A-Za-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"^https?://jobs(\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"^https?://jobs()\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"^https?://apply()\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"^https?://(?:jobs|careers)()\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"^https?://()([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("teamtailor", re.compile(r"^https?://()([a-z0-9-]+)\.teamtailor\.com", re.I)),
]


def extract_board(url: str):
    """'https://jobs.lever.co/binance/abc' -> {'ats': 'lever', 'id': 'binance', 'host': None}"""
    for ats, rx in _BOARD_PATTERNS:
        m = rx.match(url or "")
        if not m:
            continue
        ident = unquote(m.group(2))
        if ident.lower() in _STOP or len(ident) < 2:
            return None
        if ats in ("recruitee", "teamtailor"):
            ident = ident.lower()
        return {"ats": ats, "id": ident, "host": "eu" if m.group(1) else None}
    return None


def board_entry(ats: str, ident: str, host: str | None = None, name: str | None = None) -> dict:
    """Build a companies.yaml-style entry for a discovered board."""
    entry = {"name": name or pretty_name(ident), "ats": ats,
             "id": f"https://{ident}.teamtailor.com" if ats == "teamtailor" else ident}
    if host:
        entry["host"] = host
    return entry


def pretty_name(ident: str) -> str:
    base = ident.split("//")[-1].split(".teamtailor.com")[0]
    return re.sub(r"[-_]+", " ", base).strip().title() or ident


# --------------------------------------------------------------------------- aggregator: Fantastic.jobs (paid, optional)
# Career-site jobs from 54 ATS platforms (incl. Workday, SuccessFactors, Oracle, Taleo, iCIMS),
# through their Apify actor. Billed per job returned. Needs an Apify API token.
FANTASTIC_URL = "https://api.apify.com/v2/acts/fantastic-jobs~career-site-job-listing-api/run-sync-get-dataset-items"
FANTASTIC_CONFIRMED_ATS = {"greenhouse", "lever.co", "ashby"}   # spellings seen on the feed's Apify page


def fantastic_jobs(ctx: Context, api_token: str, time_range: str) -> list[Job]:
    cfg = (ctx.settings.get("aggregators") or {}).get("fantastic") or {}
    body = {
        "timeRange": time_range,
        "limit": max(10, min(5000, int(cfg.get("limit", 300)))),
        "locationSearch": cfg.get("locations") or ["United Arab Emirates"],
        "descriptionType": "text",
    }
    if cfg.get("title_search"):
        body["titleSearch"] = cfg["title_search"]
    if cfg.get("title_exclusions"):
        body["titleExclusionSearch"] = cfg["title_exclusions"]
    if cfg.get("ats_include"):
        body["ats"] = cfg["ats_include"]
    if cfg.get("ats_exclude"):
        body["atsExclusionFilter"] = cfg["ats_exclude"]
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        items = ctx.http.request("POST", FANTASTIC_URL, json_body=body, timeout=300, headers=headers).json()
    except requests.HTTPError as exc:
        # Apify validates input against the feed's list of job-system names. If one of ours is not
        # on that list, retry with only the names confirmed on the feed's own page.
        bad_input = exc.response is not None and exc.response.status_code == 400
        if not (bad_input and "atsExclusionFilter" in body):
            raise
        body["atsExclusionFilter"] = [a for a in body["atsExclusionFilter"] if a in FANTASTIC_CONFIRMED_ATS]
        print(f"[fantastic] input rejected ({str(exc)[:120]}); retrying with ats_exclude = "
              f"{body['atsExclusionFilter']}")
        items = ctx.http.request("POST", FANTASTIC_URL, json_body=body, timeout=300, headers=headers).json()
    jobs = []
    for it in items if isinstance(items, list) else []:
        locs = []
        for loc in it.get("locations_derived") or []:
            if isinstance(loc, dict):
                locs.append(", ".join(x for x in [loc.get("city"), loc.get("admin"), loc.get("country")] if x))
            else:
                locs.append(str(loc))
        countries = " ".join(str(x) for x in (it.get("countries_derived") or [])).lower()
        salary = ""
        lo, hi = it.get("ai_salary_min_value"), it.get("ai_salary_max_value")
        if lo or hi or it.get("ai_salary_value"):
            amount = f"{lo or ''}-{hi or ''}" if (lo or hi) else str(it.get("ai_salary_value"))
            salary = f"{it.get('ai_salary_currency') or ''} {amount} {(it.get('ai_salary_unit_text') or '').lower()}".strip()
        job = Job(
            source=f"fantastic/{it.get('source') or 'ats'}", company=it.get("organization") or "",
            native_id=str(it.get("id")), title=(it.get("title") or "").strip(),
            location="; ".join(x for x in locs if x) or (it.get("locations_alt") or ""),
            url=it.get("url") or "", posted_at=str(it.get("date_posted") or ""),
            description=it.get("description_text") or "",
            country_code="ae" if "united arab emirates" in countries else "",
            workplace=it.get("ai_work_arrangement") or "", salary=salary,
        )
        job.hints = {"visa": bool(it.get("ai_visa_sponsorship"))}
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------- aggregator: Google for Jobs via JSearch (optional)
def jsearch_jobs(ctx: Context, api_key: str, queries: list[str]) -> list[Job]:
    jobs = []
    for q in queries:
        data = ctx.http.get_json(
            "https://jsearch.p.rapidapi.com/search",
            params={"query": q, "country": "ae", "date_posted": "today", "page": 1, "num_pages": 1},
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
        )
        for j in data.get("data") or []:
            jobs.append(Job(
                source=f"google/{j.get('job_publisher') or 'web'}", company=j.get("employer_name") or "",
                native_id=str(j.get("job_id")), title=(j.get("job_title") or "").strip(),
                location=", ".join(x for x in [j.get("job_city"), j.get("job_state"), j.get("job_country")] if x),
                url=j.get("job_apply_link") or j.get("job_google_link") or "",
                posted_at=j.get("job_posted_at_datetime_utc") or "",
                description=j.get("job_description") or "",
                country_code=(j.get("job_country") or "").lower(),
                workplace="Remote" if j.get("job_is_remote") else "",
            ))
    return jobs
