# LinkedIn Job Search Tracker

A personal tool for ingesting nightly LinkedIn job search results (via Apify) into a local SQLite database, and reviewing them through a Flask web UI.

---

## How it works

1. You configure one or more Apify Actor tasks using the **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** Actor. Each task represents a geographic search (e.g., DC/DMV, North Carolina).
2. A cron job runs `ingest.sh` on a schedule, fetching the latest results from each task and inserting new jobs into a local SQLite database. Duplicate postings (same LinkedIn job ID) are detected and updated rather than re-inserted. Jobs that appear in multiple task runs accumulate labels from each.
3. You run the Flask app locally to browse, filter, sort, and track your application status for each job.

---

## Prerequisites

- Python 3.11+
- An [Apify](https://apify.com) account (free tier is sufficient for personal use)

---

## Apify setup

### 1. Create an Apify account

Sign up at [apify.com](https://apify.com). The free tier includes enough monthly compute to run several searches multiple times per day.

### 2. Find your API token

In the Apify Console, go to **Settings → Integrations → API tokens**. Copy your personal API token — you'll need it in `config.toml`.

### 3. Set up the Actor

Navigate to the **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** Actor in the Apify Store and click **Try for free** to open it.

### 4. Create a Task for each search

Rather than running the Actor directly, create a **Task** for each search so you can save your parameters and schedule runs.

1. In the Actor page, click **Create new Task**.
2. Give the task a descriptive name, e.g. `derek-job-search-dc-dmv`.
3. Configure your search parameters. Common fields include:
   - **Keywords** — job title(s) or skills
   - **Location** — geographic area, or leave blank for remote-only searches
   - **Date posted** — how far back to look; see note below
   - **Job type**, **Experience level**, **Remote/on-site**, etc.
4. Save the task.
5. Repeat for each additional search.

> **Note on date range:** The Actor supports four windows: **1h**, **24h**, **7d**, and **6m** (all active jobs). The first three return full job descriptions; the 6-month window does not — you'll get titles and companies but empty descriptions.

> **Note:** The task name you give in the Apify Console is what you'll use in `config.toml`. The format expected is the short name only (e.g., `derek-job-search-dc-dmv`), not the full `username~taskname` form — the ingestion script constructs that automatically.

### 5. Schedule each task

In each task's page, click the **Schedules** tab and create a schedule. A cron expression like `0 1,5,9,13,17,21 * * *` runs the task every 4 hours. Adjust to your preference.

---

## Local setup

### 1. Clone the repo

```bash
git clone git@github.com:dballing/linkedinsearch.git
cd linkedinsearch
```

### 2. Create `config.toml`

Copy the example and fill in your details:

```bash
cp config.toml.example config.toml
```

Edit `config.toml`:

```toml
api_token = "apify_api_xxxxxxxxxxxxxxxxxxxx"   # your Apify API token
username  = "your-apify-username"              # your Apify username
db_path   = "jobs.db"                          # path to SQLite database

[[tasks]]
name    = "derek-job-search-dc-dmv"   # Apify task name
label   = "dc"                         # short internal key stored in the database
display = "DC/DMV"                     # optional: pretty name shown in the UI filter bar

[[tasks]]
name    = "derek-job-search-north-carolina"
label   = "nc"
display = "NC"
```

Add as many `[[tasks]]` sections as you have searches. Each task requires a `label` — an arbitrary short string stored in the database that identifies which task a job came from (keep it consistent across ingestion runs). The `display` key is optional; if provided, it is shown as the button label in the UI filter bar. If omitted, the `label` is shown uppercased. You might use geographic tags (`dc`, `nc`), title tags (`pm`, `eng`), or any other dimension that helps you organize results.

`config.toml` is gitignored so your API token is never committed.

### 3. Run the first ingestion

```bash
./ingest.sh
```

This creates the virtual environment (if needed), installs dependencies, and fetches the latest results from each Apify task. You should see output like:

```
Starting ingestion at 2026-05-22 14:00:00 UTC
Fetching runs for 'derek-job-search-dc-dmv' (label: dc) ...
  Run 2026-05-22 14:00: 312 items retrieved
    291 inserted, 14 updated, 7 already existed
Done in 4.2s. 291 inserted, 14 updated, 7 unchanged.

```

### 4. Start the web UI

```bash
./run_app.sh
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

---

## Scheduled ingestion (cron)

To keep the database current automatically, add a cron job that runs `ingest.sh`. Edit your crontab with `crontab -e`:

```
0 1,5,9,13,17,21 * * * /path/to/linkedinsearch/ingest.sh >> /path/to/linkedinsearch/ingest.log 2>&1
```

Use the absolute path to `ingest.sh`. The script changes into its own directory before running, so relative paths in `config.toml` (e.g., `db_path = "jobs.db"`) work correctly.

---

## Using the web UI

### Filtering

- **Label**: filter to jobs from a specific task (or show all).
- **Status**: 
  - *Active* — jobs not yet skipped, rejected, withdrawn, or closed (default)
  - *Applied* — jobs currently in progress (applied, interviewing, offered)
  - *All* — everything in the database
- **View**:
  - *Grouped* — jobs with the same title and company are collapsed into a single row with expandable per-location sub-rows (default)
  - *Flat* — one row per posting

### Sorting

Click any column header to sort. Click again to reverse direction. Sorting is case-insensitive.

### Tracking status

Each job has a status dropdown. Available statuses:

| Status | Meaning |
|--------|---------|
| `new` | Freshly ingested, not yet reviewed |
| `reviewed` | You've looked at it but haven't decided |
| `applied` | Application submitted |
| `interviewing` | Active interview process |
| `offered` | Offer received |
| `rejected` | Rejected by employer |
| `withdrawn` | You withdrew your application |
| `skipped` | Not a fit — skip for now |
| `closed` | Posting expired or no longer accepting applications |

In grouped view, if all locations for a job share the same status, a group-level dropdown lets you update all of them at once.

> **Tip:** If a job you've marked `skipped` has its description updated by the employer, it will automatically be reset to `new` on the next ingestion run so you can take another look.

### Previewing job descriptions

Click the card icon (&#9783;) next to any job title to open a preview panel with the job description, without leaving the page or opening a new tab.

---

## Re-ingestion behavior

When a job already exists in the database and is seen again in a subsequent run:

- All mutable fields (title, company, location, salary, description) are refreshed with the latest data from Apify.
- The `first_seen` timestamp is preserved.
- If the job appears under a new label (from a different task), that label is added to its label list.
- If the posting has expired (`date_validthrough` in the past) and the status is `new` or `reviewed`, the status is automatically set to `closed`.
- If the job description changed and the status was `skipped`, the status is reset to `new`.

---

## Project structure

```
linkedinsearch/
├── app.py              # Flask web application
├── ingest.py           # Apify ingestion script
├── ingest.sh           # venv wrapper for ingest.py
├── run_app.sh          # venv wrapper for Flask
├── config.toml         # your local config (gitignored)
├── config.toml.example # template
├── requirements.txt    # Python dependencies
├── jobs.db             # SQLite database (gitignored)
└── templates/
    ├── base.html       # base layout, offcanvas preview panel
    └── jobs.html       # main jobs table
```
