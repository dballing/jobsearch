# Features

## Web UI

### Filtering

- **Label** — filter to a specific search dimension (geography, role type, etc.), or show all.
- **Source** — filter to LinkedIn or career-site results only. Appears automatically when both sources are present.
- **Status** — see [Status reference](#status-reference) below.
- **Viability** — filter by AI viability score (High / Medium / Low / Unscored). Appears once any jobs have been scored.

### Searching

The search box matches job title and company. Multiple words are matched independently (each must appear, in any order); wrap a phrase in quotes (`"senior tpm"`) to match it as a unit. Matching is whitespace-insensitive, so a search built from a displayed title still finds its stored original even if that has irregular spacing. A plain **Search** keeps your current filters applied (search *within* the filtered set).

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
| `deferred` | On the radar but not being acted on — e.g. a role you'd discuss with a recruiter while interviewing elsewhere, but aren't applying to in parallel. Counts as **Active** and keeps getting (re)scored, but the automations leave it alone: it is never auto-closed on expiry, auto-skipped on a low score, or reset on a description change. Clear it manually when it's no longer relevant. |
| `applied` | Application submitted |
| `rejected` | Rejected by employer |
| `ghosted` | Applied but never heard back |
| `interviewing` | Active interview process |
| `offered` | Offer received |
| `withdrawn` | You withdrew your application |
| `closed` | Posting expired or no longer active |

With **Matched-Jobs** grouping on, if all postings in a group share the same status, a group-level dropdown updates all of them at once.

**Applied date on a status change.** Changing status keeps the **Applied** date in step automatically. Moving into an *early* status (`new`, `reviewing`, `deferred`, `skipped`, `autoskipped`) means no application is outstanding, so it **clears** the date. Moving into any *applied-family* status (`applied`, `interviewing`, `offered`, `rejected`, `withdrawn`, `ghosted`) **stamps the current time — but only when the date is empty**, so a real application date is never overwritten by a later toggle (e.g. `applied → interviewing` keeps the original date). This means a job that reaches the applied family by any path (e.g. straight to `interviewing`, or back to `applied` after being cleared) always ends up with a date rather than sitting blank.

> **Tip:** If a job you've marked `skipped` (or that was auto-set to `autoskipped`) has its description updated by the employer, it is automatically reset to `new` on the next ingest run. These jobs display a ↻ icon next to their title.

### Previewing job descriptions

Click the &#9783; icon next to any job title to open a side panel with the full description and application history. The **View Job** button links to the original posting.

Descriptions arrive from feeds with inconsistent layout (hard wrapping, inline bullet glyphs, single-line walls). By default the panel applies a built-in heuristic formatter that reflows the text into paragraphs and bullet lists. Optionally, you can enable **AI reformatting** (`[descriptions]`, see [Configuration](configuration.md#ai-description-reformatting-descriptions)): at ingest time the model re-emits each description as clean Markdown, which the panel shows instead. It changes formatting only — a per-job content-integrity check rejects any output that altered the wording, and the panel falls back to the heuristic formatter whenever an AI version isn't available (feature off, API error, integrity failure, or missing libraries).

### Cover letter prompt

The preview panel also provides a **Cover Letter Prompt** button. Clicking it copies a ready-to-paste prompt to your clipboard containing the job title, company, location, salary (if known), and full description. Paste it directly into whatever AI chat session you use to generate cover letters — no manual copy-paste of the job description required. The button briefly flashes "✓ Copied!" to confirm the clipboard write succeeded.

The prompt also embeds **today's date** (read from your browser) with an instruction to date the letter with exactly that date — models have no reliable clock and otherwise routinely hallucinate the date on a generated letter.

### Company name override

When a job is posted by a third party (e.g. a job board or recruiting firm) rather than the actual employer, the ingested company name may reflect the posting agent rather than the hiring organization. To correct this, open the job preview and enter the real employer name in the **Actual company name** field below the meta bar, then press Save or Enter.

- The override replaces the displayed company name in the table and preview panel. The original name is shown in muted italic as "(via Original Name)" in the preview.
- In the main table, an asterisk (<sup>*</sup>) appears next to the company name when an override is active; hovering shows the original name.
- Both the original and override names are searched when using the title/company search bar.
- The cover letter prompt includes both names (e.g. "Company: Actual Co (advertised by Posting Agent)").
- Viability scoring sends the real employer to the AI, keeping the "posted via *Posting Agent*" note **only when the posting agent might be a recruiter/staffing firm** — for a recognized job board or aggregator (Ladders, LinkedIn, Indeed, …) the "via" is pure distribution, so it's dropped and the scorer sees just the employer. The scorer is also told to judge whether a role is a third-party contract/staffing arrangement **only from that note or explicit contract terms**, treating generic "our client" / vertical body-text boilerplate as neutral. Together these stop the model from mistaking an aggregator repost (e.g. a Netflix role relisted via Ladders) for a contract role.
- The override is cleared by deleting the field contents and saving. The original ingested name is always preserved.

**Canonical rename (change the underlying name).** The same editor has a checkbox, **"Change the underlying company name everywhere (adds a permanent alias)"** — off by default. With it checked, Save doesn't set a per-job "(via …)" override; instead it treats the typed value as the employer's real name and, in one step: (1) rewrites the scraped `company` on **every** job with that name, flagging each for rescoring and logging a `company_renamed` history event; and (2) appends the mapping to `[company_aliases]` in `config.toml` (with an `# Added YYYY-MM-DD via web app.` comment) so future ingests normalize it too — that alias is what keeps the rewrite from reverting on the next re-scrape. The feed's original spelling is still preserved in each job's `raw`. Use this for a genuine variant spelling ("X, LLC" → "X"); use the plain override for a third-party "posted via" correction.

### Job title override

When a feed's title is mangled or generic (e.g. an aggregator's "Program Manager - Req# 12345"), open the job preview and type the real title in the **Title override** field below the meta bar, then Save or Enter.

- The override replaces the displayed title everywhere it's shown (table, grouped header, preview panel, cover-letter prompt) and is what viability scoring sees. The preview shows the original in muted italic as "(originally …)".
- In the table an asterisk (<sup>*</sup>) appears next to the title; hovering shows the original. Both the original and the override are searched by the title/company search bar, and column-sorting orders by the effective (override-aware) title.
- It's **per-job**, not fanned out across a matched group — postings in a group legitimately carry different titles, so each is overridden on its own. (Group headers still show one representative title with "(varies)".)
- Changing it flags the job for rescoring (the title feeds the AI prompt). Clear it by emptying the field and saving; the original scraped title is always preserved.

### Job description override

When a feed delivers the wrong or a partial description — most often a career-site (ATS) feed that ships only a short teaser while the full posting is rendered client-side (see [the partial-description flag](#auto-skip)) — you can paste the real, complete description from the employer's site. In the preview panel, under the description, click **Paste full text** (labelled **Edit pasted text** once an override exists) to open a dialog, paste the posting, and Save.

- The override replaces the feed's description **everywhere the description is consumed**: viability scoring (the scorer reads the pasted text, not the teaser), the preview panel, and the cover-letter prompt. The feed's original text is kept in the database and can be viewed ("Show the feed's original") or restored ("Revert to feed description").
- Saving it flags the job for rescoring, so the next run re-scores it on the full text. It also **clears the ⚠ partial badge and the auto-skip exemption** for that job — once you've supplied the complete posting, it's no longer judged on partial data and auto-skip applies normally.
- It's **per-job**, not fanned out across a matched group — a pasted posting is specific to the source it came from.
- Because the override is entered by hand (not AI-reformatted), the panel renders it with the heuristic formatter rather than the AI-cleaned Markdown (which was derived from the now-superseded feed text).

### Company website link

Ingest records the employer's own site in a `company_url` column, taken from the feed (preferring the real domain — LinkedIn's `linkedin_org_url` / careersite's `domain_derived` — and falling back to the source's `organization_url`, i.e. a LinkedIn/ATS company page). It's surfaced in three places:

- **Jobs table** — a box-arrow link-out icon next to the company name opens the site in a new tab.
- **Preview panel** — the company name itself is the link.
- **Weekly report** — the employer heading links to the site; the raw URL is also printed as plain text so a paper/PDF copy carries the address.

The icon/link only appears when a URL is known (~99.7% of listings; roughly 82% resolve to the employer's own site, the rest to a company page).

### Work arrangement override

The feed classifies each job's work arrangement (remote / hybrid / on-site), and that classification is sent to the viability scorer since remote status often decides fit. When the feed is wrong or absent — e.g. a recruiter confirms a role is hybrid though the posting reads on-site — the preview panel's **Work arrangement** dropdown overrides it (On-site / Hybrid / Fully remote / Remote-hybrid-if-near-an-office; blank uses the feed's value, shown as a hint). The override wins in scoring and flags the job for rescoring. It's per-job (not fanned out across a matched group).

The dropdown also carries a special **Remote (unsupported location)** flag for the one geographic dead end the feed and the location sub-call can't catch: a role that genuinely is remote but only for residents of states/regions you can't be in. Such postings report a plain "Remote OK" and hide the restriction in an *implicit* list of eligible states rather than an explicit eligibility sentence, so the location sub-call rates the remote option a good fit and the job scores viable. (Observed: NVIDIA's "Senior Manager, Customer Program Management," remote only in CA/TX/WA.) Selecting this flag skips the AI location call and deterministically forces the score to **low** with a reason that attributes the clamp to the manual flag — the same mechanism as an AI-assessed POOR geographic fit, just triggered by hand.

### Location viability override

The positive counterpart to the "unsupported location" flag: when you'd take a job at its stated location even though the geographic check would otherwise sink it, tick **Location viability: Acceptable (override)** in the preview panel (or, for a hand-entered job, the same checkbox in the [Add a job manually](#add-a-job-manually) dialog). This is the usual reason a job gets entered by hand at all — you're tracking it *because* you'd work it.

- When set, the job's geographic fit is forced to **ACCEPTABLE**: the AI location sub-call is skipped (no cost), and — because ACCEPTABLE isn't POOR — the job is spared the POOR→low geographic clamp, so the scorer judges it on scope/comp/industry without a location penalty.
- It's per-job (the assertion is about this specific posting/location, not a whole matched group) and, like the other overrides, flags the job for rescoring. Only "Acceptable" is offered — you're asserting the location is *workable*, not ranking it — and the override wins over the "unsupported location" flag if a job somehow carries both.
- Untick it to clear the override; the next rescore returns the job to the AI-assessed geographic fit.

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

**Currency.** Salaries are shown in the currency the feed reported (`€`, `£`, `$`, …); a currency without a dedicated glyph shows as its code (e.g. `CHF 120k`), and a posting with no currency defaults to `$`. The same currency also reaches the viability scorer, so a €/£ role is comp-judged in its real currency rather than mislabeled as dollars. Next to the salary-override fields is a **currency selector** for correcting a feed that stamped the wrong currency (e.g. a euro band tagged USD). It's independent of the min/max band — you can change just the currency and leave the numbers as-is; leaving it on "feed default" keeps whatever the feed reported. No conversion is performed: `€100k` stays `€100k`, never rewritten into dollars. Like the band, the currency override is per-lens, fans out across the matched group, records in History, and flags a re-score.

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

### Add a job manually

The green **Add job** button in the toolbar opens a form for jobs that didn't come through an Apify feed — e.g. an application you submitted directly. **Title** and **company** are required (validated client- and server-side); everything else is optional: location, status, applied date, job/apply URLs, salary min/max/currency (the `k` shorthand works), posted date, labels, description, and notes. The **Applied at** field has a **Now** button that fills it with the current date and time. The form is cleared each time the dialog opens, so a previous entry never carries over.

Manually added jobs get `source = manual` (shown as "Manual" and filterable once one exists) and a `manual_`-prefixed id. A **Score viability now** checkbox scores the job inline; unchecked (or when AI is disabled/unconfigured — which surfaces a note), it starts unscored and the next scheduled rescore evaluates it. A **Location viability: Acceptable** checkbox sets the [location viability override](#location-viability-override) at entry time, so the inline score treats the location as workable. An applied-family status with no explicit applied date stamps the current time so the [weekly contact report](#weekly-contact-report) picks it up.

### Company hotlist

Employers you're especially keen on can be **hotlisted** so their fresh openings stand out. In the preview panel, click the **Hotlist ☆** star next to the company controls to toggle the employer on/off the list (workspace-wide, stored in the DB; matched case-insensitively on the effective company name).

While a hotlisted employer has a job in an **actionable** state — `new` or `reviewing` — that row is given a soft warm background tint in the jobs table (across flat, matched-group, and employer-grouped views). Once the job moves on (applied, rejected, etc.) it reverts to the normal look, so the highlight only ever flags employers with something new to act on.

### Weekly contact report

The calendar-week icon in the navbar opens `/report/weekly` — a printable record of job-search **contacts** in a Sun→Sat week (local time), intended as on-demand evidence of job-search activity for Virginia unemployment.

A "contact" is an actual exchange with an employer:

- the **application** you submitted (sourced from `applied_at`), and
- a **status change** that reflects engagement: `interviewing`, `offered`, `rejected`, or `withdrawn`.

`ghosted` is deliberately **excluded** — it's auto-inferred silence (the absence of a response), not a contact event. A role appears if it had any such contact in the week; matched-group duplicates collapse to one entry. Entries are grouped by employer and show the title, application URL, applied date/time, and each in-week contact's date/time.

Navigation: previous/next week, a date picker (jump to the week containing any date), and **This week**. The **Print** button (or your browser's print/Save-as-PDF) hides the app chrome and prints just the report. Weeks are bucketed in the **server's local timezone**, so evening activity isn't misfiled into the next UTC day.

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

1. Pre-filters existing canonical jobs by title *character* similarity > 60 % (fast upper-bound check).
2. Applies a title *word-overlap* gate (`fuzzy_title_word_threshold`, default 0.6): the titles' word sets must share at least that Jaccard fraction. Character similarity rewards a shared tail phrase, so two distinct roles with the same suffix — "Engineering Project Manager" vs "Technical Project Manager" (0.5 word-overlap) — are kept apart even when their descriptions are near-identical boilerplate, while suffix/reorder variants an aggregator produces ("Software Engineer" vs "Software Engineer - Remote", 0.67) still pass.
3. Applies a *req/posting-ID* gate (`fuzzy_title_id_gate`, default on): if both titles carry an identifier code and the codes differ — `[AQ-14258]` vs `[AQ-15000]`, `Req 14258` vs `#15000`, `L5` vs `L4` — the postings are different requisitions and never merge, even with byte-identical descriptions (the common case: one ATS template reused across many reqs). A shared code, or one side lacking a code, falls through. A "code" is a title token that mixes letters and digits or has a 4+-digit run; bare short numbers ("Level 3") are ignored.
4. Computes a full `SequenceMatcher` ratio on the job description.
5. If the ratio meets `fuzzy_desc_threshold` (default 0.85), the new job is recorded as a duplicate (`canonical_id` set to the canonical's `job_id`).

No company filter is applied — the same job often appears under different company names when posted by recruiters. Detection is cross-task.

### UI behavior

Fuzzy-linked jobs are collapsed into a single group row when **Matched-Jobs** grouping is on. Expand to see each posting individually with its own status dropdown and description preview. When the group's postings carry slightly different **titles** or **salaries** (e.g. `- AI` vs `-Ai/ML`, or feed-noise salary differences), the header shows the canonical root's concrete value followed by a small **(varies)** note, rather than an opaque "(varied)" — so the group stays identifiable and the salary is a real posting's band (not a synthetic MIN–MAX envelope). Column sorting and the salary comp-search likewise track the root's value. The **Posted** and **First Seen** columns show the group's *earliest* member date (the group was first posted/seen on that date) rather than "(varied)". The set-like columns — **viability**, **labels**, and **source** — show the *union* of the distinct values across the group (one badge each; viability best→worst) instead of "(varied)". Hovering a viability badge lists *every* reason at that level across the group's postings (deduped), not just one. Only **Status** and **Applied** still collapse to "(varied)" when members disagree — status because it's an editable control (a single dropdown can't represent mixed statuses; expand to set each), and applied date because there's no meaningful single value.

### Status inheritance

When `inherit_canonical_status = true` (default), a newly linked duplicate starts with the same status **and applied date** as its canonical — so an auto-linked duplicate of a role you've already applied to isn't left `applied` without an `applied_at`. See [Configuration](configuration.md#global-keys).

### Company-name inheritance

A newly linked duplicate also adopts its canonical's **effective employer name** as a `company_actual` override when its own scraped company differs — so a repost surfaced under an aggregator name (e.g. "RemoteHunter", "Jobgether") shows the real employer ("Cribl") without a manual re-override on every repost. A genuine copy that already names the employer gets no override. This is independent of `inherit_canonical_status` (naming is a display concern, not application status).

### Notes

- Match candidates include both canonical roots **and** already-linked members; every hit is resolved to its canonical root before linking, so reposts that rewrite the prose still link via an identical sibling, and no chains form.
- Existing jobs before `fuzzy_dedup` was enabled are not retroactively linked — only new/re-ingested jobs are checked.

### Manual linking

When fuzzy matching doesn't catch two postings you can tell are the same role, link them manually. Click the **🔗 link icon** next to any job title:

1. Type a title or company name to search. Multiple words narrow results (all must match); wrap a phrase in quotes for an exact match (e.g. `"senior tpm" zillow`).
2. Select the match from the results.
3. Click **Link to selected**.

If the job being linked has `new` or `reviewing` status, it inherits the canonical's status. If you select a job that is itself already linked, your job is linked to the root directly — no chains are created.

To **unlink** a job, click its 🔗 icon (blue when a link is active) and click **Unlink**.

**Promote to canonical.** The *canonical* is the group's root — the posting that represents the group in the jobs list, that new duplicates resolve to, and that viability's canonical-promotion compares against. When fuzzy matching (or a manual link) picks a canonical you'd rather wasn't the representative, open any grouped member's preview pane and click **Promote to canonical**: that posting becomes the root, and the former root and every sibling are re-pointed at it (one hop, no chains). The change is recorded in History (`Promoted to canonical…` on the new root, `Replaced as group canonical…` on the old).

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
| `--early-stage` | Score only `new`/`reviewing`/`deferred` jobs (narrower than the default active filter) |
| `--autoskipped` | Score only `autoskipped` jobs (not plain `skipped`); any that no longer score at/below the auto-skip threshold are surfaced back to `new` (logged to `viability.log` and job history). Meant for after a prompt change — pair with `--force` if the prompt is unchanged |
| `--status STATUS` | Score only jobs with exactly this status (e.g. `skipped`). Unlike the default filter this has no NULL/needs-rescored escape — it's exactly that status. Pair with `--force` to also reach jobs whose hash is already current |
| `--current-viability LEVEL` | Score only jobs whose **current** stored score is `high`/`medium`/`low`. Composes with any status filter — the run may then change the score |
| `--force` | Rescore all matching jobs even if the prompt hash is current |
| `--all` | Also score closed/ghosted/skipped jobs (default: exclude them) |
| `--since YYYY-MM-DD` | Only jobs first ingested (UTC) on that date or later |
| `--previous-days N` | Only jobs first ingested within the trailing N days |
| `--config PATH` | Use a different config file |

`--early-stage`/`--all`/`--autoskipped`/`--status` are mutually exclusive, as are `--since`/`--previous-days`. A typical post-prompt-change sweep of what was auto-skipped: `./rescore_viability.sh --autoskipped`. To revisit strong roles you'd manually skipped and see how many still score high: `./rescore_viability.sh --status skipped --current-viability high --force` (drop `--force` to re-check only the ones with stale scores).

### How it works

- Each job is scored in one Anthropic API call. Your candidate `prompt` is sent as a cached system prompt, so repeated calls within a session only pay full token cost on the first.
- Alongside the rating and one-sentence reason, the scorer self-reports a **factor breakdown**: for six fixed dimensions — *role requirements fit* (can I do it?), *role interest fit* (do I want it? — captures intangibles like consulting/farm-out or industry aversion), *seniority fit* (is the level right? — weighted gently, since a small step up/down is normal), *company fit* (size, stage, viability, exclusions), *compensation*, and *location* — plus any extra axes it finds relevant, it gives a signed contribution from **−2 to +2** (where **0 = no effect on the rating**) and a terse note. This makes the verdict auditable: a factor merely *mentioned* in the reason (score 0) is now distinguishable from one that actually **lowered** the rating (a negative score) — e.g. a job with no listed salary should show `compensation: 0`, not a silent dock. The scorer is required to explain the rating *entirely* through the factors, so nothing is scored invisibly. It's a model self-report of *how it weighed each factor*, not internal arithmetic — the rating itself is a single holistic judgment, with no numeric formula under the hood (the rating is deliberately **not** a threshold on the sum of the factor scores: some negatives are near-vetoes a sum can't model). In particular, an **absolute dealbreaker** stated in your profile (e.g. "won't", "not interested in") or an inability to do the core job acts as a **veto** — it forces the rating to `low` regardless of how positive everything else is; a softer preference ("prefer to avoid") is weighted as a strong negative but won't by itself disqualify. The breakdown shows in the job **preview panel** under the reason.
- A SHA-256 hash of the prompt is stored with each score. On subsequent runs, only jobs with a missing or stale hash are re-scored.
- Jobs with `NULL` viability are always scored regardless of status (they may have inherited a status from a canonical without ever being evaluated).
- Jobs are also flagged for re-scoring when a viability-relevant field changes independently of the prompt — a manual [salary override](#salary-override), [company override](#company-name-override), [job title override](#job-title-override), [work arrangement override](#work-arrangement-override), or [location viability override](#location-viability-override). Such a flagged job is re-scored on the next run even if its prompt hash is current and even if it is `skipped`/`closed` (so a correction that improves it can resurface it). The flag clears once the job is successfully re-scored.
- When a linked (`skipped`/`autoskipped`) job scores strictly better than both its canonical and its own previous score, it is automatically reset to `new` for human review — unless `auto_skip` is enabled and the score is still below the threshold, in which case it updates to `autoskipped` instead. This only fires when the canonical's own score is **current** (its prompt hash matches this run's): a stale canonical score is an apples-to-oranges yardstick that would spuriously promote duplicates purely because of a prompt change, so the comparison waits until the canonical is itself re-scored.

### Auto-skip

Once you have confidence in your viability prompt, enable automatic skipping:

```toml
[viability]
auto_skip            = true
auto_skip_confidence = "low"   # "low" or "medium"
```

Any `new` or `reviewing` job that scores at or below the threshold is automatically set to `autoskipped` after rescoring. The `autoskipped` status is functionally identical to `skipped` but is historically distinguishable from a manually-set skip. The rescore summary line reports how many were auto-skipped.

**Partial-description exemption.** Some career-site (ATS) feeds — Oracle HCM, Workable, ADP, Greenhouse, etc. — deliver only a short teaser in the job description, with the full body (responsibilities, qualifications, benefits) rendered client-side and never reaching the feed. Ingest flags these postings (short `description_text` plus an actor-extracted requirements summary — evidence a fuller posting existed) and viability scoring **still scores them but never auto-skips them**, so a possibly-good role isn't silently buried on half a posting. They stay `new`/`reviewing` for a manual look, carry a **⚠ partial** badge in the viability column, and the rescore summary reports how many were kept this way. Open the source link to read the full posting; if the employer later exposes the full text, the next ingest clears the flag.

### Validating a prompt change

The scoring prompt is tuned over time, and the premise is that the *current* prompt is scoring correctly — so `compare_scoring.sh` checks that a prompt edit doesn't unintentionally shift ratings before you adopt it. It samples recent jobs (10 per stored tier by default) and scores each one **twice** on identical inputs — once with the committed (`git HEAD`) prompt and once with your working-tree prompt — then reports how often the two agree, a confusion matrix, and (as a model-nondeterminism baseline) how often re-scoring with the *same* prompt disagrees with the stored score. It writes nothing to the database.

```bash
./compare_scoring.sh --previous-days 30       # 10 jobs per tier from the last 30 days
./compare_scoring.sh --n 15 --since 2026-07-01
./compare_scoring.sh --tier medium --n 25     # focus on one band (where churn usually is)
./compare_scoring.sh --reasons                # also print reasons/factors for unchanged jobs
```

Because the harness writes nothing, the reasons and factor breakdowns it computes never reach the database — so it prints the old/new reason and the **new factor breakdown** inline for every job whose rating *changed* (add `--reasons` to see them for all jobs). That's where you judge whether a move is an *improvement*: the harness only measures drift, not correctness.

Run it **while HEAD still holds the old prompt** — i.e. before committing the change. If new-vs-old drift is materially larger than the nondeterminism baseline (or skews consistently up/down), revisit the prompt before committing.

### UI

Once any jobs are scored:

- A **Viability** column shows color-coded badges (green/yellow/red). Hover for the one-sentence reason.
- The **preview panel** shows the full factor breakdown under the reason — each dimension's signed contribution (−2…+2; 0 = no effect) and note.
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
