# Configuration Reference

All configuration lives in `config.toml` (gitignored — never committed). Copy `config.toml.example` to get started.

## Top-level keys

```toml
api_token = "apify_api_xxxxxxxxxxxxxxxxxxxx"   # Apify API token (required)
username  = "your-apify-username"              # Apify username (required)
db_path   = "jobs.db"                          # path to SQLite database (default: jobs.db)
```

## Labels

Map short label keys to display names shown in the UI filter bar. Any label without an entry is shown uppercased.

```toml
[labels]
dc = "DC/DMV"
nc = "NC"
```

## Tasks

Each `[[tasks]]` entry defines one Apify task to ingest from.

```toml
[[tasks]]
name  = "my-job-search-dc-dmv"   # Apify task name (short form)
label = "dc"                      # label key stored in the database
```


Multiple tasks can share the same `label` — they contribute to the same filter group. Use the Source filter in the UI to distinguish LinkedIn from career-site results within a label.

### Per-task keys

| Key | Default | Description |
|-----|---------|-------------|
| `name` | *(required)* | Apify task name (short form — the ingestion script adds `username~` automatically) |
| `label` | *(required unless `label_from_input` is set)* | Short key stored in the database |
| `actor` | `"linkedin"` | `"linkedin"` or `"careersite"`. Use `"careersite"` for `fantastic-jobs/career-site-job-listing-api`. Career-site jobs get a `cs_` ID prefix to avoid collision. |
| `label_from_input` | *(unset)* | Read the label from a named field in each run's INPUT record. See [Generic tasks](#generic-tasks-with-per-schedule-labels) below. |
| `exclude_ats_duplicates` | `false` | Skip LinkedIn results the actor has flagged as duplicates of career-site postings. Useful when running parallel LinkedIn + career-site tasks for the same geography. |
| `reset_on_change` | *(global value)* | Per-task override of the global `reset_on_change` setting. |
| `fuzzy_dedup` | *(global value)* | Per-task override of the global `fuzzy_dedup` setting. |

### Generic tasks with per-schedule labels

Instead of creating one Apify task per search variation, create a single generic task and drive the label from per-schedule input overrides. This reduces maintenance: add the task N times to one schedule, each entry with its own bespoke input overrides.

**(Sample) Apify schedule input override (per entry):**
```json
{
  "locationSearch": ["Virginia, United States", "Washington, District of Columbia, United States"],
  "locationExclusionSearch": ["West Virginia, United States"],
  "_jobsearch_label": "dc"
}
```

**`config.toml`:**
```toml
[[tasks]]
name             = "my-generic-linkedin"
label            = "unknown"          # fallback if field not found in run input
label_from_input = "_jobsearch_label"

[[tasks]]
name             = "my-generic-careersite"
label            = "unknown"
label_from_input = "_jobsearch_label"
actor            = "careersite"
```

The ingest script fetches each run's INPUT record from Apify and extracts the label field. The field is passed through to the actor, which silently ignores it. Existing tasks with a hardcoded `label` and no `label_from_input` are unaffected.

## Global keys

These sit at the top level of `config.toml` (not inside `[[tasks]]`).

| Key | Default | Description |
|-----|---------|-------------|
| `reset_on_change` | `true` | Reset `skipped`/`autoskipped` jobs back to `new` if their description changes. Set `false` for tasks where employers frequently make minor edits. Per-task `reset_on_change` overrides this. |
| `auto_ghost` | `false` | Automatically move `applied` jobs to `ghosted` when they've been waiting longer than `auto_ghost_days`. Only affects `applied` — `interviewing` and later statuses are intentionally excluded. |
| `auto_ghost_days` | `180` | Number of days since `applied_at` before auto-ghosting. |
| `fuzzy_dedup` | `true` | Master switch for near-duplicate detection. Per-task `fuzzy_dedup` overrides this. |
| `fuzzy_desc_threshold` | `0.85` | Minimum description similarity (0–1) to consider two jobs near-duplicates. |
| `fuzzy_title_threshold` | `0.6` | Minimum title similarity used as a fast pre-filter before the description check. |
| `inherit_canonical_status` | `true` | When a new job is linked as a duplicate, inherit the canonical's current status. Set `false` to always start duplicates as `new`. |

## Viability scoring (`[viability]`)

```toml
[viability]
enabled = true
api_key = "sk-ant-xxxxxxxxxxxxxxxxxxxx"   # or set ANTHROPIC_API_KEY env var
model   = "claude-sonnet-4-6"             # optional; default: claude-haiku-4-5
prompt  = """
Describe yourself as a candidate…
"""

# Optional auto-skip (disabled by default):
auto_skip            = false
auto_skip_confidence = "low"   # "low" (only low) or "medium" (low + medium)
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable viability scoring. |
| `api_key` | *(env)* | Anthropic API key. Falls back to `ANTHROPIC_API_KEY` environment variable. |
| `model` | `"claude-haiku-4-5"` | Anthropic model to use. |
| `prompt` | *(required)* | Your candidate description. Be specific: background, target roles, deal-breakers. |
| `auto_skip` | `false` | Automatically set `new`/`reviewing` jobs to `autoskipped` if they score at or below the threshold. |
| `auto_skip_confidence` | `"low"` | Threshold: `"low"` skips only low-scored jobs; `"medium"` skips low and medium. |

See [Features → Viability scoring](features.md#viability-scoring) for usage details.
