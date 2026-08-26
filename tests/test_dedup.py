"""Tests for ingest.find_canonical — member-aware fuzzy dedup with root resolution."""
import ingest

DESC_A = ("We are hiring a Staff Technical Program Manager to lead cross-functional "
          "infrastructure programs across many teams at global scale. " * 6)
DESC_B = ("About Our Client: the organization operates in telemetry infrastructure for "
          "AI, bridging ambition and operational reality with a flexible platform. " * 6)


def _insert(conn, job_id, title, desc, canonical_id=None, first_seen="2026-01-01"):
    conn.execute(
        "INSERT INTO jobs (job_id, title, company, job_description, canonical_id, "
        "first_seen, raw) VALUES (?,?,?,?,?,?,?)",
        (job_id, title, "Acme", desc, canonical_id, first_seen, "{}"),
    )
    conn.commit()


def test_member_match_resolves_to_root(jobs_db):
    # Group: root A (early) + member B pointing at A. B's prose differs from A's.
    _insert(jobs_db, "A", "Staff TPM", DESC_A, first_seen="2026-01-01")
    _insert(jobs_db, "B", "Staff TPM", DESC_B, canonical_id="A", first_seen="2026-02-01")
    # A new posting identical to member B (but unlike root A) must link to root A.
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_B, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_direct_root_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_title_prefilter_blocks_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    # Same description, wildly different title -> title pre-filter rejects.
    matches = ingest.find_canonical(jobs_db, "P", "Warehouse Forklift Operator",
                                    "Acme", DESC_A, 0.85)
    assert matches == []


def test_description_threshold_blocks_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    # Same title, unrelated description -> below desc threshold.
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_B, 0.85)
    assert matches == []


def test_returns_distinct_roots_oldest_first(jobs_db):
    # Two separate canonicals that both match the query resolve to two roots, oldest first.
    _insert(jobs_db, "OLD", "Staff TPM", DESC_A, first_seen="2026-01-01")
    _insert(jobs_db, "NEW", "Staff TPM", DESC_A, first_seen="2026-05-01")
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["OLD", "NEW"]


def test_does_not_match_itself(jobs_db):
    _insert(jobs_db, "P", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert matches == []


# ── Fast-path gates (length / shingle Jaccard / reverse) must not change results ──
def test_reworded_near_duplicate_still_matches(jobs_db):
    # A near-identical repost with a few words changed still scores >= 0.85, so the cheap
    # shingle pre-gate must NOT discard it — guards against the gate being too aggressive.
    edited = DESC_A.replace("cross-functional", "multi-functional")
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", edited, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_shingle_gate_rejects_boilerplate_sharing_non_dup(jobs_db):
    # Shares vocabulary/boilerplate but not phrase order → must stay rejected (the char
    # multiset quick_ratio can be fooled here; the shingle gate + threshold are not).
    shuffled = " ".join(reversed(DESC_A.split()))
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", shuffled, 0.85)
    assert matches == []


def test_word_shingles():
    assert ingest._word_shingles("a b c d", k=3) == {("a", "b", "c"), ("b", "c", "d")}
    assert ingest._word_shingles("a b c", k=3) == {("a", "b", "c")}
    assert ingest._word_shingles("a b", k=3) is None      # fewer than k words → no gate
    assert ingest._word_shingles("", k=3) is None


# ── Title word-overlap gate ──
def test_title_words():
    # Lowercased, punctuation split out and dropped, deduplicated to a set.
    assert ingest._title_words("Engineering Project Manager") == {"engineering", "project", "manager"}
    assert ingest._title_words("Software Engineer - Remote") == {"software", "engineer", "remote"}
    assert ingest._title_words("Sr. Staff SWE, Backend") == {"sr", "staff", "swe", "backend"}
    assert ingest._title_words("---") == set()             # no alnum tokens → gate skipped


def test_word_gate_rejects_peer_qualifier(jobs_db):
    # Identical description, titles share the "Project Manager" tail but differ by a leading
    # qualifier. Char-ratio (0.73) clears the pre-filter, but word-overlap is 2/4 = 0.5 < 0.6,
    # so these distinct roles must NOT merge — the Cisco "Technical vs Engineering PM" case.
    _insert(jobs_db, "A", "Engineering Project Manager", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Technical Project Manager", "Acme", DESC_A, 0.85)
    assert matches == []


def test_word_gate_allows_suffix_variant(jobs_db):
    # Same role with an added suffix: word-overlap 2/3 = 0.67 >= 0.6, so an aggregator's
    # "… - Remote" repost still merges with the identical-description canonical.
    _insert(jobs_db, "A", "Software Engineer", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Software Engineer - Remote", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_word_gate_skipped_for_titleless_tokens(jobs_db):
    # A title with no alnum tokens yields an empty word set → Jaccard is undefined, so the gate
    # is skipped and the char pre-filter alone governs (here both titles are "---", char 1.0).
    _insert(jobs_db, "A", "---", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "---", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_word_gate_threshold_configurable(jobs_db):
    # Lowering title_word_threshold below the pair's 0.5 overlap lets the peer-qualifier
    # pair merge again — confirms the gate is driven by the passed-in threshold, not hardcoded.
    _insert(jobs_db, "A", "Engineering Project Manager", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Technical Project Manager", "Acme", DESC_A,
                                    0.85, title_word_threshold=0.4)
    assert [m["job_id"] for m in matches] == ["A"]


# ── Req/posting-ID gate ──
def test_title_id_codes():
    # Bracketed req IDs, bare years, and level tags qualify; role words and bare short numbers don't.
    assert ingest._title_id_codes("Sr Project Manager [AQ-14258]") == {"aq-14258"}
    assert ingest._title_id_codes("Data Engineer Req 14258") == {"14258"}
    assert ingest._title_id_codes("Software Engineer L5") == {"l5"}
    assert ingest._title_id_codes("2024 Summer Intern") == {"2024"}
    assert ingest._title_id_codes("Staff Engineer, Level 3") == set()   # bare single digit ignored
    assert ingest._title_id_codes("Senior Product Manager") == set()


def test_id_gate_rejects_differing_req_id(jobs_db):
    # Same Aquent template (identical description) reused across two requisitions. Word-overlap
    # is 0.67 (would merge), but the differing [AQ-…] codes disqualify them — the real bug case.
    _insert(jobs_db, "A", "Sr Project Manager [AQ-14258]", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Sr Project Manager [AQ-15000]", "Acme", DESC_A, 0.85)
    assert matches == []


def test_id_gate_allows_matching_req_id(jobs_db):
    # Identical req ID in both titles → same requisition → still merges (description also matches).
    _insert(jobs_db, "A", "Sr Project Manager [AQ-14258]", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Sr Project Manager [AQ-14258]", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_id_gate_ignored_when_one_side_lacks_code(jobs_db):
    # An aggregator stripped the req ID from one title. We can't infer a difference from a
    # missing code, so the ID gate stays out of the way and normal matching applies.
    _insert(jobs_db, "A", "Sr Project Manager [AQ-14258]", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Sr Project Manager", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_id_gate_can_be_disabled(jobs_db):
    # With title_id_gate=False the differing req IDs no longer disqualify; word-overlap (0.67)
    # and the identical description carry the match — confirms the gate is toggleable.
    _insert(jobs_db, "A", "Sr Project Manager [AQ-14258]", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Sr Project Manager [AQ-15000]", "Acme", DESC_A,
                                    0.85, title_id_gate=False)
    assert [m["job_id"] for m in matches] == ["A"]
