# Configuration Reference

All configuration lives in `config.toml` (gitignored — never committed). Copy `config.toml.example` to get started.

**Live reload.** The running web app re-reads `config.toml` (and any per-search files) whenever you save an edit — changes take effect on the **next request**, no restart needed. This covers everything display- and scoring-related: search names, labels, viability prompts, feeds, aliases, AI settings. A half-saved or malformed file is ignored (the app keeps serving the last good config and picks up your fix automatically once it parses). **Two exceptions need a restart:** `db_path` and `uploads_dir` — they bind the running process to an open database and attachment directory that can't be swapped mid-flight. Edit one of those and the app shows a **sticky banner** telling you to restart; the banner clears itself once you do (or once you revert the value). Batch tools (`ingest.sh`, `rescore_viability.sh`) read the config fresh on every run, so reload doesn't apply to them.

## Top-level keys (`[basics]`)

Shared, app/DB-wide settings live under a `[basics]` table. (An older layout put these bare at the top level — still accepted, with a deprecation warning; run `python ingest.py --fixbasics` to migrate an existing config in place, comments preserved.)

```toml
[basics]
api_token   = "apify_api_xxxxxxxxxxxxxxxxxxxx" # Apify API token (required)
username    = "your-apify-username"            # Apify username (required)
db_path     = "jobs.db"                        # path to SQLite database (default: jobs.db)
uploads_dir = "uploads"                        # dir for job file attachments (default: uploads)
```

## Multiple job searches (`[[searches]]`)

By default `config.toml` describes **one** search. To run several distinct searches — each with its own `[viability]` criteria and its own feeds — in one app and one database, add a `[[searches]]` manifest and move the **per-search** stanzas (`[viability]`, `[[tasks]]`, `[labels]`) into per-search files. The **shared** stanzas (`[basics]`, `[company_aliases]`, and optionally `[ai]`/`[descriptions]`) stay in the canonical `config.toml`; each per-search file inherits them and may not redeclare a global stanza.

```toml
# config.toml — shared globals + the search manifest
[basics]
db_path = "jobs.db"
# … api_token, username, uploads_dir, dedup knobs …

[company_aliases]        # global (written at runtime by the app) → must live here
"Sirius XM" = "SiriusXM"

[[searches]]
search_id          = "tpm"                 # PERMANENT primary key — never rename (see note below)
search_name        = "TPM / Program Mgmt"  # shown in the UI's search selector
search_config_file = "searches/tpm.toml"   # holds [viability], [[tasks]], [labels]
adopts_legacy      = true                  # one-time: this search absorbs the pre-split jobs
                                           # of a DB that began as a single search (at most one)

[[searches]]
search_id          = "director"
search_name        = "Director of Eng/IT"
search_config_file = "searches/director.toml"
```

```toml
# searches/tpm.toml — per-search stanzas only
[viability]
enabled = true
prompt  = """…TPM-specific candidate framing…"""

[[tasks]]
name  = "apify-linkedin-tpm"
label = "dc"
```

Behavior:
- **Per-lens everything.** Status, viability, the salary/geo overrides, and event history are tracked separately per `(job, search)`; the same posting is one row in the jobs table with independent state under each search. Notes, attachments, and the description override are shared (you apply once).
- **The UI** shows one search ("lens") at a time — a selector appears in the navbar; switching sets `?search=<id>` (and a sticky cookie). The selector also offers **"All searches"**, a read-only combined view of every lens at once: a flat, un-grouped list with a **Search** column tagging each row's lens with a stable, color-coded badge (each search keeps its own hue). Because status/viability are per-lens, a posting tracked under two searches appears **once per search** — the row unit is the `(job, search)` tuple, so the same job can read *applied* in one row and *skipped* in another. Per-row status changes and preview-panel edits still work and target that row's own lens; page-wide actions with no single target lens (manual **Add job**, the **bulk-skip**) are hidden until you pick a concrete search. The sentinel is a view only — it is never a write target.
- **Scoring:** `./rescore_viability.sh` scores **every** search (each under its own criteria, in its own child process — the writer lock is process-scoped, so it fans out rather than looping in-process). `--search <id>` narrows to one. `./ingest.sh` already ingests all searches in one run.
- **Dedup:** automatic near-duplicate grouping never crosses searches; a manual merge/promote in the UI may.
- **Going from one search to many:** set `adopts_legacy = true` on the search that should inherit your existing (pre-split) jobs; the next ingest/rescore/app start folds them in automatically (idempotent).
- **`search_id` is permanent; never rename it.** It's the primary key stored in every `job_search_state` row and prefixed onto every run-tracking key. Renaming it in config does **not** migrate those rows — it orphans them: the search then shows up empty (its status, viability, overrides, and history all gone) and every task re-ingests its full backlog. If you truly must change one, it's a manual DB migration (`UPDATE`ing `job_search_state.search_id` plus the `ingest_state`/`ingest_history` key prefixes), not a config edit. `search_name` (the display label) is free to change anytime.
- **Task `name`s, by contrast, are safe to rename** — including renaming on the Apify side, renaming back, or reusing an old name for a new task. Run-tracking keys off the immutable Apify run_id, not the task name, so an already-processed run is never re-ingested and a genuinely new run always is. (One consequence: within a single search, the same run is processed once regardless of which task name it arrived under.)

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
| `schedule_name` | *(unset)* | Ingest only the runs that this Apify **schedule** triggered. Lets two searches share one Apify task, split by schedule. The schedule's console **name** (an opaque schedule id also works). See [One Apify task, two schedules](#one-apify-task-two-schedules) below. |

### One Apify task, two schedules

Two **searches** (lenses) can share a **single** Apify task definition when the only difference between them is the input a schedule injects — e.g. one scraper invoked by a US schedule and a Europe schedule that pass different location limiters, each feeding a different search. Apify lists runs per *task*, not per *schedule*, so a shared task's run list mixes both schedules' runs; there is no server-side "runs of schedule X" query. Set `schedule_name` on each search's task entry to filter its ingest down to only the runs that schedule triggered — matched on the run's platform-stamped `meta.scheduleId` (already present on the run-list response, so no extra API calls). Without it, **both** searches would ingest **both** schedules' postings.

```toml
# searches/midatl_tpm.toml
[[tasks]]
name          = "derek-tpm-scraper"          # the SHARED Apify task
label         = "tpm"
schedule_name = "job-search-schedule-usa"

# searches/europe_tpm.toml
[[tasks]]
name          = "derek-tpm-scraper"          # same Apify task
label         = "tpm"
schedule_name = "job-search-schedule-europe"
```

`schedule_name` is the schedule's name as shown in the Apify console. Apify stamps each run with the schedule's opaque *id* (in `meta.scheduleId`), not its name, so ingest fetches the account's schedules once per run and resolves the name → id before matching. (An opaque id in the field also works and skips a lookup path; the value is tried as an id first, then as a name.)

A run whose schedule doesn't match is skipped and recorded as seen for *this* search only (so it isn't re-probed each cycle); the sibling search that owns that schedule still ingests it under its own history. Leaving `schedule_name` unset preserves the default single-schedule behavior (every run of the task is ingested).

**Adding a scoped search to a task with history — seed it first.** Resolving a run's schedule costs one API call per run (`scheduleId` is on the run *detail*, not the run *list*). A brand-new search has no ingest history, so it would scan the task's *entire* run backlog one run at a time — slow, and usually pointless (you added the lens to track its schedule going forward, not to dredge a mostly-foreign back-catalog). Run `ingest.py --seed <search_id>` once: it marks the search's current runs all-seen (list-only, no per-run fetch — instant) so the search starts from *now* and only processes runs created afterward. A scoped task also prints a `--seed` hint if a normal ingest is about to probe a large batch.

**Troubleshooting a `0 of N run(s)` dry run:** if scoping matches nothing, ingest prints the schedule ids actually present on that task's runs (`NOTE: … Schedule ids present: <id> (count), …`), and — if the name itself wasn't found among your account's schedules — a `WARNING` listing the known schedule names. Cross-check the spelling against the console.

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

These live under `[basics]` in `config.toml` (not inside `[[tasks]]`; bare top-level placement is still accepted but deprecated — see the top of this doc). Per-task overrides where noted.

| Key | Default | Description |
|-----|---------|-------------|
| `reset_on_change` | `true` | Reset `skipped`/`autoskipped` jobs back to `new` if their description changes. Set `false` for tasks where employers frequently make minor edits. Per-task `reset_on_change` overrides this. |
| `auto_ghost` | `false` | Automatically move `applied` jobs to `ghosted` when they've been waiting longer than `auto_ghost_days`. Only affects `applied` — `interviewing` and later statuses are intentionally excluded. |
| `auto_ghost_days` | `180` | Number of days since `applied_at` before auto-ghosting. |
| `fuzzy_dedup` | `true` | Master switch for near-duplicate detection. Per-task `fuzzy_dedup` overrides this. |
| `fuzzy_desc_threshold` | `0.85` | Minimum description similarity (0–1) to consider two jobs near-duplicates. |
| `fuzzy_title_threshold` | `0.6` | Minimum title *character* similarity used as a fast pre-filter before the description check. |
| `fuzzy_title_word_threshold` | `0.6` | Minimum title *word-overlap* (Jaccard on lowercased alnum tokens) required for a near-duplicate. Rejects distinct roles that share a tail phrase but differ by a qualifier — e.g. "Engineering Project Manager" vs "Technical Project Manager" (0.5) — even when their descriptions are near-identical. Suffix/reorder variants ("Software Engineer" vs "Software Engineer - Remote", 0.67) still merge. |
| `fuzzy_title_id_gate` | `true` | When both titles carry a req/posting-ID code and the codes differ (e.g. `[AQ-14258]` vs `[AQ-15000]`, `Req 14258` vs `#15000`, `L5` vs `L4`), treat them as different requisitions and never merge — even with byte-identical descriptions. A shared code, or one side lacking a code, falls through to the normal gates. A "code" is a title token that mixes letters and digits or contains a 4+-digit run; bare short numbers ("Level 3") are ignored. Set `false` to disable. |
| `inherit_canonical_status` | `true` | When a new job is linked as a duplicate, inherit the canonical's current status. Set `false` to always start duplicates as `new`. |

## AI engine settings (`[ai]`)

The AI-backed features (viability scoring and description reformatting) share one
engine configuration. Put the Anthropic key and default model here once:

```toml
[ai]
api_key = "sk-ant-xxxxxxxxxxxxxxxxxxxx"   # or set ANTHROPIC_API_KEY env var
model   = "claude-haiku-4-5"              # default model for all AI features
effort  = "medium"                        # default thinking effort (reasoning models only)
```

| Key | Default | Description |
|-----|---------|-------------|
| `api_key` | *(env)* | Anthropic API key. Falls back to `ANTHROPIC_API_KEY`. |
| `model` | `"claude-haiku-4-5"` | Default model; a feature section may override it. |
| `effort` | `"medium"` | Default thinking effort (`low`/`medium`/`high`/`xhigh`/`max`); a feature section may override it. **Applies only to reasoning models** — see below. |

**Resolution order** for each feature: a value in the feature's own section wins,
then `[ai]`, then the built-in default / `ANTHROPIC_API_KEY`. This is backward
compatible — an `api_key`/`model` left under `[viability]` still works as an override.

### Model pricing

Pricing lives in **`model_pricing.json`** (tracked, shipped) rather than in code, so a model can
be priced without a code change. Rates are USD per *million* tokens; only `input` and `output`
are stored, and the two cache rates are derived from input by a multiplier — `cache_write_mult`
(default 1.25) and `cache_read_mult` (default 0.10). The 0.1× is standard but not universal:
Fable/Mythos 5.1 bill cache hits at 0.025×, per a *footnote* on the pricing page rather than a
column, which is why the multiplier is overridable per model.

**Prices are temporal.** A model maps to either a single rate object — shorthand for "this
price, for all time" — or a *list* of periods, each optionally carrying `effective_start` and
`effective_until` (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`, UTC):

```json
"claude-sonnet-5": [
  { "input": 3.00, "output": 15.00, "effective_until": "2026-09-23" },
  { "input": 2.00, "output": 10.00, "effective_start": "2026-09-23" }
]
```

Periods are **half-open** `[start, until)`, so one ending on 2026-09-23 and the next starting
that day don't overlap. Both failure shapes are fatal at load: an **overlap** makes the rate at
an instant ambiguous, and a **gap** makes it unknown — which is the same failure as not knowing
today's price, and we already refuse to run on that. Inventing a neighbouring period's number
would be worse than refusing.

This is what makes a mispriced ledger repairable. `cost_usd` is a *cache* — every AI row stores
the model and all four token counts — so `./reprice.sh` recomputes each row at the rate in
effect at **its own** timestamp. See [Repricing](#repricing) below.

> It's JSON rather than TOML because `.gitignore` carries a blanket `*.toml` — the rule that
> keeps the secret-bearing `config.toml` and every `searches/*.toml` out of the repo. The shipped
> table has to be tracked, and adding a negation to that glob would weaken the protection for
> every other file it covers.

**Adding a model yourself: `model_pricing.local.json`.** Anthropic ships models faster than this
repo does. Drop a file next to the shipped one (gitignored; override the path with
`JOBSEARCH_MODEL_PRICING`) using the same shape, and it merges **per model** over the shipped
table:

```json
{
  "models": {
    "claude-sonnet-5-5": { "input": 2.00, "output": 10.00 },
    "claude-haiku-4-5":  { "input": 0.80, "output": 4.00,
                           "suppress_drift_warning": true,
                           "note": "negotiated rate" }
  }
}
```

A local entry **replaces** the shipped one outright rather than patching individual fields, so an
override always describes a complete, coherent price — a half-inherited hybrid (new input rate,
stale cache multiplier) would be silently wrong with nothing to catch it. Edits are picked up
without a restart. A malformed file is a hard error, not a fall-back to shipped prices: quietly
billing at rates you deliberately overrode is the failure this is meant to prevent.

> **Overriding a model means owning its whole price history.** The replacement is per *model*,
> not per period — splicing your periods into ours would produce a timeline nobody authored, and
> the resulting hybrid would be wrong with nothing to catch it. So if you override
> `claude-sonnet-foo`, it's on you to know that model's pricing history: declaring only
> `{"effective_start": "<today>"}` silently orphans every ledger row written before then, and
> `./reprice.sh` will refuse to run rather than guess what they cost. Consult the
> [canonical pricing table](https://platform.claude.com/docs/en/about-claude/pricing) if you need
> the historical rates, and declare a period covering each era you care about. The batch entry
> points detect the common version of this mistake and print a warning naming the model, the date
> your override starts, and the history it dropped — but the warning is a safety net, not a
> substitute for writing the timeline correctly.

Every run that can spend money **discloses** which models are locally priced — `ingest.py` and
`rescore_viability.py` print a `NOTE:` line at startup, and the stats modal says so next to the
cost figures. `suppress_drift_warning` turns off the *comparison warnings* described below (a
negotiated rate has no reason to match the public page); it does **not** suppress that
disclosure, because a forgotten override quietly distorting months of cost figures is exactly
what the disclosure is for.

**The drift check.** `tests/test_pricing_live.py` diffs `model_pricing.json` against Anthropic's
published pricing page and **fails** on a real mismatch, so the shipped table can't go stale.
Local overrides only ever produce **warnings**, never failures — a deliberate override (a
pre-announcement price, a negotiated deal) must not be able to redden the suite. Two warnings
exist: an override that *disagrees* with the published page (usually a typo, occasionally
intentional), and one that has become *redundant* because the shipped table caught up. The
redundant case matters more than it looks: the override keeps winning, so the next time that
price changes and the shipped table is corrected, your stale local copy silently overrides the
correction.

**Every configured model must be priced, or the run refuses to start.** `ingest.py` and
`rescore_viability.py` check each search's models after loading config and exit before spending
anything; the app's on-demand rescore refuses that one request the same way (it keeps serving,
since config is hot-reloaded). An unpriced model still scores jobs, but its
[spend ledger](features.md) rows are written at $0 and are indistinguishable from genuinely free
ones afterwards — and because rows are priced at call time, the real cost can't be reconstructed
later. The error names the model, the config key and the search, and gives both fixes. A **dated
snapshot** (`claude-sonnet-5-20260101`) needs no entry: it's the same model pinned, so it
inherits its base model's rates. A **point release** (`claude-sonnet-5-5`) does need one — it's a
distinct model that may bill differently, so it is never allowed to inherit its predecessor's
numbers.

### Repricing

When a price changes — or when you discover one changed weeks ago — the ledger can be repaired,
because `cost_usd` is derived from data every row already stores:

```bash
./reprice.sh --dry-run          # show what would move, per lens and model
./reprice.sh                    # apply
./reprice.sh --model claude-sonnet-5 --search europe_tpm   # narrow
```

The sequence is: close the old period and add the new one with the **real effective date from
the announcement**, dry-run, then apply. Each row is recomputed at the rate effective at its own
`ts`, so a single run repairs a mixed-era ledger correctly — there's no cutoff flag to supply,
because the timeline already encodes it.

It's deliberately manual. Anthropic publishes *current* rates, not effective dates or a price
history, so the cutoff is a judgment call made from the announcement email — the one fact this
repo cannot observe. A row whose model has no period covering its timestamp aborts the whole run
(listed per model-month) rather than repricing some rows and leaving others stale. Apify rows are
skipped: their charge was never derived from a token count, so there's nothing to recompute. The
run takes the same writer lock as ingest and rescore.

**`effort` — how it's applied.** Effort controls how much a *reasoning* model (Claude 4.6+/5,
e.g. `claude-sonnet-5`) thinks before answering. It parallels `model`: an `effort` sits alongside
every `model` knob (`[ai]`, `[viability]` + its `location_effort`, `[descriptions]`), with the same
resolution order. But it only *does* anything when the resolved model is a reasoning model — on the
default Haiku (a non-reasoning model) the value is **ignored/thrown away**, and if you set it
explicitly there, the batch runs print a one-line notice saying so. Unlike `model`, the *effective*
effort (i.e. the value actually used — nothing on a non-reasoning model) is folded into the
viability staleness hash, so **changing the effort re-scores every job** when it actually changes
scoring, and is a no-op on a Haiku config.

## Viability scoring (`[viability]`)

Each score is a `high`/`medium`/`low` rating plus a one-sentence reason and a self-reported **factor breakdown** (a signed −2…+2 contribution, where 0 = no effect, for six fixed dimensions — *role requirements fit*, *role interest fit*, *seniority fit*, *company fit*, *compensation*, *location* — plus any extra axes the model surfaces). The breakdown is always produced when scoring runs — there's no config key for it — and is shown in the job preview panel. Note that *role interest fit* and *seniority fit* read your candidate `prompt` for intangibles (e.g. "I don't want to be farmed out as a consultant") and level tolerance ("open to a small step down"), so state those preferences there. Wording matters: an **absolute** dealbreaker ("won't", "not interested in", "refuse") acts as a **veto** that forces the rating to `low` regardless of other factors, whereas a soft preference ("prefer to avoid", "ideally") is weighted as a strong negative but won't by itself disqualify. See [Features → Viability scoring](features.md#viability-scoring) and, before adopting a prompt edit, the `compare_scoring.sh` stability check described there.

```toml
[viability]
enabled = true
model   = "claude-sonnet-5"     # optional: override [ai].model for scoring
effort  = "medium"              # optional: thinking effort for the scorer (reasoning models only)
prompt  = """
Describe yourself as a candidate…
"""

# Optional: a resume file, folded into the profile as ground-truth of your real experience.
# Path is relative to THIS file. Formats: .md/.txt, .pdf (needs pypdf), .docx (needs python-docx).
resume_file = "resume.md"

# Optional geographic preferences (see below):
location_prompt = """
I currently reside in Alexandria, Virginia.
PREFERRED: DC Metro / Northern Virginia; also fully remote with no state restriction that
bars my current residence (Virginia) — i.e. I can work it without moving.
GOOD: Raleigh/RTP and elsewhere in North Carolina.
ACCEPTABLE: South Carolina.
POOR: on-site/hybrid whose only locations are outside VA/DC/NC/SC, unless fully remote.
State-restricted remote: some "remote" roles restrict eligibility to (or exclude) residents
of certain states. I presently reside in Virginia but am willing to establish residence in
DC, NC, or SC for a role I want. So do NOT treat such a role as closed just because VA is
excluded — it is workable as long as its eligible-state list includes any of DC, NC, or SC.
Rate it by the best eligible state among those: DC → PREFERRED (my metro; no real move);
NC → GOOD; only SC → ACCEPTABLE; none of DC/NC/SC eligible → POOR.
If a posting's ONLY location is an entire country ("United States") with no state or city,
assume it falls within my target areas (my searches are already geographically pre-filtered)
and treat it as at least ACCEPTABLE.
"""
location_model = "claude-haiku-4-5"   # optional; defaults to [ai].model
location_effort = "low"               # optional; effort for the location sub-call (reasoning models only)
location_use_description = true        # optional; default true (see below)

# Optional auto-skip (disabled by default):
auto_skip            = false
auto_skip_confidence = "low"   # "low" (only low) or "medium" (low + medium)

# Optional company reject-list (employers you'll never work for). One name per line reads best
# as the list grows; the inline form ["Initech", "Globex"] is equivalent (TOML parses both the same):
reject_companies = [
    "Initech",
    "Globex",
]
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

> **State-restricted remote roles.** A common trap: a posting says "remote" but its description
> excludes your home state (e.g. *"remote, but not eligible to be hired in … VA …"*). With the
> description on (the default), the geo sub-call honors that condition — it drops the remote
> option for you and rates by whatever's left, which is often just an out-of-area office → `POOR`
> → forced to `low`. That's correct **only if you truly can't take the job.** If you'd relocate to
> an *eligible* state you otherwise like, say so in `location_prompt` — the sub-call judges by the
> residence/constraints you state, not just where you live now. Name the states you'd move to and
> the tier each earns, and tell it a role stays workable when its eligible list includes any of
> them (see the example above). Value judgments live entirely in `location_prompt`, so this is the
> place to encode it — no code change. Because your current state (the one being excluded) is
> baked into the wording, **revisit this rule if you actually relocate.** Per job, the manual
> **geo-fit `ACCEPTABLE` override** is the quick escape hatch — it skips the sub-call entirely and
> suppresses the `low`-clamp for that one posting.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable viability scoring. |
| `api_key` | *(from `[ai]`)* | Optional per-feature override of the Anthropic key. |
| `model` | *(from `[ai]`)* | Optional per-feature override of the model. |
| `effort` | *(from `[ai]`)* | Thinking effort for the scorer, applied only on a reasoning `model` (see `[ai].effort`). Its effective value is in the staleness hash, so changing it re-scores. |
| `prompt` | *(required)* | Your candidate description. Be specific: background, target roles, deal-breakers. |
| `resume_file` | *(none)* | Path to a resume file, folded into `prompt` as **authoritative, ground-truth** evidence of your **actual** experience — so `role_requirements_fit` is judged against your real background, not just the self-description. It also **mandates** an extra `application_competitiveness` factor: the **employer's-eye** view of how likely you are to get a positive response (a screen/callback) rather than be filtered out — a distinct question from whether the role fits *you*, with its own signed −2…+2 contribution shown in the breakdown. Resolved **relative to the file that declares `[viability]`** (the canonical config in single-search mode, the per-search file in multi-search — so each lens can point at a resume tailored to that kind of role); an absolute path works too. Formats: **`.md`/`.txt`** (read directly), **`.pdf`** (needs `pypdf`), **`.docx`** (needs `python-docx`) — those two libraries are only required if you actually use those formats. The resume's **text** is folded into the staleness hash (via `prompt`), so **editing the resume re-scores** the jobs that see it, while a rename or a byte-identical copy churns nothing. A configured-but-unreadable resume (missing file, empty/image-only PDF, unsupported format, or a missing extractor library) is a **loud config error** — not silently ignored, since a silent skip would leave scores looking resume-informed when they never saw it. **Tip:** PDF/DOCX text extraction can garble multi-column layouts, tables, or headers. Common **letter-spaced** headers (a title stored with wide tracking that extracts as `P R O F E S S I O N A L  S U M M A R Y`) are **auto-repaired** back to real words; other garble may survive, so run **`./preview_resume.sh <file>`** to print the exact text the scorer will see and sanity-check it before relying on a run. |
| `location_prompt` | *(none)* | Your geographic/remote preferences. When set, a focused single-purpose AI call matches each job's location(s) against this and feeds only the verdict — one of four ordinal tiers `PREFERRED` > `GOOD` > `ACCEPTABLE` > `POOR` — to the main scorer, which reads geography far more reliably than parsing a multi-city list inline. The tier names are generic; **your** prompt decides which locations earn which tier. Put **all** location/remote judgments here and keep geography out of `prompt` so it isn't double-judged. Editing it re-scores every job (it's folded into the staleness hash). The sub-call also reads each job's **description**, so it honors eligibility conditions in the prose (e.g. a "remote" role that only accepts residents of certain states) — **include where you live** so it can tell whether such conditions include you. A **`POOR`** verdict — the bottom tier, meaning no listed location or remote option the candidate can actually work — **deterministically forces the overall viability to `low`** (geography is a hard disqualifier); the main scorer would otherwise discount it and still return `medium`. The other three tiers flow to the scorer as advisory context. When this override fires, the score's reason keeps the model's own explanation with a bracketed `[Forced to LOW: …]` note appended. *Tip:* if your ingest tasks already restrict searches by geography, you can tell the prompt to assume a bare-country location (`"United States"`) is in-area — the feed wouldn't have surfaced it otherwise. |
| `location_effort` | *(auto)* | Thinking effort for the location sub-call (reasoning models only). Mirrors `location_model`'s source when unset: the viability effort when the sub-call reads descriptions, else `[ai].effort`. On a reasoning model the sub-call now runs adaptive thinking at this effort (it used to always run thinking-disabled). Folded into the staleness hash. |
| `location_model` | *(auto)* | Optional model for the location sub-call. When set, it always wins. When unset, the default **depends on `location_use_description`**: with the description off, the sub-call is a trivial location match, so it uses the cheap `[ai]` model even when `model` above is pricier; with the description on, reading the prose to tell a real eligibility restriction from incidental office/regional wording is a comprehension task the cheap model gets wrong (it false-`POOR`s fully-remote roles), so it **escalates to the model viability scoring uses** — i.e. `[viability].model` if set, else `[ai].model` (so the escalation only buys a better sub-call when your scoring model is more capable than `[ai].model`; if your whole setup is on one cheap model, set `location_model` here). Set this explicitly to override either default. |
| `location_use_description` | `true` | Whether the location sub-call reads each job's description. On (default), it honors eligibility conditions in the prose — e.g. a "remote" role restricted to residents of certain states — but must run per unique description **and needs a capable model** (see `location_model`: the default auto-escalates to your viability `model`; a cheap model like Haiku mis-reads noisy descriptions and false-`POOR`s remote jobs). Off, it dedups hard by location set (fewer calls, cheaper, cheap model is fine) but can't see those conditions. Folded into the staleness hash, so flipping it re-scores. |
| `auto_skip` | `false` | Automatically set `new`/`reviewing` jobs to `autoskipped` if they score at or below the threshold. |
| `auto_skip_confidence` | `"low"` | Threshold: `"low"` skips only low-scored jobs; `"medium"` skips low and medium. |
| `reject_companies` | *(none)* | A list of employer names you will **never** work for. Their jobs are forced to `viability = low` + `status = autoskipped` **without an AI call** (no tokens) — a hard human decision, not a judgment call, so it bypasses scoring entirely. Matching is **exact and case-insensitive** on the **whole** company field (never substring — `"Foo"` won't hit `"Foobar Inc"`) and runs **after `[company_aliases]`**, so the stored name is already canonical: list `"Foo"` once and it catches every `"Foo, LLC"`/`"Foo Co."` variant the alias map folds in. Only affects **pre-decision** jobs (`new`/`reviewing`/`deferred`) and already-`autoskipped` ones; a job you've already **applied** to or are **interviewing** for is left alone (your action outranks the list) and still scored normally. **Not** folded into the staleness hash (adding a name would otherwise force a full-DB re-score): instead, adding a company auto-skips its existing pre-decision jobs on the **next** rescore, and removing one takes effect on the next `rescore_viability.sh --autoskipped`, which re-scores the autoskipped set and promotes anything that now clears the bar. The stored reason reads `Autoskipped: <employer> is on your company reject-list.` *Caveat (shared with aliases):* a job ingested under an old spelling **before** you added its alias keeps that spelling until it's next re-ingested, so the reject-list won't catch that straggler until then — list the variant too, or let it re-ingest. |

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
# effort = "low"               # optional: effort if `model` is a reasoning model
```

| Key | Default | Description |
|-----|---------|-------------|
| `use_ai_on_descriptions` | `false` | Enable AI reformatting at ingest. |
| `api_key` / `model` | *(from `[ai]`)* | Optional per-feature overrides. |
| `effort` | *(from `[ai]`)* | Thinking effort, applied only if `model` is a reasoning model. The Haiku default reformats at `temperature=0` and ignores it. (A faithful reformat is trivial, so keep it low.) |

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
