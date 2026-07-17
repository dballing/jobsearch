# Configuration Reference

All configuration lives in `config.toml` (gitignored — never committed). Copy `config.toml.example` to get started.

## Top-level keys

```toml
api_token   = "apify_api_xxxxxxxxxxxxxxxxxxxx" # Apify API token (required)
username    = "your-apify-username"            # Apify username (required)
db_path     = "jobs.db"                        # path to SQLite database (default: jobs.db)
uploads_dir = "uploads"                        # dir for job file attachments (default: uploads)
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

## AI engine settings (`[ai]`)

The AI-backed features (viability scoring and description reformatting) share one
engine configuration. Put the Anthropic key and default model here once:

```toml
[ai]
api_key = "sk-ant-xxxxxxxxxxxxxxxxxxxx"   # or set ANTHROPIC_API_KEY env var
model   = "claude-haiku-4-5"              # default model for all AI features
```

| Key | Default | Description |
|-----|---------|-------------|
| `api_key` | *(env)* | Anthropic API key. Falls back to `ANTHROPIC_API_KEY`. |
| `model` | `"claude-haiku-4-5"` | Default model; a feature section may override it. |

**Resolution order** for each feature: a value in the feature's own section wins,
then `[ai]`, then the built-in default / `ANTHROPIC_API_KEY`. This is backward
compatible — an `api_key`/`model` left under `[viability]` still works as an override.

## Viability scoring (`[viability]`)

```toml
[viability]
enabled = true
model   = "claude-sonnet-5"     # optional: override [ai].model for scoring
prompt  = """
Describe yourself as a candidate…
"""

# Optional geographic preferences (see below):
location_prompt = """
I currently reside in Alexandria, Virginia.
PREFERRED: DC Metro / Northern Virginia; also fully remote.
GOOD: Raleigh/RTP and elsewhere in North Carolina.
ACCEPTABLE: South Carolina.
POOR: on-site/hybrid whose only locations are outside VA/DC/NC/SC, unless fully remote.
If a posting's ONLY location is an entire country ("United States") with no state or city,
assume it falls within my target areas (my searches are already geographically pre-filtered)
and treat it as at least ACCEPTABLE.
"""
location_model = "claude-haiku-4-5"   # optional; defaults to [ai].model
location_use_description = true        # optional; default true (see below)

# Optional auto-skip (disabled by default):
auto_skip            = false
auto_skip_confidence = "low"   # "low" (only low) or "medium" (low + medium)
```

> **Recommended: set `[viability].model` to a capable model such as `claude-sonnet-5`.**
> The global default (`[ai].model`) is `claude-haiku-4-5` on purpose — it's the cheapest model,
> and the other AI feature (description reformatting) is high-volume, low-judgment work where
> Haiku is the right call. Viability scoring is different, and it's worth spending a little more
> here for three reasons:
> 1. **The rating is a genuine judgment, not a classification.** Weighing scope, seniority, comp,
>    industry, and deal-breakers against a detailed candidate profile rewards a stronger model
>    with more discerning, better-calibrated ratings and reasons; Haiku is comparatively blunt.
> 2. **It sets the geographic sub-call's model when `location_use_description` is on.** That
>    sub-call then reads full job descriptions, and Haiku mis-reads them — it false-`POOR`s
>    fully-remote roles whose descriptions merely mention other regions (which then get clamped to
>    `low`), where Sonnet correctly tells a real "remote only for residents of X" restriction from
>    incidental prose. Setting a capable `model` fixes scoring *and* geography in one place.
> 3. **The cost delta is small.** The candidate profile (the bulk of the tokens) is prompt-cached,
>    so after the first call a full rescore is cheap, and rescores are incremental. The quality
>    gain far outweighs the few extra cents. (If you'd rather keep scoring on Haiku, set
>    `location_model` to a capable model instead, so at least the description-aware geo call is
>    accurate.)

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable viability scoring. |
| `api_key` | *(from `[ai]`)* | Optional per-feature override of the Anthropic key. |
| `model` | *(from `[ai]`)* | Optional per-feature override of the model. |
| `prompt` | *(required)* | Your candidate description. Be specific: background, target roles, deal-breakers. |
| `location_prompt` | *(none)* | Your geographic/remote preferences. When set, a focused single-purpose AI call matches each job's location(s) against this and feeds only the verdict — one of four ordinal tiers `PREFERRED` > `GOOD` > `ACCEPTABLE` > `POOR` — to the main scorer, which reads geography far more reliably than parsing a multi-city list inline. The tier names are generic; **your** prompt decides which locations earn which tier. Put **all** location/remote judgments here and keep geography out of `prompt` so it isn't double-judged. Editing it re-scores every job (it's folded into the staleness hash). The sub-call also reads each job's **description**, so it honors eligibility conditions in the prose (e.g. a "remote" role that only accepts residents of certain states) — **include where you live** so it can tell whether such conditions include you. A **`POOR`** verdict — the bottom tier, meaning no listed location or remote option the candidate can actually work — **deterministically forces the overall viability to `low`** (geography is a hard disqualifier); the main scorer would otherwise discount it and still return `medium`. The other three tiers flow to the scorer as advisory context. When this override fires, the score's reason keeps the model's own explanation with a bracketed `[Forced to LOW: …]` note appended. *Tip:* if your ingest tasks already restrict searches by geography, you can tell the prompt to assume a bare-country location (`"United States"`) is in-area — the feed wouldn't have surfaced it otherwise. |
| `location_model` | *(auto)* | Optional model for the location sub-call. When set, it always wins. When unset, the default **depends on `location_use_description`**: with the description off, the sub-call is a trivial location match, so it uses the cheap `[ai]` model even when `model` above is pricier; with the description on, reading the prose to tell a real eligibility restriction from incidental office/regional wording is a comprehension task the cheap model gets wrong (it false-`POOR`s fully-remote roles), so it **escalates to the model viability scoring uses** — i.e. `[viability].model` if set, else `[ai].model` (so the escalation only buys a better sub-call when your scoring model is more capable than `[ai].model`; if your whole setup is on one cheap model, set `location_model` here). Set this explicitly to override either default. |
| `location_use_description` | `true` | Whether the location sub-call reads each job's description. On (default), it honors eligibility conditions in the prose — e.g. a "remote" role restricted to residents of certain states — but must run per unique description **and needs a capable model** (see `location_model`: the default auto-escalates to your viability `model`; a cheap model like Haiku mis-reads noisy descriptions and false-`POOR`s remote jobs). Off, it dedups hard by location set (fewer calls, cheaper, cheap model is fine) but can't see those conditions. Folded into the staleness hash, so flipping it re-scores. |
| `auto_skip` | `false` | Automatically set `new`/`reviewing` jobs to `autoskipped` if they score at or below the threshold. |
| `auto_skip_confidence` | `"low"` | Threshold: `"low"` skips only low-scored jobs; `"medium"` skips low and medium. |

See [Features → Viability scoring](features.md#viability-scoring) for usage details.

## AI description reformatting (`[descriptions]`)

Optional. At ingest time, hand each job description to the model and store a cleaned
**Markdown** version that the UI renders instead of the built-in regex formatter.
**Formatting only — never content** (verified per job; see below). Requires the
`markdown` and `bleach` packages and an `[ai]` key.

```toml
[descriptions]
use_ai_on_descriptions = true
# model = "claude-haiku-4-5"   # optional: override [ai].model for reformatting
```

| Key | Default | Description |
|-----|---------|-------------|
| `use_ai_on_descriptions` | `false` | Enable AI reformatting at ingest. |
| `api_key` / `model` | *(from `[ai]`)* | Optional per-feature overrides. |

Notes:
- **Scope:** only new jobs and jobs whose description changed are formatted (no
  backfill of existing rows). Existing rows keep the regex renderer until they change.
- **Integrity check:** the AI output is accepted only if its text content matches the
  original (a normalized-token similarity check). On failure — or any API error, or if
  `markdown`/`bleach` aren't installed — it silently falls back to the regex renderer.
- **Cost:** spends tokens per formatted description (logged in the ingest run summary,
  with token counts and an estimated `$`). Byte-identical descriptions are formatted
  once and reused (within a run and across runs), so the same posting in many locations
  costs a single call.

See [Features → Description rendering](features.md#previewing-job-descriptions) for
how the formatted version is displayed.

## Company name normalization (`[company_aliases]`)

Optional. Feeds spell the same employer inconsistently (e.g. `Sirius XM` vs
`Sirius XM Radio`). This table maps each variant spelling to the canonical name to
store; the rewrite happens at ingest, so grouping, employer search, viability, and
display all use one consistent name.

```toml
[company_aliases]
"Sirius XM"       = "SiriusXM"
"Sirius XM Radio" = "SiriusXM"
```

- Keys are variant spellings, values the canonical name. Quote names containing spaces.
- Matching is **case-insensitive** and **exact** on the whole company field (after
  trimming) — not substring or fuzzy. The canonical value is stored with the exact
  casing you write here.
- Applied to **newly ingested and re-seen** jobs only — there is no bulk rewrite of
  existing rows. A job already stored under an old spelling is normalized the next time
  its posting reappears in a feed.
- Aliases are **not chained**: map every variant directly to the final name (an `X → Y`
  and `Y → Z` pair does not turn `X` into `Z`).
- This table is also **written by the web app**: the preview panel's "change the
  underlying company name" option adds an entry here (with an `# Added YYYY-MM-DD via web
  app.` end-of-line comment) and rewrites the existing rows in one step. It re-emits the
  block in a tidy, sorted style — entries **grouped by canonical** (an employer's variants
  together), canonicals **A→Z**, and the `=` and EOL comments each **column-aligned** — and
  touches nothing outside the table, so your other keys, comments, and the API key are left
  as-is. (Editing by hand still works; the next web-app add just re-tidies the block.)

See [Features → Company name normalization](features.md#company-name-normalization).
