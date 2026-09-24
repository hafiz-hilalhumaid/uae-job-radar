#!/usr/bin/env python3
"""
UAE Job Radar - watches employers' own job feeds for UAE software roles every hour
and pings you on Telegram when a matching job goes live.

  python radar.py poll [--no-notify]           check every watched board (hourly, via GitHub Actions)
  python radar.py digest [--no-notify]         send held alerts + hiring-momentum summary
  python radar.py harvest                      collect job-board IDs from Common Crawl (monthly)
  python radar.py sweep [--max-minutes 40]     find which of those boards have UAE jobs (daily)
  python radar.py discover candidates.txt      find the job board behind specific company names
  python radar.py test-telegram                check your Telegram secrets
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
import yaml

from scoring import Scorer, fingerprint, is_ambiguous_location, location_type
from sources import (ADAPTERS, ATS_CONCURRENCY, ENRICHERS, Context, Http, NotFound, board_entry, board_url,
                     extract_board, fantastic_jobs, jsearch_jobs)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOBS_FILE = DATA / "jobs.json"
STATE_FILE = DATA / "state.json"
BOARDS_FILE = DATA / "uae_boards.json"    # boards found automatically (sweep / aggregator results)
TOKENS_FILE = DATA / "tokens.json"        # every board ID seen in Common Crawl (harvest)


# ----------------------------------------------------------------------------- helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: {path.name} is unreadable, starting fresh", file=sys.stderr)
    return default


def dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump(obj), encoding="utf-8")
    tmp.replace(path)


def load_yaml(name: str):
    with open(ROOT / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def company_key(c: dict) -> str:
    if c.get("ats") == "workday":
        return f"workday:{c.get('tenant')}/{c.get('site')}"
    return f"{c.get('ats')}:{c.get('id')}"


def user_agent(settings: dict) -> str:
    email = (settings.get("contact_email") or "").strip()
    return "uae-job-radar/1.0 (personal job search" + (f"; {email}" if email else "") + ")"


def h(text) -> str:
    return html.escape(str(text or ""), quote=False)


def short(text: str, n: int = 70) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def local_time(iso_str: str, tz: ZoneInfo, fmt: str = "%d %b %H:%M") -> str:
    try:
        return datetime.fromisoformat(iso_str).astimezone(tz).strftime(fmt)
    except (TypeError, ValueError):
        return ""


def fmt_job(rec: dict) -> str:
    salary = f" · {h(rec['salary'])}" if rec.get("salary") else ""
    flags = f"\n   ⚑ {h(', '.join(rec['flags']))}" if rec.get("flags") else ""
    url = html.escape(rec.get("url") or "", quote=True)
    return (
        f"<b>{rec['score']}</b> · <a href=\"{url}\">{h(rec['title'])}</a>\n"
        f"   {h(rec['company'])} · {h(short(rec.get('location', ''), 60))}{salary}{flags}"
    )


class Telegram:
    def __init__(self, enabled: bool = True):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = enabled and bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not text.strip():
            return True
        if not self.enabled:
            print("----- message (Telegram not configured / --no-notify) -----")
            print(re.sub(r"<[^>]+>", "", html.unescape(text)))
            return True
        ok = True
        for chunk in _chunks(text, 3800):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=20,
                )
                if r.status_code != 200:
                    ok = False
                    print(f"telegram error {r.status_code}: {r.text[:300]}", file=sys.stderr)
            except requests.RequestException as exc:
                ok = False
                print(f"telegram error: {exc}", file=sys.stderr)
            time.sleep(0.6)
        return ok


def _chunks(text: str, limit: int):
    buf = ""
    for block in text.split("\n\n"):
        if buf and len(buf) + len(block) + 2 > limit:
            yield buf
            buf = ""
        buf = f"{buf}\n\n{block}" if buf else block
    if buf.strip():
        yield buf


def load_state() -> dict:
    state = load_json(STATE_FILE, {})
    state.setdefault("host_cache", {})
    state.setdefault("health", {})
    state.setdefault("rejected", {})
    state.setdefault("queue", [])
    return state


# ----------------------------------------------------------------------------- reports
def write_reports(jobs: dict, tz: ZoneInfo) -> None:
    open_recs = sorted(
        (r for r in jobs.values() if r.get("status") == "open"),
        key=lambda r: (-r["score"], r.get("first_seen", "")),
    )

    def md(text) -> str:
        return str(text or "").replace("|", "\\|").replace("[", "(").replace("]", ")").replace("\n", " ")

    updated = datetime.now(tz).strftime("%a %d %b %Y, %H:%M")
    lines = [
        f"# Open UAE matches ({len(open_recs)})", "",
        f"_Last change {updated} ({tz.key}). Sorted by score. Generated by radar.py._", "",
        "| Score | Role | Company | Location | First seen | Flags |",
        "|---:|---|---|---|---|---|",
    ]
    for r in open_recs:
        url = (r.get("url") or "").replace(" ", "%20").replace(")", "%29")
        lines.append(
            f"| {r['score']} | [{md(r['title'])}]({url}) | {md(r['company'])} | {md(short(r.get('location', ''), 50))} "
            f"| {local_time(r.get('first_seen', ''), tz)} | {md(', '.join(r.get('flags', [])))} |"
        )
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "matches.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with open(DATA / "matches.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["score", "title", "company", "location", "first_seen", "flags", "salary", "source", "url"])
        for r in open_recs:
            writer.writerow([r["score"], r["title"], r["company"], r.get("location", ""), r.get("first_seen", ""),
                             "; ".join(r.get("flags", [])), r.get("salary", ""), r.get("source", ""), r.get("url", "")])


# ----------------------------------------------------------------------------- boards
def load_boards():
    """companies.yaml (your picks) + data/uae_boards.json (found automatically)."""
    manual = [c for c in (load_yaml("companies.yaml") or []) if c and c.get("enabled", True)]
    auto = load_json(BOARDS_FILE, {})
    boards, keys = [], set()
    for c in manual:
        keys.add(company_key(c))
        boards.append(dict(c, _auto=False))
    for ck, c in auto.items():
        if ck not in keys and c.get("ats") in ADAPTERS:
            keys.add(ck)
            boards.append(dict(c, _auto=True))
    return boards, auto


def fetch_boards(boards, ctx, workers: int):
    """Read many boards in parallel, never more than ATS_CONCURRENCY requests per ATS at once."""
    limits = {ats: threading.Semaphore(n) for ats, n in ATS_CONCURRENCY.items()}
    fallback = threading.Semaphore(2)

    def task(c):
        with limits.get(c["ats"], fallback):
            return ADAPTERS[c["ats"]](c, ctx)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, c): c for c in boards}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                yield c, fut.result(), None
            except Exception as exc:  # noqa: BLE001 - one broken board must not stop the run
                yield c, None, exc


def norm_url(url: str) -> str:
    """URL without tracking parameters, for spotting the same job arriving from two sources."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not re.match(r"(utm_|gh_src|source$|src$|ref$|lever-|ashby_.*src|trk)", k, re.I)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       urlencode(query), "")).lower()


class Tracker:
    """Decides what is new, dedups across sources, scores and records jobs."""

    def __init__(self, jobs, state, settings, scorer, ctx, now):
        self.jobs, self.state, self.settings, self.scorer, self.ctx, self.now = jobs, state, settings, scorer, ctx, now
        self.fp_index = {r["fingerprint"]: u for u, r in jobs.items() if r.get("status") == "open"}
        self.url_index = {norm_url(r.get("url", "")): u for u, r in jobs.items() if r.get("status") == "open"}

    def consider(self, job, ck: str, board: dict | None = None, expires_days: int | None = None):
        """Return a new record, or None if the job is known, unwanted or a duplicate."""
        uid = f"{ck}:{job.native_id}"
        if uid in self.jobs:
            rec = self.jobs[uid]
            if rec.get("status") == "closed" and expires_days is None:   # reopened on a board
                rec["status"] = "open"
                rec.pop("closed_at", None)
            return None
        if uid in self.state["rejected"]:
            return None

        category = self.scorer.title_match(job.title)
        if not category:
            self.state["rejected"][uid] = self.now
            return None
        loc = location_type(job, self.settings)
        ambiguous = is_ambiguous_location(job)
        if loc is None and not ambiguous:
            self.state["rejected"][uid] = self.now
            return None
        if board is not None and board["ats"] in ENRICHERS and (ambiguous or not job.description):
            try:
                ENRICHERS[board["ats"]](job, board, self.ctx)
            except Exception as exc:  # noqa: BLE001
                print(f"[{ck}] detail fetch failed for '{job.title}': {exc}", file=sys.stderr)
                if ambiguous:
                    return None                      # try again next run
            loc = location_type(job, self.settings)
            if loc is None:
                self.state["rejected"][uid] = self.now
                return None

        fp, nurl = fingerprint(job), norm_url(job.url)
        dup_uid = next((u for u in (self.fp_index.get(fp), self.url_index.get(nurl) if nurl else None)
                        if u and u in self.jobs), None)
        if dup_uid:                                   # same role already tracked from another source
            dup = self.jobs[dup_uid]
            if expires_days is None and dup.get("expires"):
                # An aggregator found it first; now its board is watched directly, so the board owns it
                # (it will be closed when it disappears from the board instead of aging out).
                rec = self.jobs.pop(dup_uid)
                rec.update(uid=uid, company_key=ck, source=job.source, url=job.url or rec.get("url", ""))
                rec.pop("expires", None)
                self.jobs[uid] = rec
                self.fp_index[fp] = uid
                if nurl:
                    self.url_index[nurl] = uid
                self.state["queue"] = [uid if u == dup_uid else u for u in self.state["queue"]]
            else:
                self.state["rejected"][uid] = self.now
            return None

        score, flags = self.scorer.score(job, category, loc)
        rec = {
            "uid": uid, "company_key": ck, "source": job.source, "company": job.company,
            "title": job.title, "location": job.location, "url": job.url,
            "posted_at": job.posted_at, "salary": job.salary, "workplace": job.workplace,
            "category": category[0], "loc_type": loc, "score": score, "flags": flags,
            "fingerprint": fp, "first_seen": self.now, "status": "open",
        }
        if expires_days:
            rec["expires"] = iso(datetime.fromisoformat(self.now) + timedelta(days=expires_days))
        self.jobs[uid] = rec
        self.fp_index[fp] = uid
        if nurl:
            self.url_index[nurl] = uid
        return rec


# ----------------------------------------------------------------------------- poll
def cmd_poll(args) -> int:
    settings = load_yaml("config.yaml")
    boards, auto = load_boards()
    jobs: dict = load_json(JOBS_FILE, {})
    state = load_state()
    jobs_before, state_before, auto_before = dump(jobs), dump(state), dump(auto)

    tz = ZoneInfo((settings.get("schedule") or {}).get("timezone", "Asia/Kolkata"))
    now_dt = now_utc()
    now, today = iso(now_dt), now_dt.date().isoformat()
    local_now = now_dt.astimezone(tz)
    quiet = local_now.strftime("%a") in set((settings.get("schedule") or {}).get("quiet_days") or [])
    thresholds = settings.get("thresholds") or {}
    instant_at = int(thresholds.get("instant", 70))
    digest_at = int(thresholds.get("digest", 40))
    urgent_at = int(thresholds.get("urgent_on_quiet_days", 101))
    first_run = not state.get("bootstrapped")

    ctx = Context(http=Http(user_agent(settings)), cache=state["host_cache"], settings=settings)
    tracker = Tracker(jobs, state, settings, Scorer(settings), ctx, now)
    new_recs, seeded, polled, failed = [], [], 0, 0
    active_keys = {company_key(c) for c in boards} | {"fantastic", "jsearch"}

    # 1) Every watched board, in parallel.
    results = sorted(fetch_boards(boards, ctx, int(settings.get("workers", 8))), key=lambda r: r[0]["name"].lower())
    for c, fetched, exc in results:
        ck = company_key(c)
        health = state["health"].setdefault(ck, {"fails": 0})
        if exc is not None:
            failed += 1
            health["fails"] = int(health.get("fails", 0)) + 1
            health["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"[{ck}] ERROR {health['last_error']}", file=sys.stderr)
            if c.get("_auto") and health["fails"] >= 5:
                auto.pop(ck, None)                     # auto-found board keeps failing: drop it
            continue
        polled += 1
        if health.get("fails"):
            health["fails"] = 0
            health.pop("last_error", None)

        if c.get("_auto") and ck in auto and any(location_type(j, settings) == "uae" for j in fetched):
            auto[ck]["last_uae"] = today

        seeding = not health.get("seeded")
        fetched_uids, new_here = set(), []
        for job in fetched:
            fetched_uids.add(f"{ck}:{job.native_id}")
            rec = tracker.consider(job, ck, board=c)
            if rec:
                new_here.append(rec)
        for uid, rec in jobs.items():                  # gone from the board -> closed
            if rec.get("company_key") == ck and rec.get("status") == "open" and uid not in fetched_uids:
                rec["status"], rec["closed_at"] = "closed", now
        if seeding:
            health["seeded"] = True
            for rec in new_here:
                rec["notified"] = "seed"
            seeded.extend(new_here)
        else:
            new_recs.extend(new_here)

    # 2) Aggregators (optional, need API keys) - catch employers not on the watchlist.
    agg = settings.get("aggregators") or {}
    apify_token = os.getenv("APIFY_TOKEN", "").strip()
    fcfg = agg.get("fantastic") or {}
    mode = str(fcfg.get("mode", "daily")).lower()
    daily_hour = int(fcfg.get("daily_hour", 7))
    today_local = local_now.date().isoformat()
    daily_due = local_now.hour >= daily_hour and state.get("fantastic_daily") != today_local
    window = "24h" if daily_due else "1h"
    if apify_token and fcfg.get("enabled", True) and (mode == "hourly" or daily_due):
        if daily_due:
            state["fantastic_daily"] = today_local     # once a day, even if GitHub runs the job late
        try:
            feed = fantastic_jobs(ctx, apify_token, window)
            print(f"[fantastic] {len(feed)} UAE jobs in the last {window}")
            for job in feed:
                found = extract_board(job.url)          # teach the radar a new board to watch directly
                if found and found["ats"] in ADAPTERS:
                    entry = board_entry(found["ats"], found["id"], found["host"], job.company or None)
                    key = company_key(entry)
                    if key not in active_keys and key not in auto:
                        auto[key] = dict(entry, found=today, last_uae=today, via="fantastic")
                rec = tracker.consider(job, "fantastic", expires_days=21)
                if rec:
                    (seeded if first_run else new_recs).append(rec)
        except Exception as exc:  # noqa: BLE001
            print(f"[fantastic] ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            state["health"].setdefault("fantastic", {})["last_error"] = str(exc)[:200]

    rapid_key = os.getenv("RAPIDAPI_KEY", "").strip()
    js_cfg = agg.get("jsearch") or {}
    every = max(1, int(js_cfg.get("every_hours", 4)))
    if rapid_key and js_cfg.get("queries") and now_dt.hour % every == 0:
        try:
            feed = jsearch_jobs(ctx, rapid_key, js_cfg["queries"])
            print(f"[jsearch] {len(feed)} results")
            for job in feed:
                rec = tracker.consider(job, "jsearch", expires_days=21)
                if rec:
                    (seeded if first_run else new_recs).append(rec)
        except Exception as exc:  # noqa: BLE001
            print(f"[jsearch] ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    # 3) Route new jobs: instant, digest, or silent.
    instant = []
    for rec in new_recs:
        if rec["score"] >= instant_at and (not quiet or rec["score"] >= urgent_at):
            rec["notified"] = "instant"
            instant.append(rec)
        elif rec["score"] >= digest_at:
            rec["notified"] = "queued"
            state["queue"].append(rec["uid"])
        else:
            rec["notified"] = "none"

    # 4) Housekeeping.
    for rec in jobs.values():
        if rec.get("status") != "open":
            continue
        if rec.get("company_key") not in active_keys:
            rec["status"], rec["closed_at"] = "closed", now      # board removed from the watchlist
        elif rec.get("expires") and rec["expires"] < now:
            rec["status"], rec["closed_at"] = "closed", now      # aggregator jobs age out after 21 days
    stale = iso(now_dt - timedelta(days=int(settings.get("drop_boards_after_days", 60))))[:10]
    for ck in [k for k, c in auto.items() if c.get("last_uae", today) < stale]:
        auto.pop(ck)                                               # no UAE jobs for 60 days
    reject_cutoff = iso(now_dt - timedelta(days=45))
    state["rejected"] = {u: t for u, t in state["rejected"].items() if t >= reject_cutoff}
    closed_cutoff = iso(now_dt - timedelta(days=60))
    jobs = {u: r for u, r in jobs.items()
            if not (r.get("status") == "closed" and r.get("closed_at", now) < closed_cutoff)}
    state["queue"] = [u for u in dict.fromkeys(state["queue"]) if u in jobs]

    # 5) Notify.
    tg = Telegram(enabled=not args.no_notify)
    if first_run:
        state["bootstrapped"] = True
        open_recs = sorted((r for r in jobs.values() if r.get("status") == "open"), key=lambda r: -r["score"])
        lines = [f"✅ <b>UAE Job Radar is live.</b> {len(open_recs)} matching UAE roles are open right now "
                 f"across {polled} boards ({failed} failed). Top {min(15, len(open_recs))}:"]
        lines += [fmt_job(r) for r in open_recs[:15]]
        lines.append("Full list: data/matches.md in your repo. From now on you only hear about NEW postings.")
        tg.send("\n\n".join(lines))
    else:
        if instant:
            instant.sort(key=lambda r: -r["score"])
            head = f"🆕 <b>{len(instant)} new UAE match{'es' if len(instant) != 1 else ''}</b>"
            tg.send("\n\n".join([head] + [fmt_job(r) for r in instant]))
        if seeded:
            seeded.sort(key=lambda r: -r["score"])
            companies = len({r["company_key"] for r in seeded})
            lines = [f"➕ <b>Newly watched boards</b>: {len(seeded)} matching UAE role{'s' if len(seeded) != 1 else ''} "
                     f"already open at {companies} compan{'ies' if companies != 1 else 'y'}. "
                     f"Best {min(10, len(seeded))}:"]
            lines += [fmt_job(r) for r in seeded[:10]]
            lines.append("All of them are in data/matches.md.")
            tg.send("\n\n".join(lines))

    # 6) Persist only when something changed (keeps the repo history small).
    jobs_changed = dump(jobs) != jobs_before
    if jobs_changed or first_run or not (DATA / "matches.md").exists():
        write_reports(jobs, tz)
    if jobs_changed:
        save_json(JOBS_FILE, jobs)
    if dump(state) != state_before:
        save_json(STATE_FILE, state)
    if dump(auto) != auto_before:
        save_json(BOARDS_FILE, auto)

    print(f"polled {polled} boards ({len(auto)} auto-found), {failed} failed, "
          f"{len(new_recs) + len(seeded)} new matching jobs, {len(instant)} instant alerts, "
          f"{len(state['queue'])} queued for digest{' (quiet day)' if quiet else ''}")
    return 0


# ----------------------------------------------------------------------------- harvest + sweep
# Coverage beyond your list: Common Crawl's public index lists every job-board URL its crawler saw.
# `harvest` turns those URLs into board IDs (monthly); `sweep` checks each board once and adds
# the ones with UAE jobs to data/uae_boards.json, which `poll` then watches every hour.
CC_PATTERNS = [
    "boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "job-boards.eu.greenhouse.io/*",
    "boards.eu.greenhouse.io/*", "jobs.lever.co/*", "jobs.eu.lever.co/*", "jobs.ashbyhq.com/*",
    "apply.workable.com/*", "jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*",
    "*.recruitee.com", "*.teamtailor.com",
]


def token_key(b: dict) -> str:
    return f"{b['ats']}|{b.get('host') or ''}|{b['id']}"


def cmd_harvest(args) -> int:
    settings = load_yaml("config.yaml")
    http = Http(user_agent(settings), timeout=120, pause=1.5)
    crawls = http.request("GET", "https://index.commoncrawl.org/collinfo.json", attempts=6).json()
    crawls = crawls[: args.crawls]                     # newest first
    existing = load_json(TOKENS_FILE, {})
    found = set(existing.get("boards", []))
    before = len(found)
    for crawl in crawls:
        api = crawl["cdx-api"]
        for pattern in CC_PATTERNS:
            try:
                meta = http.request("GET", api, params={"url": pattern, "output": "json", "showNumPages": "true"},
                                    attempts=6).text.strip()
                pages = int(json.loads(meta).get("pages", 0)) if meta.startswith("{") else int(meta or 0)
            except Exception as exc:  # noqa: BLE001
                print(f"[{crawl['id']}] {pattern}: page count failed ({exc})", file=sys.stderr)
                continue
            got = 0
            for page in range(min(pages, args.max_pages)):
                try:
                    text = http.request("GET", api, params={"url": pattern, "output": "json", "fl": "url",
                                                            "page": page}, attempts=6).text
                except NotFound:
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"[{crawl['id']}] {pattern} page {page}: {exc}", file=sys.stderr)
                    continue
                for line in text.splitlines():
                    try:
                        url = json.loads(line).get("url", "")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    b = extract_board(url)
                    if b:
                        k = token_key(b)
                        if k not in found:
                            found.add(k)
                            got += 1
            print(f"[{crawl['id']}] {pattern}: {pages} pages, {got} new boards")
    save_json(TOKENS_FILE, {"harvested": iso(now_utc()), "crawls": [c["id"] for c in crawls],
                            "boards": sorted(found)})
    print(f"{len(found)} boards known ({len(found) - before} new). Next: python radar.py sweep")
    return 0


def cmd_sweep(args) -> int:
    settings = load_yaml("config.yaml")
    tokens = load_json(TOKENS_FILE, {}).get("boards", [])
    if not tokens:
        print("No boards yet - run `python radar.py harvest` first.", file=sys.stderr)
        return 1
    boards, auto = load_boards()
    known = {company_key(c) for c in boards}
    state = load_state()
    ctx = Context(http=Http(user_agent(settings), pause=0.25), cache=state["host_cache"], settings=settings)
    today = now_utc().date().isoformat()
    cursor = int(state.get("sweep_cursor", 0)) % len(tokens)
    order = tokens[cursor:] + tokens[:cursor]
    deadline = time.monotonic() + args.max_minutes * 60
    limits = {ats: threading.Semaphore(n) for ats, n in ATS_CONCURRENCY.items()}

    def probe(tok: str):
        ats, host, ident = tok.split("|", 2)
        entry = board_entry(ats, ident, host or None)
        if ats == "smartrecruiters":
            entry["country"] = "ae"
        if company_key(entry) in known or ats not in ADAPTERS:
            return entry, None
        with limits.get(ats, threading.Semaphore(2)):
            jobs = ADAPTERS[ats](entry, ctx)
        uae = [j for j in jobs if location_type(j, settings) == "uae"]
        if uae and uae[0].company and uae[0].company.lower() != entry["name"].lower():
            entry["name"] = uae[0].company
        return entry, len(uae)

    checked = added = 0
    with ThreadPoolExecutor(max_workers=int(settings.get("workers", 8))) as pool:
        i = 0
        while i < len(order) and time.monotonic() < deadline:
            batch = order[i:i + 200]
            i += len(batch)
            for fut in as_completed([pool.submit(probe, t) for t in batch]):
                checked += 1
                try:
                    entry, uae = fut.result()
                except Exception:  # noqa: BLE001 - dead boards are normal here
                    continue
                if uae:
                    key = company_key(entry)
                    entry.pop("country", None)
                    if key not in auto:
                        added += 1
                        auto[key] = dict(entry, found=today, via="sweep")
                    auto[key].update(last_uae=today, uae_jobs=uae)
    state["sweep_cursor"] = (cursor + checked) % len(tokens)
    save_json(BOARDS_FILE, auto)
    save_json(STATE_FILE, state)
    print(f"checked {checked}/{len(tokens)} boards, {added} new UAE boards, {len(auto)} auto-watched in total; "
          f"cursor at {state['sweep_cursor']}")
    return 0


# ----------------------------------------------------------------------------- digest
def cmd_digest(args) -> int:
    settings = load_yaml("config.yaml")
    tz = ZoneInfo((settings.get("schedule") or {}).get("timezone", "Asia/Kolkata"))
    jobs = load_json(JOBS_FILE, {})
    state = load_state()
    now_dt = now_utc()

    queued = [jobs[u] for u in dict.fromkeys(state["queue"]) if u in jobs and jobs[u].get("status") == "open"]
    queued.sort(key=lambda r: -r["score"])
    lines = [f"📬 <b>Digest · {now_dt.astimezone(tz).strftime('%a %d %b')}</b> — "
             f"{len(queued)} role(s) held since the last digest"]
    lines += [fmt_job(r) for r in queued[:40]]
    if len(queued) > 40:
        lines.append(f"…and {len(queued) - 40} more in data/matches.md")

    week_ago = iso(now_dt - timedelta(days=7))
    momentum = Counter(
        r["company"] for r in jobs.values()
        if r.get("first_seen", "") >= week_ago and r.get("notified") != "seed"
    )
    ramping = [(name, n) for name, n in momentum.most_common(8) if n >= 2]
    if ramping:
        lines.append("📈 <b>Hiring momentum, last 7 days</b> — good targets for outreach to the hiring "
                     "manager, even for roles not posted yet:\n" +
                     "\n".join(f"• {h(name)}: {n} new matching roles" for name, n in ramping))

    broken = [(ck, hl) for ck, hl in state["health"].items() if int(hl.get("fails", 0)) >= 3]
    if broken:
        lines.append("🛠 <b>Boards failing 3+ times in a row</b> (fix or disable in companies.yaml):\n" +
                     "\n".join(f"• {h(ck)} — {h(short(hl.get('last_error', ''), 90))}" for ck, hl in broken[:12]))

    if not queued and not ramping and not broken:
        lines.append("Nothing new since the last digest.")
    Telegram(enabled=not args.no_notify).send("\n\n".join(lines))

    state["queue"] = []
    for rec in queued:
        rec["notified"] = "digest"
    save_json(STATE_FILE, state)
    save_json(JOBS_FILE, jobs)
    return 0


# ----------------------------------------------------------------------------- discover
def slug_variants(name: str) -> list[str]:
    base = re.sub(r"(?<![a-z])(llc|fz-?llc|fze|inc|ltd|technologies|technology|labs|group|holdings?)(?![a-z])",
                  " ", name.lower())
    compact = re.sub(r"[^a-z0-9.]", "", base).strip(".")
    dashed = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    plain = re.sub(r"[^a-z0-9]", "", base)
    return list(dict.fromkeys(v for v in (plain, dashed, compact) if v))


def cmd_discover(args) -> int:
    settings = load_yaml("config.yaml")
    names = [ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    ctx = Context(http=Http(user_agent(settings), pause=0.5), cache={}, settings=settings)
    scorer = Scorer(settings)
    known = {company_key(c) for c in (load_yaml("companies.yaml") or []) if c}
    print(f"# Probing {len(names)} companies on Greenhouse (US+EU), Lever, Ashby, Workable, "
          f"SmartRecruiters, Recruitee.\n# Paste the entries you want into companies.yaml. "
          f"Open each URL to confirm it is the right company.\n")
    for name in names:
        hits = []
        slugs = slug_variants(name)
        sr_ids = list(dict.fromkeys([re.sub(r"[^A-Za-z0-9]", "", name)] + slugs))
        for ats in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"):
            for slug in (sr_ids if ats == "smartrecruiters" else slugs):
                c = {"name": name, "ats": ats, "id": slug}
                if ats == "smartrecruiters":
                    c["country"] = ""          # count all jobs when probing
                if company_key(c) in known:
                    continue
                try:
                    jobs = ADAPTERS[ats](c, ctx)
                except (NotFound, requests.RequestException, ValueError, KeyError, TypeError):
                    continue
                except Exception:  # noqa: BLE001
                    continue
                if not jobs:
                    continue
                uae = [j for j in jobs if location_type(j, settings)]
                matching = [j for j in uae if scorer.title_match(j.title)]
                host = ctx.cache.get(f"{ats}:{slug}")
                hits.append((c, host, len(jobs), len(uae), len(matching)))
                break
        if not hits:
            print(f"# {name}: no public board found (custom site, Workday, Oracle, or a different slug)")
            continue
        for c, host, total, uae, matching in hits:
            host_part = ", host: eu" if host == "eu" else ""
            print(f"- {{name: \"{name}\", ats: {c['ats']}, id: \"{c['id']}\"{host_part}}}"
                  f"   # {total} jobs, {uae} in UAE, {matching} matching · {board_url(c, host)}")
    return 0


def cmd_test_telegram(args) -> int:
    tg = Telegram()
    if not tg.enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set.", file=sys.stderr)
        return 1
    return 0 if tg.send("✅ UAE Job Radar can reach you on Telegram.") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="UAE Job Radar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="check every watched board")
    p.add_argument("--no-notify", action="store_true", help="print alerts instead of sending them")
    d = sub.add_parser("digest", help="send held alerts and the hiring-momentum summary")
    d.add_argument("--no-notify", action="store_true")
    hv = sub.add_parser("harvest", help="collect job-board IDs from Common Crawl")
    hv.add_argument("--crawls", type=int, default=2, help="how many recent crawls to read (default 2)")
    hv.add_argument("--max-pages", type=int, default=400, help="safety cap per URL pattern per crawl")
    sw = sub.add_parser("sweep", help="check harvested boards for UAE jobs")
    sw.add_argument("--max-minutes", type=float, default=40, help="stop after this long; resumes next time")
    disc = sub.add_parser("discover", help="find the job system behind a list of company names")
    disc.add_argument("file", help="text file with one company name per line")
    sub.add_parser("test-telegram", help="send a test message")
    args = parser.parse_args()
    return {"poll": cmd_poll, "digest": cmd_digest, "harvest": cmd_harvest, "sweep": cmd_sweep,
            "discover": cmd_discover, "test-telegram": cmd_test_telegram}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
