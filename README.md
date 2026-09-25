# UAE Job Radar

Every hour, checks the job boards of **every employer it can find with a UAE opening**. When a
React / TypeScript / full-stack / AI role appears in the UAE, it scores the role against your
profile and messages you on Telegram.

The boards it checks are the employers' own feeds on Greenhouse, Lever, Ashby, SmartRecruiters,
Workable, Recruitee, Teamtailor and Workday. A Gmail add-on forwards LinkedIn, Indeed, Bayt,
GulfTalent and Naukrigulf alerts to the same chat, so everything lands in one place.

Runs free on GitHub Actions. No server, no database, no scraping of LinkedIn.

---

## How it covers (almost) every UAE employer

It doesn't rely on a hand-picked list. Four layers feed one Telegram chat, deduplicated:

| Layer | Who it covers | Delay | Cost |
|---|---|---|---|
| **1. Every board with a UAE job** — monthly `harvest` pulls every Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Recruitee/Teamtailor board ID from Common Crawl's public web index; daily `sweep` checks each once and keeps those with UAE jobs; `poll` checks all of those hourly | Startups, scale-ups, global tech/fintech/crypto firms with UAE teams | ≤ ~1 hour | Free |
| **2. Fantastic.jobs** (optional) — a database of 175k+ career sites on 54 job systems | Big employers on Workday, SuccessFactors, Oracle, Taleo, iCIMS, Zoho… (banks, conglomerates, many Abu Dhabi groups) | daily mode: up to ~1 day · hourly mode: ~2–4 hours | Pay per job; the free $5/month Apify credit likely covers daily mode |
| **3. Board alerts via Gmail** | Companies and agencies that post only on LinkedIn, Bayt, GulfTalent, Naukrigulf, Indeed | depends on each site's alert timing | Free |
| **4. Google for Jobs via JSearch** (optional) | Anything Google indexes, including company sites with job markup | hours | Free plan: every 4 hours |

Plus `companies.yaml`: companies you always want watched, even with no UAE opening today.

Fantastic.jobs results also **teach** layer 1. When one of its jobs sits on a Greenhouse/Lever/Ashby/…
board the radar didn't know, that board is added and watched directly from then on.

**What no layer sees:** career pages with no feed and no job markup, roles shared only in private
WhatsApp groups or by agencies, and referrals. For those, use the digest's "Hiring momentum" list
for outreach.

---

## What it catches, and what it can't

**1. Jobs before the boards show them.** Employers publish to their own hiring system first.
LinkedIn, Bayt and Google copy it hours or days later. The radar reads the source hourly, so you're
often among the first applicants.

**2. Jobs that are posted but easy to miss.** These include roles only on a company careers
site, Workday roles Google indexes poorly, and roles flagged **"unlisted role"**. Ashby's feed
marks roles hidden from the public careers page, and when the feed includes them the radar shows them.

**3. Jobs never advertised.** Referrals, recruiter-only mandates and internal moves: no software
sees these. The digest's **"Hiring momentum"** section lists companies that opened 2+ matching
UAE roles this week. Those teams are growing, so message the hiring manager even before your
exact role is posted.

Beware "secret job" offers in Telegram/WhatsApp groups that ask for visa or processing fees.
That is the most common UAE job scam.

---

## Setup (about 20 minutes)

### 1. Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → copy the **token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number
   after `"chat":{"id":`. That's your **chat ID**.

### 2. GitHub repository
Create a new repository and upload everything in this folder, keeping the `.github` folder.

**Public or private?**
- **Public (recommended):** unlimited free Actions minutes, so hourly polling plus the nightly
  sweep costs nothing. It shows on your profile, so pick a neutral name if colleagues might look.
- **Private:** 2,000 free minutes/month. The hourly poll alone uses roughly 1,400–2,200.
  Change the sweep to twice a week (see the comment in `sweep.yml`) and check
  Settings → Billing after a few days. GitHub bills extra minutes if you go over.

### 3. Secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Also check **Settings → Actions → General → Workflow permissions** is set to **Read and write**.
The radar saves its state back into `data/`.

Optional, for layers 2 and 4:
- `APIFY_TOKEN`: from apify.com → Settings → API & Integrations. Enables Fantastic.jobs.
- `RAPIDAPI_KEY`: subscribe to the JSearch API on RapidAPI (free plan). Enables Google for Jobs.

### 4. First run
1. **Actions → poll → Run workflow.** It reads your `companies.yaml` boards and sends one summary:
   "N matching UAE roles open right now, top 15". After that you only hear about **new** postings.
2. **Actions → harvest → Run workflow.** It collects every job-board ID from Common Crawl's two most
   recent crawls. This takes 30–120 minutes and then runs monthly by itself.
3. **Actions → sweep → Run workflow**, a few times over the first day or two, or just let it run
   nightly. Each run checks boards for 40 minutes and resumes where it stopped. Boards with UAE
   jobs join the hourly poll automatically.
   - The first time a newly found board is read, you get **one** "Newly watched boards" message
     with its best open roles, not a flood.

The full list is always in `data/matches.md` (also `data/matches.csv` for Sheets). Auto-found
boards are in `data/uae_boards.json`. The digest runs Mon/Wed/Fri at 08:03 IST.

### 4b. Reliable schedule (recommended)
Since late August 2026, GitHub's built-in cron has been firing far less often than configured. An hourly
job may run only 2–3 times a day. `apps_script/Scheduler.gs` fixes this by starting the workflows through
GitHub's API on Google's timer: poll every hour, sweep daily ~04:00 IST, digest Mon/Wed/Fri ~08:00 IST.
GitHub's own cron stays on as a backup. Duplicate runs are harmless, and the digest is sent once a day
at most.

1. **GitHub token:** your profile picture → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
   - Name: `uae-job-radar scheduler`.
   - Expiration: past the end of your search if offered, otherwise the longest available. Note the date.
   - Repository access: **Only select repositories** → `uae-job-radar`.
   - Permissions → Repository permissions → **Actions: Read and write**.
   - **Generate token** and copy it.
2. **script.google.com:** open your forwarder project, or create a **New project**. Add a script file
   (**＋ → Script**) named `Scheduler` and paste `apps_script/Scheduler.gs` into it. Check `RADAR` at the
   top matches your GitHub username and repo name.
3. **Project Settings → Script properties → Add script property:** `GITHUB_TOKEN` = the token.
4. Select `setupSchedulerTriggers` → **Run** → approve the permissions.
5. **Test:** select `runPoll` → **Run**. A new poll run should appear in your Actions tab within a minute.

If the token expires or is revoked, Google e-mails you that the trigger failed. Create a new token and
update `GITHUB_TOKEN`.

### 5. Board alerts → Telegram (LinkedIn, Indeed, Bayt, GulfTalent, Naukrigulf…)
1. Create job alerts on each site. Use UAE as the location, one alert per role keyword
   (frontend, react, full stack, software engineer, AI engineer), and the most frequent option each offers.
   - **Google Jobs:** search `react developer jobs in Dubai`, open the jobs panel, turn on alerts.
   - **Dubai Careers** (dubaicareers.ae) and **HiringCafe** have e-mail alerts too.
2. In Gmail, create a label **`job-alerts`**. Once the first alert e-mails arrive, make a filter
   on their **From** addresses that applies the label. Build it from the senders you actually
   receive; they differ by site and change over time.
3. Go to **script.google.com → New project**, paste `apps_script/Code.gs`, then
   **Project Settings → Script properties**: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Select `setupTriggers` → **Run** → approve. It now checks the label every 10 minutes.

Link-pattern matching is best-effort. If a site's links come through as "Alert" instead of the
site name, the job still arrives. Tweak `LINK_PATTERNS` if you want cleaner dedup.

### 6. Boards to open by hand (no public feed)
Check these daily on your campaign days:
- Startup portfolio boards: `jobs.hub71.com`, `jobs.global.vc`, `jobs.mevp.com`, `careers.becocapital.com`
- Company sites: G42 (`careers.g42.ai`), MBZUAI, Tabby, Noon
- Big UAE employers' portals

---

## How scoring works

| Signal | Effect |
|---|---|
| Title category: frontend / full stack 45, AI 42, general software 30 | base score |
| React, TypeScript, Next.js, LLM, agentic, LangGraph, FastAPI… in description | up to +25 |
| Located in the UAE / "Remote – MENA/GCC" | +15 / +5 |
| Visa or relocation mentioned | +10 |
| Asks ≤5 years / 7 years / 8+ years | +5 / −8 / −20 |
| Staff / Principal / Architect · Lead · Junior in title | −15 · −5 · −10 |
| "UAE residents only", "currently based in UAE" | −30 |
| "Immediate joiners" (bad fit for a long notice period) | −15 |
| Arabic required | −15 |
| Emirati-only / Emiratisation | −60 |

**≥70** → Telegram on that hourly check. **40–69** → next digest. **<40** → only in `matches.md`.
Sundays are quiet: alerts wait for Monday's digest unless a role scores 90+.
Change any of this in `config.yaml`.

---

## Growing coverage

Coverage grows by itself: the monthly harvest finds new boards, the nightly sweep adds the ones with
UAE jobs, and Fantastic.jobs results add any board they point to. Auto-found boards with no UAE job
for 60 days are dropped again (`drop_boards_after_days` in `config.yaml`).

To add specific companies by hand:
1. Put their names in `candidates.txt`, one per line.
2. Run `python radar.py discover candidates.txt` on your laptop (`pip install -r requirements.txt` first).
3. It probes Greenhouse (US and EU), Lever, Ashby, Workable, SmartRecruiters and Recruitee.
   For each board it finds, it prints a ready-to-paste line with UAE job counts and the board URL.
4. Confirm the URL is the right company, then paste the line into `companies.yaml`.

## Spend the Fantastic.jobs credit only where it counts

The radar already reads Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and Teamtailor
boards itself, for free. `config.yaml` → `ats_exclude` tells the paid feed to skip those, so the ~12 jobs
a day come only from systems the radar can't read (Workday, Oracle, SuccessFactors, Taleo, iCIMS…).
It's already filled in. If Apify ever rejects one of the names, the radar retries with the three names
confirmed on the feed's page (greenhouse, lever.co, ashby) and says so in the Actions log.

## Costs

| Piece | Monthly cost |
|---|---|
| GitHub Actions, Telegram, Gmail script, Common Crawl | Free (public repo) |
| Fantastic.jobs (optional) | $0 on Apify's free plan. It works through Apify, **not** Fantastic's own $95+/month API plans. The "$4 per 1,000 jobs" on the Apify page is the best-plan price; on the free plan a test run cost **$0.25 for 20 jobs (~$12.50 per 1,000)**, so the free $5 buys roughly 400 jobs a month. `limit: 12` in daily mode stays inside that. With no card on file, Apify blocks runs when the credit is used up and charges nothing; the radar sends a Telegram message when the feed pauses and when it's back. |
| JSearch (optional) | Free plan (200 requests/month) at one query every 4 hours |

## Troubleshooting

- **"Boards failing 3+ times" in the digest.** The slug is wrong or the company changed hiring
  systems. Open its careers page, check where "Apply" leads, fix the entry or add `enabled: false`.
- **Greenhouse EU boards** (`host: eu`). The radar tries the EU API host, falls back to the
  standard host, and remembers whichever answered.
- **Workday errors.** Some Workday sites block cloud servers. Move that company to a
  page-change monitor or drop it.
- **Teamtailor roles outside the UAE showing up.** If a company's feed carries no location,
  `default_location` is used. Delete that line to only keep roles with a UAE location in the feed.
- **Runs late.** GitHub may delay scheduled runs by several minutes, occasionally more. That's
  normal; the radar is still hours ahead of the job boards.
- **Harvest shows errors for some patterns.** Common Crawl's index server gets busy and the radar
  retries. Re-run the harvest later; boards already collected are kept.
- **A poll takes long.** Lower `workers` if boards start failing with HTTP 429, or raise it
  (max ~12) if runs are slow. Per-system limits keep each job site at a few requests at a time.
- **Stopped running.** GitHub pauses schedules in repos with no activity for 60 days. The radar
  commits to `data/` whenever jobs change, which normally keeps it alive. If it pauses anyway,
  re-enable it under Actions.
- **Test without Telegram:** `python radar.py poll --all --no-notify` prints messages instead.
- **Offline self-test:** `python tests/test_offline.py`.

## Ground rules

- It only reads public job feeds that employers' careers pages use, a few requests per job system
  at a time with pauses, each board at most once an hour.
- It never logs into LinkedIn or scrapes it. Scraping breaks LinkedIn's terms and risks your
  account, which matters more than any data. LinkedIn arrives through your own alert e-mails instead.
- Apply through the normal apply link. The radar just gets you there first.
