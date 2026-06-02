# Features

## Web UI

### Filtering

- **Label** — filter to a specific search dimension (geography, role type, etc.), or show all.
- **Source** — filter to LinkedIn or career-site results only. Appears automatically when both sources are present.
- **Status** — see [Status reference](#status-reference) below.
- **Viability** — filter by AI viability score (High / Medium / Low / Unscored). Appears once any jobs have been scored.
- **View** — *Grouped* (default): near-duplicate jobs are collapsed into a single expandable row. *Flat*: one row per posting.

### Columns and sorting

Click any column header to sort; click again to reverse; click a third time to return to the default. Sorting is case-insensitive.

Use the **⊞ columns** button in the filter bar to show or hide individual columns. Preferences are saved in `localStorage` per browser.

### Status reference

| Status | Meaning |
|--------|---------|
| `new` | Freshly ingested, not yet reviewed |
| `skipped` | Not a fit — skip for now (set manually) |
| `autoskipped` | Automatically skipped by viability scoring (see [Viability → Auto-skip](#auto-skip)) |
| `reviewing` | Opened but not decided yet |
| `applied` | Application submitted |
| `rejected` | Rejected by employer |
| `ghosted` | Applied but never heard back |
| `interviewing` | Active interview process |
| `offered` | Offer received |
| `withdrawn` | You withdrew your application |
| `closed` | Posting expired or no longer active |

In grouped view, if all postings in a group share the same status, a group-level dropdown updates all of them at once.

> **Tip:** If a job you've marked `skipped` (or that was auto-set to `autoskipped`) has its description updated by the employer, it is automatically reset to `new` on the next ingest run. These jobs display a ↻ icon next to their title.

### Previewing job descriptions

Click the &#9783; icon next to any job title to open a side panel with the full description and application history. The **View Job** button links to the original posting.

### Bulk-skip low-viability jobs

When any `new` jobs on the current page have a `low` viability score, a **Skip N low & new** button appears in the filter bar. Clicking it confirms and sets all matching jobs on that page to `skipped` in one action.

---

## Re-ingestion behavior

When a job already exists in the database and appears again in a subsequent ingest run:

- All mutable fields (title, company, location, salary, description) are refreshed.
- `first_seen` is preserved.
- If the job appears under a new label, that label is added to its list.
- If the posting has expired and status is `new` or `reviewing`, it is automatically set to `closed`.
- If the description changed and status was `skipped` or `autoskipped`, it resets to `new` (unless `reset_on_change = false` for that task).
- If `auto_ghost = true` and status is `applied` and `applied_at` is at least `auto_ghost_days` old, it moves to `ghosted`. See [Configuration → auto_ghost](configuration.md#global-keys).

---

## Fuzzy near-duplicate detection

When the same job appears on multiple platforms or under slightly different titles, the fuzzy dedup feature detects and groups these automatically.

### How it works

On each new job ingested, the script:

1. Pre-filters existing canonical jobs by title similarity > 60 % (fast upper-bound check).
2. Computes a full `SequenceMatcher` ratio on the job description.
3. If the ratio meets `fuzzy_desc_threshold` (default 0.85), the new job is recorded as a duplicate (`canonical_id` set to the canonical's `job_id`).

No company filter is applied — the same job often appears under different company names when posted by recruiters. Detection is cross-task.

### UI behavior

Fuzzy-linked jobs are collapsed into a single group row in the Grouped view. Expand to see each posting individually with its own status dropdown and description preview.

### Status inheritance

When `inherit_canonical_status = true` (default), a newly linked duplicate starts with the same status as its canonical. See [Configuration](configuration.md#global-keys).

### Notes

- Only canonical jobs (`canonical_id IS NULL`) are considered as match candidates, preventing chains.
- Existing jobs before `fuzzy_dedup` was enabled are not retroactively linked — only new/re-ingested jobs are checked.

### Manual linking

When fuzzy matching doesn't catch two postings you can tell are the same role, link them manually. Click the **🔗 link icon** next to any job title:

1. Type a title or company name to search. Multiple words narrow results (all must match); wrap a phrase in quotes for an exact match (e.g. `"senior tpm" zillow`).
2. Select the match from the results.
3. Click **Link to selected**.

If the job being linked has `new` or `reviewing` status, it inherits the canonical's status. If you select a job that is itself already linked, your job is linked to the root directly — no chains are created.

To **unlink** a job, click its 🔗 icon (blue when a link is active) and click **Unlink**.

---

## Viability scoring

`rescore_viability.sh` uses the Anthropic API to rate each job as **high**, **medium**, or **low** viability against your candidate description.

### Setup

Add a `[viability]` section to `config.toml` — see [Configuration → Viability scoring](configuration.md#viability-scoring-viability).

### Running

```bash
./rescore_viability.sh
```

Or chain after ingestion in cron:
```
0 1,5,9,13,17,21 * * * /path/to/jobsearch/ingest.sh >> /path/to/jobsearch/ingest.log 2>&1 && /path/to/jobsearch/rescore_viability.sh >> /path/to/jobsearch/viability.log 2>&1
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Show how many jobs would be scored without scoring them |
| `--early-stage` | Score only `new`/`reviewing` jobs (narrower than the default active filter) |
| `--force` | Rescore all matching jobs even if the prompt hash is current |
| `--all` | Also score closed/ghosted/skipped jobs (default: exclude them) |
| `--config PATH` | Use a different config file |

### How it works

- Each job is scored in one Anthropic API call. Your candidate `prompt` is sent as a cached system prompt, so repeated calls within a session only pay full token cost on the first.
- A SHA-256 hash of the prompt is stored with each score. On subsequent runs, only jobs with a missing or stale hash are re-scored.
- Jobs with `NULL` viability are always scored regardless of status (they may have inherited a status from a canonical without ever being evaluated).
- When a linked (`skipped`/`autoskipped`) job scores strictly better than both its canonical and its own previous score, it is automatically reset to `new` for human review — unless `auto_skip` is enabled and the score is still below the threshold, in which case it updates to `autoskipped` instead.

### Auto-skip

Once you have confidence in your viability prompt, enable automatic skipping:

```toml
[viability]
auto_skip            = true
auto_skip_confidence = "low"   # "low" or "medium"
```

Any `new` or `reviewing` job that scores at or below the threshold is automatically set to `autoskipped` after rescoring. The `autoskipped` status is functionally identical to `skipped` but is historically distinguishable from a manually-set skip. The rescore summary line reports how many were auto-skipped.

### UI

Once any jobs are scored:

- A **Viability** column shows color-coded badges (green/yellow/red). Hover for the one-sentence reason.
- A **Viability** filter appears in the filter bar.
- Stale scores (prompt changed since last score) are shown at 50% opacity with a tooltip.

---

## Importing existing applications

`import_linkedin.sh` bulk-imports LinkedIn jobs by URL or numeric ID.

```bash
./import_linkedin.sh --status applied \
  "https://www.linkedin.com/jobs/view/1234567890" \
  4383359492
```

URLs/IDs can also be piped via stdin:
```bash
cat my_urls.txt | ./import_linkedin.sh --status applied
```

| Flag | Effect |
|------|--------|
| `--status STATUS` | Initial status (default: `applied`) |
| `--label LABEL` | Label key to apply |
| `--dry-run` | Print what would be imported without writing |
| `--debug` | Print the raw Apify response |
| `--config PATH` | Use a different config file |

**Notes:**
- Dead postings (no longer on LinkedIn) create a stub record (URL + status) so the application remains trackable.
- Jobs already in the database have their status updated to the specified value.
- Fuzzy dedup runs normally, linking imports to matching existing jobs.

---

## Known limitations

### Displayed location may not match search geography

Each posting can include multiple locations. The **Location** column shows only the first one returned by Apify, which may not be the one that matched your search geography. Hover over the location to see the full list, or click through to the original posting.
