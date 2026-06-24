# Features

## Web UI

### Filtering

- **Label** — filter to a specific search dimension (geography, role type, etc.), or show all.
- **Source** — filter to LinkedIn or career-site results only. Appears automatically when both sources are present.
- **Status** — see [Status reference](#status-reference) below.
- **Viability** — filter by AI viability score (High / Medium / Low / Unscored). Appears once any jobs have been scored.

### Searching

The search box matches job title and company. A plain **Search** keeps your current filters applied (search *within* the filtered set).

**Search all** runs the same term against **every** job with a clean slate — all statuses, default grouping and sort — so nothing is hidden behind your filters. This is handy when a job looks familiar and you want to find its potential duplicate to [link manually](#manual-linking), without losing your place. While in this mode a banner shows "your filters are paused"; click the **✕ / Restore filters** button (or run a normal search) to return to exactly the filtered view you came from. Your prior view is remembered in the URL, so linking a job (which reloads the page) doesn't lose it.

Two in-row shortcuts run a Search all seeded from a specific job, using the same paused-filters / restore mechanism — handy for hunting near-duplicates that fuzzy dedup missed:

- **🔍 next to a title** — find every job whose title matches that string, across all statuses.
- **🔍 next to a salary** — find every job with that identical salary band. An exact-matching comp range with slightly different descriptions is a strong missed-duplicate signal. (Shown only for real salaries — blank and `$0k–$0k` hourly-rate rows are skipped.)

In both cases you can match the duplicate and jump straight back to where you were.

### Grouping

Two independent **Group by** toggles in the filter bar control how rows are organised. Either, both, or neither can be active:

- **Matched-Jobs** (default on) — near-duplicate jobs (see [Fuzzy near-duplicate detection](#fuzzy-near-duplicate-detection)) are collapsed into a single expandable row. Click the ▸ chevron in the Location cell to expand the group and see each posting. Turn this off for a flat list of every posting.
- **Employer** (default off) — postings are grouped under a header row for each employer (the effective company name). Each employer section is collapsible via the ▾ chevron on its header, and the postings inside are indented and shown in a smaller font. With **Matched-Jobs** also on, the two nest: an employer section contains matched-job groups, which themselves expand to individual postings — a double-indent at the deepest level.

When grouping by employer, employer sections are listed alphabetically by default. A near-duplicate group whose postings span two slightly different company names is filed under just one employer (its alphabetically-first effective name) and shown whole.

### Columns and sorting

Click any column header to sort; click again to reverse; click a third time to return to the default. Sorting is case-insensitive.

When grouping by **Employer**, sorting works on two independent axes: the **Company** header re-orders the employer sections themselves (A→Z / Z→A), while every other column sorts the postings *within* each employer. Changing one does not reset the other.

Use the **⊞ columns** button in the filter bar to show or hide individual columns. Preferences are saved in `localStorage` per browser.

The **Per page** dropdown sets how many rows are shown per page — 25 (default), 50, 100, 200, or **All** (everything on one page; handy for Ctrl-F, slower on large result sets). When grouping by employer it counts employer sections per page. Changing it preserves your current filters and sort.

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

With **Matched-Jobs** grouping on, if all postings in a group share the same status, a group-level dropdown updates all of them at once.

> **Tip:** If a job you've marked `skipped` (or that was auto-set to `autoskipped`) has its description updated by the employer, it is automatically reset to `new` on the next ingest run. These jobs display a ↻ icon next to their title.

### Previewing job descriptions

Click the &#9783; icon next to any job title to open a side panel with the full description and application history. The **View Job** button links to the original posting.

Descriptions arrive from feeds with inconsistent layout (hard wrapping, inline bullet glyphs, single-line walls). By default the panel applies a built-in heuristic formatter that reflows the text into paragraphs and bullet lists. Optionally, you can enable **AI reformatting** (`[descriptions]`, see [Configuration](configuration.md#ai-description-reformatting-descriptions)): at ingest time the model re-emits each description as clean Markdown, which the panel shows instead. It changes formatting only — a per-job content-integrity check rejects any output that altered the wording, and the panel falls back to the heuristic formatter whenever an AI version isn't available (feature off, API error, integrity failure, or missing libraries).

### Cover letter prompt

The preview panel also provides a **Cover Letter Prompt** button. Clicking it copies a ready-to-paste prompt to your clipboard containing the job title, company, location, salary (if known), and full description. Paste it directly into whatever AI chat session you use to generate cover letters — no manual copy-paste of the job description required. The button briefly flashes "✓ Copied!" to confirm the clipboard write succeeded.

### Company name override

When a job is posted by a third party (e.g. a job board or recruiting firm) rather than the actual employer, the ingested company name may reflect the posting agent rather than the hiring organization. To correct this, open the job preview and enter the real employer name in the **Actual company name** field below the meta bar, then press Save or Enter.

- The override replaces the displayed company name in the table and preview panel. The original name is shown in muted italic as "(via Original Name)" in the preview.
- In the main table, an asterisk (<sup>*</sup>) appears next to the company name when an override is active; hovering shows the original name.
- Both the original and override names are searched when using the title/company search bar.
- The cover letter prompt includes both names (e.g. "Company: Actual Co (advertised by Posting Agent)").
- Viability scoring sends both names to the AI with the same "posted via" note.
- The override is cleared by deleting the field contents and saving. The original ingested name is always preserved.

### Company name normalization

Feeds spell the same employer inconsistently (e.g. "Sirius XM" vs "Sirius XM Radio"). The optional `[company_aliases]` config table maps variant spellings to one canonical name, applied automatically at ingest — so the stored value, and therefore grouping, employer search, viability scoring, and display, all use a single consistent name. See [Configuration](configuration.md#company-name-normalization-company_aliases).

- Matching is **case-insensitive** and **exact** on the whole company field (after trimming whitespace) — not substring or fuzzy.
- Applied to **newly ingested and re-seen** jobs only; a job already stored under an old spelling is normalized the next time its posting reappears in a feed (there is no bulk rewrite of existing rows).
- Aliases are not chained: map every variant directly to the final name.
- Each rewrite is recorded in the job's **History** as a `Company normalized: <feed name> → <canonical> (auto)` entry, so there's an audit trail of what the feed originally said and when it was canonicalized.
- This is distinct from the per-job **Company name override** above. Normalization canonicalizes the *feed's* spelling for everyone via config; the override manually corrects a single posting (e.g. a recruiting firm shown instead of the employer). They stack — an override's muted "(via …)" original reflects the normalized feed name.

### Salary override

The upstream feed extracts salary with its own AI, which sometimes misses a figure that is stated in the job description. To fill it in (or correct a wrong value), open the job preview and enter the annual **Salary override** min and/or max below the meta bar, then press Save or Enter. Inputs accept plain numbers, `$`/commas, or a `k` shorthand (e.g. `120k` → 120000).

- The override wins over the feed value everywhere salary is shown, sorted, or matched (table display, the Salary column sort, and the exact comp-range search icon).
- It is **shared across the matched group** like notes/attachments: the same role across locations shares one salary, so saving fans the value out to every current posting in the group (and each keeps its own copy if the group is later split). Every member's History records the edit.
- In the main table, an asterisk (<sup>*</sup>) appears next to an overridden salary; hovering shows the feed's original value (or notes the feed had none).
- Either bound may be left blank (e.g. a minimum-only "$120k+"). The override is cleared by emptying both fields and saving; the feed value then shows again.
- Setting or clearing an override flags the job for re-scoring (see [Viability scoring](#viability-scoring)), since compensation feeds the candidate evaluation.

### Notes

The preview panel has a **Notes** box for free-text notes about a role (recruiter contacts, follow-ups, impressions). Notes are **shared across the matched group**: saving writes the same text to every current posting in the fuzzy-match group, and editing any posting updates the whole group. Each posting keeps its own copy, so if the group is later split they all retain the note. Every member's History records the edit (the posting you actually typed on reads "Note updated"; siblings read "Note updated (on a grouped posting)").

### Attachments

The preview panel also lets you attach **files** to a job — cover letters, documents shared during interviews, etc. Files are stored on disk under UUID names (in `uploads_dir`, see [Configuration](configuration.md)); the real filename and metadata live in the database, and downloads serve the original filename.

- Attachments are **shared across the matched group** like notes: uploading links the file to every current posting in the group.
- Each file shows its name (click to download), size, and a **×** to remove it from the current posting.
- Removal is **reference-counted**: removing a file from one posting only unlinks it there; the physical file is deleted only once no posting references it anymore. So if you attach a file while jobs are grouped, then later split them, removing it from one job leaves the others' copies intact.
- Max upload size is 25 MB per file.
- The `uploads/` directory lives outside `jobs.db`, so back it up separately from the database.

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

### Run summary

Each ingest run ends with a breakdown so you can see what actually happened (a per-run line uses a compact one-line form; the final grand total is the block below):

```
Done in 540.6s. 230 postings seen.
  New:      1 standalone, 8 grouped, 0 arrived-expired
  Existing: 0 updated, 145 unchanged, 76 ATS duplicates skipped
  Side-ops: 12 auto-ghosted
```

- **New** — postings inserted this run: **standalone** (no fuzzy match — a genuinely new role), **grouped** (fuzzy-matched an existing role on arrival — a fresh duplicate posting), **arrived-expired** (inserted straight to `closed`). "Postings seen" is the run total; `standalone` is your count of net-new roles.
- **Existing** — postings seen again: **updated** (data changed), **unchanged**, and **ATS duplicates skipped**.
- **Side-ops** — operations on rows that weren't new inserts: **re-linked** (an existing unlinked posting newly grouped), **orphan merges** (existing canonicals merged into one group), **reset→new** (a `skipped` posting whose description changed), **auto-closed** (an existing posting that expired), **auto-ghosted** (the post-ingest aging step). Only non-zero categories are shown.

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

Fuzzy-linked jobs are collapsed into a single group row when **Matched-Jobs** grouping is on. Expand to see each posting individually with its own status dropdown and description preview.

### Status inheritance

When `inherit_canonical_status = true` (default), a newly linked duplicate starts with the same status **and applied date** as its canonical — so an auto-linked duplicate of a role you've already applied to isn't left `applied` without an `applied_at`. See [Configuration](configuration.md#global-keys).

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
- Jobs are also flagged for re-scoring when a viability-relevant field changes independently of the prompt — currently a manual [salary override](#salary-override) or [company override](#company-name-override). Such a flagged job is re-scored on the next run even if its prompt hash is current and even if it is `skipped`/`closed` (so a correction that improves it can resurface it). The flag clears once the job is successfully re-scored.
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
