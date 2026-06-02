# Job Search Tracker

A personal tool for ingesting job search results from multiple sources (via Apify) into a local SQLite database, and reviewing them through a Flask web UI.

![Job Search Tracker UI](docs/screenshot.png)

---

## How it works

1. You configure one or more Apify Actor tasks using either:
   - **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** — LinkedIn job postings (default)
   - **[fantastic-jobs/career-site-job-listing-api](https://apify.com/fantastic-jobs/career-site-job-listing-api)** — career-site postings from 54+ ATS platforms (Greenhouse, Lever, Workday, Ashby, etc.)
2. A cron job runs `ingest.sh` on a schedule, fetching the latest results and inserting new jobs into a local SQLite database.
3. You run the Flask app locally to browse, filter, sort, and track your application status for each job.

---

## Prerequisites

- Python 3.11 or later (3.11 introduced `tomllib`). Python 3.12+ recommended.
- An [Apify](https://apify.com) account (free tier is sufficient for personal use)

> **Linux note:** On Debian/Ubuntu, `python3-venv` is a separate package and may not be installed by default. If `python3 -m venv .venv` fails, run `sudo apt install python3-venv` first.

---

## Apify setup

### 1. Create an Apify account

Sign up at [apify.com](https://apify.com). The free tier includes enough monthly compute to run several searches multiple times per day.

### 2. Find your API token

In the Apify Console, go to **Settings → Integrations → API tokens**. Copy your personal API token — you'll need it in `config.toml`.

### 3. Set up the Actor(s)

Navigate to the Actor(s) you want to use in the Apify Store and click **Try for free**:

- **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** — LinkedIn postings
- **[fantastic-jobs/career-site-job-listing-api](https://apify.com/fantastic-jobs/career-site-job-listing-api)** — career-site postings across ATS platforms

### 4. Create a Task for each search

Rather than running the Actor directly, create a **Task** for each search so you can save your parameters and schedule runs.

1. In the Actor page, click **Create new Task**.
2. Give the task a descriptive name, e.g. `my-job-search-dc-dmv`.
3. Configure your search parameters (keywords, location, date range, job type, etc.).
4. Save the task. Repeat for each additional search.

> **Note on date range:** Both actors support four windows: **1h**, **24h**, **7d**, and **6m** (all active jobs). The first three return full job descriptions; the 6-month window does not.

> **Note:** Use the short task name (e.g. `my-job-search-dc-dmv`) in `config.toml` — the ingestion script adds `username~` automatically.

### 5. Schedule each task

In each task's **Schedules** tab, create a schedule. `0 1,5,9,13,17,21 * * *` runs every 4 hours.

---

## Local setup

### 1. Clone the repo

```bash
git clone git@github.com:dballing/jobsearch.git
cd jobsearch
```

### 2. Set up the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create `config.toml`

```bash
cp config.toml.example config.toml
```

Minimal working config:

```toml
api_token = "apify_api_xxxxxxxxxxxxxxxxxxxx"
username  = "your-apify-username"

[labels]
dc = "DC/DMV"

[[tasks]]
name  = "my-job-search-dc-dmv"
label = "dc"
```

→ See **[Configuration reference](docs/configuration.md)** for all available options.

### 4. Run the first ingestion

```bash
./ingest.sh
```

### 5. Start the web UI

```bash
./run_app.sh
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

---

## Scheduled ingestion (cron)

```
0 1,5,9,13,17,21 * * * /path/to/jobsearch/ingest.sh >> /path/to/jobsearch/ingest.log 2>&1 && /path/to/jobsearch/rescore_viability.sh >> /path/to/jobsearch/viability.log 2>&1
```

Use the absolute path to `ingest.sh`. The script changes into its own directory, so relative paths in `config.toml` work correctly.

---

## Documentation

- **[Configuration reference](docs/configuration.md)** — all `config.toml` keys, tasks, labels, viability config, auto-ghost, generic tasks with per-schedule labels
- **[Features](docs/features.md)** — web UI, status reference, fuzzy dedup, manual linking, viability scoring, auto-skip, importing existing applications, known limitations

---

## Project structure

```
jobsearch/
├── app.py                   # Flask web application
├── ingest.py                # Apify ingestion script
├── ingest.sh                # venv wrapper for ingest.py
├── import_linkedin.py       # one-off import of jobs by LinkedIn URL/ID
├── import_linkedin.sh       # venv wrapper for import_linkedin.py
├── rescore_viability.py     # AI viability scoring script
├── rescore_viability.sh     # venv wrapper for rescore_viability.py
├── viability.py             # shared scoring helpers (prompt_hash, score_job)
├── run_app.sh               # venv wrapper for Flask
├── config.toml              # your local config (gitignored)
├── config.toml.example      # template
├── requirements.txt         # Python dependencies
├── jobs.db                  # SQLite database (gitignored)
├── TODO.md                  # known open issues and future ideas
├── docs/
│   ├── configuration.md     # full config reference
│   ├── features.md          # feature documentation
│   └── screenshot.png       # UI screenshot (used in README)
└── templates/
    ├── base.html            # base layout, navbar, offcanvas preview, stats modal
    └── jobs.html            # main jobs table with filters and column picker
```
