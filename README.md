# Job Search Tracker

A personal tool for ingesting job search results from multiple sources (via Apify) into a local SQLite database, and reviewing them through a Flask web UI.

![Job Search Tracker UI](docs/screenshot.png)

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

`ingest.sh` and `run_app.sh` require the `.venv` to exist and will exit with an error if it does not — run this step before scheduling cron or starting the app.

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
- `reset_on_change` — overrides the global `reset_on_change` default for this task. See global keys below.
- `fuzzy_dedup` — overrides the global `fuzzy_dedup` default for this task. See global keys below.

Global optional keys (top-level, not inside `[[tasks]]`):

- `reset_on_change` — `true` (default). Reset `skipped` jobs back to `new` if their description changes on a subsequent run. Set to `false` for tasks where job posters frequently make minor edits to re-surface listings, to avoid spurious resets. Per-task `reset_on_change` overrides this.
- `fuzzy_dedup` — `true` (default). Master switch: enables near-duplicate detection for all tasks. A per-task `fuzzy_dedup` key overrides this for that task, so you can opt individual tasks out (or in).
- `fuzzy_desc_threshold` — float between 0 and 1 (default: `0.85`). Minimum description similarity to consider two jobs near-duplicates. Only takes effect when fuzzy dedup is enabled for at least one task.
- `fuzzy_title_threshold` — float between 0 and 1 (default: `0.6`). Minimum title similarity used as a fast pre-filter before the (more expensive) description comparison. Lower this if you expect near-duplicates with substantially reworded titles; raise it to tighten the filter.
- `inherit_canonical_status` — `true` (default). When a new job is linked as a duplicate of an existing canonical job, inherit the canonical's current status (e.g. if you already marked it `skipped`, the duplicate starts as `skipped` too). Set to `false` to always start duplicates as `new`.

`config.toml` is gitignored so your API token is never committed.

### 4. Run the first ingestion

```bash
./ingest.sh
```

This fetches the latest results from each Apify task. You should see output like:

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
  - *New* — jobs not yet looked at
  - *Reviewing* — jobs you've opened but haven't decided on yet
  - *Active* — jobs not yet skipped, rejected, withdrawn, ghosted, or closed (default)
  - *Applied* — jobs currently in progress (applied, interviewing, offered, ghosted)
  - *All* — everything in the database
- **View**:
  - *Grouped* — near-duplicate jobs (linked via fuzzy dedup) are collapsed into a single row with expandable sub-rows; each un-linked job is its own row (default)
  - *Flat* — one row per posting

### Sorting

Click any column header to sort. Click again to reverse. Click a third time to return to the default sort. Sorting is case-insensitive.

### Tracking status

Each job has a status dropdown. Available statuses:

| Status | Meaning |
|--------|---------|
| `new` | Freshly ingested, not yet looked at |
| `skipped` | Not a fit — skip for now |
| `reviewing` | You've opened it but haven't decided yet — needs another look |
| `applied` | Application submitted |
| `rejected` | Rejected by employer |
| `ghosted` | Applied but never heard back — effectively closed from your side |
| `interviewing` | Active interview process |
| `offered` | Offer received |
| `withdrawn` | You withdrew your application |
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
- If the posting has expired (`date_validthrough` in the past) and the status is `new` or `reviewing`, the status is automatically set to `closed`.
- If the job description changed and the status was `skipped`, the status is reset to `new` (unless `reset_on_change = false` for that task). Jobs reset this way display a ↻ icon next to the title as a visual indicator that they are "new again" rather than freshly ingested. The icon clears the next time you manually change the status.

---

## Fuzzy near-duplicate detection

When the same job is posted on multiple platforms (LinkedIn and a career-site ATS), or when an employer posts nearly identical jobs under slightly different titles, you end up with duplicate entries in the database. The fuzzy dedup feature detects and groups these automatically.

### How it works

Enable it per task with `fuzzy_dedup = true`. On each new job ingested, the script:

1. Pre-filters all existing canonical jobs by title similarity > 60 % (fast upper-bound check).
2. Computes a full `SequenceMatcher` similarity ratio on the job description.
3. If the ratio meets `fuzzy_desc_threshold` (default 0.85), the new job is recorded as a duplicate of the existing canonical job — its `canonical_id` is set to the canonical's `job_id`.

No company filter is applied — the same job often appears under different company names when posted by recruiters or aggregators. Near-duplicate detection is cross-task: a new LinkedIn job is compared against all existing canonical jobs in the database, not just jobs from the same task.

### UI behavior

Fuzzy-linked jobs are **grouped together** in the Grouped view, exactly like multi-location postings. The group header shows:

- **"N similar postings"** if the canonical and duplicate(s) have different titles or companies.
- **"N postings of this title by this company"** if they share the same title and company (e.g. same job across multiple locations with near-identical descriptions).

Expand the group to see each posting individually with its own status dropdown and description preview.

### Status inheritance

When `inherit_canonical_status = true` (default), a newly ingested duplicate starts with the same status as its canonical job. If you already marked the LinkedIn version as `skipped`, the career-site version starts as `skipped` too, without any manual action.

### Notes

- Only jobs with `canonical_id IS NULL` (i.e. canonical jobs, not already-linked duplicates) are considered as potential matches, preventing chains.
- Fuzzy matching is CPU-bound. For large ingestion runs with many candidates per company, it can add noticeable time. Lower `fuzzy_desc_threshold` to cast a wider net; raise it to be more conservative.
- Existing jobs in the database before `fuzzy_dedup` was enabled are not retroactively linked — only new or re-ingested jobs are checked.

---

## Viability scoring

`rescore_viability.sh` uses the Anthropic API to rate each job posting as **high**, **medium**, or **low** viability for you as a candidate. Ratings and a one-sentence reason are stored in the database and shown as color-coded badges in the UI.

### Setup

1. Add a `[viability]` section to `config.toml`:
   ```toml
   [viability]
   enabled = true
   api_key = "sk-ant-xxxxxxxxxxxxxxxxxxxx"   # or set ANTHROPIC_API_KEY env var instead
   # model = "claude-haiku-4-5"              # optional, this is the default
   prompt = """
   I am a senior software engineer with 12 years of Python and cloud infrastructure
   experience (AWS/GCP). I am looking for IC or tech-lead roles at Series A–C startups
   or mid-size companies. I will not consider roles that are primarily Java, require
   relocation, or involve more than 10% travel.
   """
   ```
2. Add `anthropic` to your venv: `pip install anthropic` (or `pip install -r requirements.txt` after the next pull).

### Running

```bash
./rescore_viability.sh
```

Or chain it after ingestion in your cron entry:

```
0 1,5,9,13,17,21 * * * /path/to/jobsearch/ingest.sh && /path/to/jobsearch/rescore_viability.sh
```

Available flags:

| Flag | Effect |
|------|--------|
| `--dry-run` | Show how many jobs would be scored without scoring them |
| `--force` | Rescore all matching jobs even if their hash already matches the current prompt |
| `--all` | Also score closed/ghosted jobs (default: exclude them) |
| `--config PATH` | Use a different config file |

### How it works

- Each job is scored in a single Anthropic API call. The candidate description (your `prompt`) is sent as a cached system prompt, so only the first call in a session pays full token cost — subsequent calls reuse the cache.
- A SHA-256 hash of the prompt is stored alongside each score. On subsequent runs, only jobs with a missing or stale hash are re-scored. Change your `prompt` and re-run to update all scores.
- By default, closed, ghosted, and skipped jobs are excluded (they're not worth paying to score). Use `--all` to include them.

### UI

Once any jobs are scored, the UI shows:

- A **Viability** column with color-coded badges: <span style="color:green">high</span> · <span style="color:#856404">medium</span> · <span style="color:red">low</span>. Hover a badge to see the one-sentence reason.
- A **Viability filter** in the filter bar: All · High · Medium · Low · Unscored.
- The viability badge and reason also appear in the job description preview panel (offcanvas).

---

## Known limitations

### Displayed location may not match your search geography

Each job posting can include multiple locations. The **Location** column in the UI shows only the first location in that list (`locations[0]`), which is whatever order Apify received them in. This means a posting that matched your "Virginia" search filter might display "Billund, Region of South Denmark, Denmark" if that happened to be the first location in the raw data.

There is no reliable programmatic way to sort or prefer locations without knowing which one triggered the match. If a location looks wrong, hover over it to see the full list of locations for that posting, or click through to the original posting.

---

## Project structure

```
jobsearch/
├── app.py                   # Flask web application
├── ingest.py                # Apify ingestion script
├── ingest.sh                # venv wrapper for ingest.py
├── rescore_viability.py     # AI viability scoring script
├── rescore_viability.sh     # venv wrapper for rescore_viability.py
├── viability.py             # shared scoring helpers (prompt_hash, score_job)
├── run_app.sh               # venv wrapper for Flask
├── config.toml              # your local config (gitignored)
├── config.toml.example      # template
├── requirements.txt         # Python dependencies
├── jobs.db                  # SQLite database (gitignored)
└── templates/
    ├── base.html            # base layout, offcanvas preview panel
    └── jobs.html            # main jobs table
```
