"""
Offline end-to-end test: fakes every ATS response, runs poll twice and a digest.
Run:  python tests/test_offline.py      (no network, no Telegram needed)
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402
import sources  # noqa: E402
from scoring import Scorer, fingerprint, location_type, required_years  # noqa: E402

LONG_FE = ("We build with React, TypeScript and Next.js. 4+ years of experience with React. "
           "Visa sponsorship and relocation support provided. You'll work on LLM agents.")

FIXTURES = {
    # Greenhouse, US host
    "https://boards-api.greenhouse.io/v1/boards/careem/jobs": {"jobs": [
        {"id": 1, "title": "Senior Frontend Engineer", "location": {"name": "Dubai, United Arab Emirates"},
         "absolute_url": "https://job-boards.greenhouse.io/careem/jobs/1", "updated_at": "2026-09-20T10:00:00Z",
         "content": "&lt;p&gt;" + LONG_FE + "&lt;/p&gt;"},
        {"id": 2, "title": "Senior Backend Engineer", "location": {"name": "Karachi, Pakistan"},
         "absolute_url": "https://job-boards.greenhouse.io/careem/jobs/2", "content": ""},
        {"id": 3, "title": "Engineering Manager", "location": {"name": "Dubai"},
         "absolute_url": "https://job-boards.greenhouse.io/careem/jobs/3", "content": ""},
    ]},
    # Greenhouse EU board: EU API host is "unreachable", US host serves it
    "https://boards-api.greenhouse.io/v1/boards/ziina/jobs": {"jobs": [
        {"id": 4701132101, "title": "Full-Stack Engineer", "location": {"name": "Dubai"},
         "absolute_url": "https://job-boards.eu.greenhouse.io/ziina/jobs/4701132101",
         "content": "Next.js, React, TypeScript, Nest.js. Must be currently residing in the UAE."},
    ]},
    # Lever
    "https://api.lever.co/v0/postings/binance": [
        {"id": "abc", "text": "Senior Frontend Engineer - Stablecoin", "hostedUrl": "https://jobs.lever.co/binance/abc",
         "categories": {"location": "UAE, Dubai", "allLocations": ["Asia / Taiwan, Taipei", "UAE, Dubai"]},
         "createdAt": 1758000000000, "country": "AE", "descriptionPlain": "React and TypeScript. 5+ years of experience.",
         "lists": [{"text": "Requirements", "content": "<li>Web3 a plus</li>"}]},
        {"id": "def", "text": "Backend Engineer (Java)", "hostedUrl": "https://jobs.lever.co/binance/def",
         "categories": {"location": "Asia / Taiwan, Taipei"}, "country": "TW", "descriptionPlain": "Java"},
    ],
    # Ashby
    "https://api.ashbyhq.com/posting-api/job-board/ether.fi": {"jobs": [
        {"id": "e1", "title": "Senior Frontend Engineer", "location": "Cayman",
         "secondaryLocations": [{"location": "Dubai"}, {"location": "New York"}], "isListed": True,
         "publishedAt": "2026-09-19T08:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/ether.fi/e1",
         "descriptionPlain": "React, TypeScript, wagmi. 8+ years of professional experience.",
         "compensation": {"scrapeableCompensationSalarySummary": "$150K - $250K"}},
        {"id": "e2", "title": "AI Engineer, Agents", "location": "Dubai", "isListed": False,
         "publishedAt": "2026-09-21T08:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/ether.fi/e2",
         "descriptionPlain": "Agentic workflows with LLMs, LangGraph, Python, FastAPI. 3+ years of experience."},
    ]},
    # SmartRecruiters list + detail
    "https://api.smartrecruiters.com/v1/companies/Vitol/postings": {"totalFound": 1, "content": [
        {"id": "744", "name": "Full Stack Desk Developer (Typescript React & Python)", "releasedDate": "2026-09-18T00:00:00Z",
         "location": {"city": "Dubai", "country": "ae", "fullLocation": "Dubai, United Arab Emirates"}},
    ]},
    "https://api.smartrecruiters.com/v1/companies/Vitol/postings/744": {
        "postingUrl": "https://jobs.smartrecruiters.com/Vitol/744-full-stack",
        "jobAd": {"sections": {"jobDescription": {"text": "<p>TypeScript, React, Python. Immediate joiners preferred.</p>"}}}},
    # Workable
    "https://apply.workable.com/api/v1/widget/accounts/bayutdubizzle": {"jobs": [
        {"title": "Senior Software Engineer - ReactJS", "shortcode": "5956E4F3C1", "country": "United Arab Emirates",
         "city": "Dubai", "url": "https://apply.workable.com/bayutdubizzle/j/5956E4F3C1/", "published_on": "2026-09-17"},
        {"title": "Software Engineer - React", "shortcode": "LHR1", "country": "Pakistan", "city": "Lahore",
         "url": "https://apply.workable.com/bayutdubizzle/j/LHR1/"},
    ]},
    # Workday list (ambiguous location) + detail
    "https://parsons.wd5.myworkdayjobs.com/wday/cxs/parsons/Search/jobs": {"total": 1, "jobPostings": [
        {"title": "Senior Software Engineer", "externalPath": "/job/Abu-Dhabi/Senior-Software-Engineer_R1",
         "locationsText": "2 Locations", "postedOn": "Posted Today", "bulletFields": ["R1"]},
    ]},
    "https://parsons.wd5.myworkdayjobs.com/wday/cxs/parsons/Search/job/Abu-Dhabi/Senior-Software-Engineer_R1": {
        "jobPostingInfo": {"jobDescription": "<p>Angular or React. 6+ years of experience.</p>",
                           "location": "Abu Dhabi, United Arab Emirates", "additionalLocations": ["Dubai"],
                           "startDate": "2026-09-24", "externalUrl": "https://parsons.wd5.myworkdayjobs.com/Search/job/R1"}},
}

FIXTURES["https://api.lever.co/v0/postings/newco"] = [
    {"id": "n1", "text": "React Developer", "hostedUrl": "https://jobs.lever.co/newco/n1",
     "categories": {"location": "Dubai, UAE"}, "country": "AE", "descriptionPlain": "React, TypeScript, Next.js"}]
FIXTURES["https://api.lever.co/v0/postings/nouae"] = [
    {"id": "x1", "text": "Frontend Engineer", "hostedUrl": "https://jobs.lever.co/nouae/x1",
     "categories": {"location": "Berlin"}, "country": "DE"}]
FIXTURES["https://api.ashbyhq.com/posting-api/job-board/ashbystartup"] = {"jobs": [
    {"id": "xyz", "title": "Full Stack Engineer", "location": "Abu Dhabi", "jobUrl": "https://jobs.ashbyhq.com/ashbystartup/xyz",
     "descriptionPlain": "Node, React"}]}

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item><title>Full-stack Engineer (AI-native)</title><link>https://careers.qashio.com/jobs/1-fs</link>
<guid>1-fs</guid><pubDate>Mon, 22 Sep 2026 08:00:00 +0000</pubDate>
<description>&lt;p&gt;React, TypeScript, LLM agents.&lt;/p&gt;</description>
<tt:locations><tt:location><tt:city>Dubai</tt:city><tt:country>United Arab Emirates</tt:country></tt:location></tt:locations></item>
<item><title>Account Executive</title><link>https://careers.qashio.com/jobs/2-ae</link><guid>2-ae</guid></item>
</channel></rss>"""

COMPANIES = [
    {"name": "Careem", "ats": "greenhouse", "id": "careem", "tier": "A"},
    {"name": "Ziina", "ats": "greenhouse", "id": "ziina", "host": "eu", "tier": "A"},
    {"name": "Tamara", "ats": "greenhouse", "id": "tamara", "host": "eu"},
    {"name": "Binance", "ats": "lever", "id": "binance", "tier": "A"},
    {"name": "Ether.fi", "ats": "ashby", "id": "ether.fi", "tier": "A"},
    {"name": "Vitol", "ats": "smartrecruiters", "id": "Vitol", "tier": "A"},
    {"name": "Bayut", "ats": "workable", "id": "bayutdubizzle", "tier": "A"},
    {"name": "Qashio", "ats": "teamtailor", "id": "https://careers.qashio.com", "tier": "A"},
    {"name": "Parsons", "ats": "workday", "tenant": "parsons", "shard": "wd5", "site": "Search", "tier": "C"},
    {"name": "Broken Co", "ats": "lever", "id": "doesnotexist", "tier": "A"},
]


class FakeResp:
    def __init__(self, data=None, text=""):
        self._data, self.text = data, text

    def json(self):
        return self._data


CC_API = "https://index.commoncrawl.org/CC-MAIN-2026-34-index"
CC_LINES = {
    "jobs.lever.co/*": ['{"url": "https://jobs.lever.co/newco/1"}', '{"url": "https://jobs.lever.co/nouae/2"}',
                        '{"url": "https://jobs.lever.co/binance/abc"}'],
    "jobs.ashbyhq.com/*": ['{"url": "https://jobs.ashbyhq.com/deadboard"}'],
}
FANTASTIC_ITEMS = [
    {"id": 555, "title": "Frontend Engineer (React/TypeScript)", "organization": "Workday Corp UAE",
     "url": "https://acme.wd3.myworkdayjobs.com/Careers/job/Dubai/FE_R9", "date_posted": "2026-09-25T06:00:00",
     "locations_derived": [{"city": "Dubai", "admin": "Dubai", "country": "United Arab Emirates"}],
     "countries_derived": ["United Arab Emirates"], "description_text": "React and TypeScript. 4 years of experience.",
     "ai_visa_sponsorship": True, "source": "workday"},
    {"id": 556, "title": "Full Stack Engineer", "organization": "Ashby Startup",
     "url": "https://jobs.ashbyhq.com/ashbystartup/xyz?utm_source=fantastic", "date_posted": "2026-09-25T05:00:00",
     "locations_derived": ["Abu Dhabi, Abu Dhabi, United Arab Emirates"],
     "countries_derived": ["United Arab Emirates"], "description_text": "Node, React", "source": "ashby"},
]


def fake_request(self, method, url, *, params=None, json_body=None, headers=None, timeout=None, attempts=3):
    if url.endswith("collinfo.json"):
        return FakeResp([{"id": "CC-MAIN-2026-34", "cdx-api": CC_API}])
    if url == CC_API:
        lines = CC_LINES.get(params["url"], [])
        if params.get("showNumPages"):
            return FakeResp(text='{"pages": %d, "pageSize": 5, "blocks": 1}' % (1 if lines else 0))
        return FakeResp(text="\n".join(lines))
    if "api.apify.com" in url:
        assert headers["Authorization"] == "Bearer test-token"
        return FakeResp(FANTASTIC_ITEMS)
    raise sources.NotFound(url)


def fake_get_json(self, url, params=None, headers=None):
    if "eu.greenhouse.io" in url or "api.eu.lever.co" in url or "doesnotexist" in url:
        raise sources.NotFound(url)
    if url in FIXTURES:
        data = FIXTURES[url]
        if "api.lever.co" in url and params and params.get("skip"):
            return []
        return data
    raise sources.NotFound(url)


def fake_post_json(self, url, body, headers=None):
    data = FIXTURES.get(url)
    if data is None:
        raise sources.NotFound(url)
    return data if body.get("offset") == 0 else {"jobPostings": []}


EU_PAGES = {
    # new-style board page: title and location inside the link; a classic-layout row; a non-UAE job
    "https://job-boards.eu.greenhouse.io/tamara": b"""<html><body><table>
<tr class="job-post"><td><a href="https://job-boards.eu.greenhouse.io/tamara/jobs/4988000101">
<p class="body body--medium">Senior Frontend Engineer</p><p class="body body__secondary body--metadata">Dubai, United Arab Emirates</p></a></td></tr>
<tr class="job-post"><td><a href="https://job-boards.eu.greenhouse.io/tamara/jobs/4988000201">
<p class="body">Backend Engineer</p><p class="body body__secondary">Riyadh, Saudi Arabia</p></a></td></tr>
</table><div class="opening"><a href="/tamara/jobs/4988000301">Full Stack Engineer</a><span class="location">Dubai</span></div>
</body></html>""",
    "https://job-boards.eu.greenhouse.io/tamara/jobs/4988000101": b"""<html><head><script>var x=1;</script></head>
<body><div class="job__location">Dubai, United Arab Emirates</div><div>React, TypeScript, Next.js. 5+ years of experience.
Visa sponsorship provided.</div></body></html>""",
}


def fake_get_bytes(self, url, params=None):
    if url == "https://careers.qashio.com/jobs.rss":
        return RSS
    if url in EU_PAGES:
        return EU_PAGES[url]          # ignores ?page=2, like a board with a single page
    raise sources.NotFound(url)


def run(cmd, **kw):
    ns = argparse.Namespace(no_notify=True, crawls=2, max_pages=10, max_minutes=5)
    return {"poll": radar.cmd_poll, "digest": radar.cmd_digest, "harvest": radar.cmd_harvest,
            "sweep": radar.cmd_sweep}[cmd](ns)


def main():
    sources.Http.get_json = fake_get_json
    sources.Http.post_json = fake_post_json
    sources.Http.get_bytes = fake_get_bytes
    sources.Http.request = fake_request

    real_load_yaml = radar.load_yaml

    def test_yaml(name):
        if name == "companies.yaml":
            return COMPANIES
        cfg = real_load_yaml(name)
        if name == "config.yaml":
            cfg["aggregators"]["fantastic"]["mode"] = "hourly"   # exercise the feed on every test run
        return cfg
    radar.load_yaml = test_yaml

    tmp = Path(tempfile.mkdtemp())
    radar.DATA, radar.JOBS_FILE, radar.STATE_FILE = tmp, tmp / "jobs.json", tmp / "state.json"
    radar.BOARDS_FILE, radar.TOKENS_FILE = tmp / "uae_boards.json", tmp / "tokens.json"
    radar.os.environ.pop("APIFY_TOKEN", None)
    radar.os.environ.pop("RAPIDAPI_KEY", None)

    # --- unit checks
    settings = real_load_yaml("config.yaml")
    scorer = Scorer(settings)
    assert scorer.title_match("Senior Frontend Engineer")[0] == "frontend"
    assert scorer.title_match("Full-Stack Engineer")[0] == "fullstack"
    assert scorer.title_match("Engineering Manager") is None
    assert scorer.title_match("Business Developer") is None
    assert scorer.title_match("AI Data Engineer") is None
    assert scorer.title_match("Forward Deployed Engineer, Agentic Platform")[0] == "ai"
    assert required_years("Need 3-5 years of experience in React") == 3
    assert required_years("Founded 10 years ago. 8+ years of professional experience.") == 8
    j = sources.Job("x", "Acme LLC", "1", "Sr. Frontend Engineer (React)", "Dubai, UAE", "u")
    assert fingerprint(j) == "acme|senior frontend engineer|dubai", fingerprint(j)
    assert location_type(sources.Job("x", "a", "1", "t", "Remote - MENA", "u"), settings) == "regional"
    assert location_type(sources.Job("x", "a", "1", "t", "Karachi", "u"), settings) is None

    # --- first run: bootstrap, no instant alerts
    print("\n=== RUN 1 (bootstrap) ===")
    run("poll")
    jobs = radar.load_json(radar.JOBS_FILE, {})
    titles = sorted(r["title"] for r in jobs.values())
    print("tracked:", titles)
    assert "Senior Backend Engineer" not in titles          # Karachi
    assert "Engineering Manager" not in titles              # excluded title
    assert "Software Engineer - React" not in titles        # Lahore
    assert "Account Executive" not in titles
    assert "Senior Software Engineer" in titles              # Workday, resolved via detail call
    assert len(jobs) == 11, len(jobs)
    tam = {r["title"]: r for r in jobs.values() if r["company"] == "Tamara"}
    assert set(tam) == {"Senior Frontend Engineer", "Full Stack Engineer"}, tam   # Riyadh job dropped
    assert "visa/relocation" in tam["Senior Frontend Engineer"]["flags"]          # read from the job page
    assert tam["Full Stack Engineer"]["url"] == "https://job-boards.eu.greenhouse.io/tamara/jobs/4988000301"
    state = radar.load_json(radar.STATE_FILE, {})
    assert state["bootstrapped"] and state["health"]["lever:doesnotexist"]["fails"] == 1
    assert state["host_cache"]["greenhouse:ziina"] == "us"          # EU page missing -> US API fallback
    assert state["host_cache"]["greenhouse:tamara"] == "eu_page"
    ziina = next(r for r in jobs.values() if r["company"] == "Ziina")
    assert "UAE residents only" in ziina["flags"], ziina
    unlisted = next(r for r in jobs.values() if r["title"] == "AI Engineer, Agents")
    assert "unlisted role" in unlisted["flags"]
    ether = next(r for r in jobs.values() if r["title"] == "Senior Frontend Engineer" and r["company"] == "Ether.fi")
    assert "8+ yrs asked" in ether["flags"]
    vitol = next(r for r in jobs.values() if r["company"] == "Vitol")
    assert "immediate joiner" in vitol["flags"] and vitol["url"].endswith("744-full-stack")
    for r in sorted(jobs.values(), key=lambda r: -r["score"]):
        print(f"  {r['score']:>3}  {r['title']} · {r['company']} · {r['flags']}")
    assert (tmp / "matches.md").exists() and (tmp / "matches.csv").exists()

    # --- second run: a new job appears, one disappears -> instant alert + closure
    print("\n=== RUN 2 (new posting) ===")
    FIXTURES["https://boards-api.greenhouse.io/v1/boards/careem/jobs"]["jobs"].append(
        {"id": 9, "title": "Frontend Engineer II (React)", "location": {"name": "Dubai"},
         "absolute_url": "https://job-boards.greenhouse.io/careem/jobs/9", "content": LONG_FE})
    FIXTURES["https://boards-api.greenhouse.io/v1/boards/careem/jobs"]["jobs"].append(
        {"id": 10, "title": "Software Engineer (Backend)", "location": {"name": "Dubai"},
         "absolute_url": "https://job-boards.greenhouse.io/careem/jobs/10", "content": "Go, Kafka"})
    FIXTURES["https://apply.workable.com/api/v1/widget/accounts/bayutdubizzle"]["jobs"].pop(0)
    run("poll")
    jobs = radar.load_json(radar.JOBS_FILE, {})
    new = jobs["greenhouse:careem:9"]
    assert new["notified"] == "instant", new
    assert jobs["greenhouse:careem:10"]["notified"] in ("queued", "none")
    assert jobs["workable:bayutdubizzle:5956E4F3C1"]["status"] == "closed"

    # --- third run: nothing changes -> nothing written
    before = radar.JOBS_FILE.stat().st_mtime_ns
    run("poll")
    assert radar.JOBS_FILE.stat().st_mtime_ns == before, "jobs.json rewritten without changes"

    # --- coverage beyond the list: harvest Common Crawl, sweep, then poll picks the new board up
    print("\n=== HARVEST + SWEEP ===")
    run("harvest")
    tokens = radar.load_json(radar.TOKENS_FILE, {})["boards"]
    assert tokens == ["ashby||deadboard", "lever||binance", "lever||newco", "lever||nouae"], tokens
    run("sweep")
    auto = radar.load_json(radar.BOARDS_FILE, {})
    assert list(auto) == ["lever:newco"], auto                 # nouae (Berlin) and dead board skipped
    print("\n=== RUN 4 (auto-found board + Fantastic feed) ===")
    radar.os.environ["APIFY_TOKEN"] = "test-token"
    run("poll")
    jobs = radar.load_json(radar.JOBS_FILE, {})
    assert jobs["lever:newco:n1"]["notified"] == "seed"         # first read of a new board: no flood
    fj = jobs["fantastic:555"]
    assert fj["notified"] == "instant" and "visa/relocation" in fj["flags"], fj
    auto = radar.load_json(radar.BOARDS_FILE, {})
    assert "ashby:ashbystartup" in auto, auto                   # learned from the Fantastic result
    assert "fantastic:556" in jobs

    print("\n=== RUN 5 (Fantastic returns the same jobs again; new board polled directly) ===")
    run("poll")
    jobs = radar.load_json(radar.JOBS_FILE, {})
    assert "fantastic:556" not in jobs and jobs["ashby:ashbystartup:xyz"]["notified"] == "queued", \
        "a job found by the aggregator is handed over to its board, not tracked twice"
    assert "expires" not in jobs["ashby:ashbystartup:xyz"]
    assert "ashby:ashbystartup:xyz" in radar.load_json(radar.STATE_FILE, {})["queue"]

    print("\n=== DIGEST ===")
    run("digest")
    assert radar.load_json(radar.STATE_FILE, {})["queue"] == []
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
