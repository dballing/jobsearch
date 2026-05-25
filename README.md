# Job Search Tracker

A personal tool for ingesting job search results from multiple sources (via Apify) into a local SQLite database, and reviewing them through a Flask web UI.

---

## How it works

1. You configure one or more Apify Actor tasks using either:
   - **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** — LinkedIn job postings (default)
   - **[fantastic-jobs/career-site-job-listing-api](https://apify.com/fantastic-jobs/career-site-job-listing-api)** — career-site postings from 54+ ATS platforms (Greenhouse, Lever, Workday, Ashby, etc.)

   Each task represents a search (e.g., by geography, by title, by ATS platform). Multiple tasks can share a label to group results in the UI.
2. A cron job runs `ingest.sh` on a schedule, fetching the latest results from each task and inserting new jobs into a local SQLite database. Duplicate postings (same job ID) are detected and updated rather than re-inserted. Jobs that appear in multiple task runs accumulate labels from each.
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

### 3. Set up the Actor(s)

Navigate to the Actor(s) you want to use in the Apify Store and click **Try for free**:

- **[fantastic-jobs/advanced-linkedin-job-search-api](https://apify.com/fantastic-jobs/advanced-linkedin-job-search-api)** — LinkedIn postings
- **[fantastic-jobs/career-site-job-listing-api](https://apify.com/fantastic-jobs/career-site-job-listing-api)** — career-site postings across ATS platforms

### 4. Create a Task for each search

Rather than running the Actor directly, create a **Task** for each search so you can save your parameters and schedule runs.

1. In the Actor page, click **Create new Task**.
2. Give the task a descriptive name, e.g. `my-job-search-dc-dmv`.
3. Configure your search parameters. Some common fields:
   - **Keywords** — job title(s) or skills
   - **Location** — geographic area, or leave blank for remote-only searches
   - **Date posted** — how far back to look; see note below
   - **Job type**, **Experience level**, **Remote/on-site**, etc.
4. Save the task.
5. Repeat for each additional search.

> **Note on date range:** Both actors support four windows: **1h**, **24h**, **7d**, and **6m** (all active jobs). The first three return full job descriptions; the 6-month window does not — you'll get titles and companies but empty descriptions.

> **Note:** The task name you give in the Apify Console is what you'll use in `config.toml`. The format expected is the short name only (e.g., `my-job-search-dc-dmv`), not the full `username~taskname` form — the ingestion script constructs that automatically.

### 5. Schedule each task

In each task's page, click the **Schedules** tab and create a schedule. A cron expression like `0 1,5,9,13,17,21 * * *` runs the task every 4 hours. Adjust to your preference.

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

> `ingest.sh` and `run_app.sh` both create and activate the venv automatically if it doesn't exist, so this step is only strictly necessary if you want your editor or IDE to resolve packages before running either script.

### 3. Create `config.toml`

Copy the example and fill in your details:

```bash
cp config.toml.example config.toml
```

Edit `config.toml`:

```toml
api_token = "apify_api_xxxxxxxxxxxxxxxxxxxx"   # your Apify API token
username  = "your-apify-username"              # your Apify username
db_path   = "jobs.db"                          # path to SQLite database

# Map label keys to display names shown in the UI filter bar.
# If a label has no entry here it is shown uppercased.
[labels]
dc = "DC/DMV"
nc = "NC"

[[tasks]]
name  = "my-job-search-dc-dmv"      # Apify task name
label = "dc"                         # short key stored in the database

[[tasks]]
name  = "my-job-search-dc-dmv-career-sites"
label = "dc"                         # same label — joins the DC/DMV filter group
actor = "careersite"                 # use fantastic-jobs/career-site-job-listing-api
# exclude_ats_duplicates = true      # skip results already covered by the career-site task

[[tasks]]
name  = "my-job-search-north-carolina"
label = "nc"
```

Multiple tasks can share the same `label` — they contribute to the same filter group and their jobs accumulate that label. Labels represent the search dimension (geography, role type, etc.); use the Source filter in the UI to distinguish LinkedIn from career-site results within a label. The `[labels]` table maps each label key to a display name; any label without an entry is shown uppercased.

Per-task optional keys:

- `actor` — `"linkedin"` (default) or `"careersite"`. Set to `"careersite"` for tasks that use `fantastic-jobs/career-site-job-listing-api`. Career-site jobs are stored with a `cs_` prefix on their IDs to avoid collision with LinkedIn job IDs.
- `exclude_ats_duplicates` — `true` to skip LinkedIn results that the actor has flagged as duplicates of career-site postings. Useful when running both a LinkedIn and a career-site task for the same geography to avoid double-ingesting the same job. Skipped items are counted and reported in the ingestion log; in a steady state you'd expect the LinkedIn skip count to roughly equal the career-site insert count for the same run window.

`config.toml` is gitignored so your API token is never committed.

### 4. Run the first ingestion

```bash
./ingest.sh
```

This creates the virtual environment (if needed), installs dependencies, and fetches the latest results from each Apify task. You should see output like:

```
Starting ingestion at 2026-05-22 14:00:00 UTC
Fetching runs for 'my-job-search-dc-dmv' (label: dc, actor: linkedin) ...
  Run 2026-05-22 14:00: 312 items retrieved
    291 inserted, 14 updated, 7 already existed, 82 ATS duplicates skipped
Fetching runs for 'my-job-search-dc-dmv-career-sites' (label: dc, actor: careersite) ...
  Run 2026-05-22 14:00: 88 items retrieved
    82 inserted, 4 updated, 2 already existed
Done in 5.1s. 373 inserted, 18 updated, 9 unchanged, 82 ATS duplicates skipped.

```

### 5. Start the web UI

```bash
./run_app.sh
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

---

## Scheduled ingestion (cron)

To keep the database current automatically, add a cron job that runs `ingest.sh`. Edit your crontab with `crontab -e`:

```
0 1,5,9,13,17,21 * * * /path/to/jobsearch/ingest.sh >> /path/to/jobsearch/ingest.log 2>&1
```

Use the absolute path to `ingest.sh`. The script changes into its own directory before running, so relative paths in `config.toml` (e.g., `db_path = "jobs.db"`) work correctly.

---

## Using the web UI

### Filtering

- **Label**: filter to a specific search dimension (geography, role type, etc.), or show all.
- **Source**: filter to LinkedIn or career-site results only. Appears automatically when both sources are present in the database.
- **Status**:
  - *New* — jobs not yet reviewed
  - *Active* — jobs not yet skipped, rejected, withdrawn, or closed (default)
  - *Applied* — jobs currently in progress (applied, interviewing, offered)
  - *All* — everything in the database
- **View**:
  - *Grouped* — jobs with the same title and company are collapsed into a single row with expandable per-location sub-rows (default)
  - *Flat* — one row per posting

### Sorting

Click any column header to sort. Click again to reverse. Click a third time to return to the default sort. Sorting is case-insensitive.

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

Click the card icon (&#9783;) next to any job title to open a side panel with the full job description. The **View Job** button at the bottom links to the original posting.

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
jobsearch/
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
