"""Tests for the pure logic of compare_scoring.py — the before/after scoring-stability harness.

The live AI calls and the git-HEAD module import are out of scope (like every AI/network path in
this suite); what's worth locking down is the sample-selection SQL builder and the confusion-matrix
summarizer, both pure."""
import compare_scoring as cs


# ── build_sample_query: per-tier recent-sample selection ───────────────────────
def test_build_sample_query_default_no_date_bound():
    sql, params = cs.build_sample_query("high", 10)
    assert "viability = ?" in sql
    assert "ORDER BY RANDOM() LIMIT ?" in sql
    assert "first_seen" not in sql            # no date filter when neither bound is given
    assert params == ["high", 10]


def test_build_sample_query_previous_days_window():
    sql, params = cs.build_sample_query("medium", 5, previous_days=30)
    assert "first_seen >= datetime('now', ?)" in sql
    assert params == ["medium", "-30 days", 5]


def test_build_sample_query_since_takes_precedence_over_previous_days():
    sql, params = cs.build_sample_query("low", 8, since="2026-06-01", previous_days=30)
    assert "date(first_seen) >= ?" in sql
    assert "datetime('now'" not in sql        # since wins; the trailing-window branch is skipped
    assert params == ["low", "2026-06-01", 8]


# ── tiers_to_compare: --tier restricts to one band, else all three ─────────────
def test_tiers_to_compare_all_when_none():
    assert cs.tiers_to_compare(None) == cs.TIERS


def test_tiers_to_compare_single_tier():
    assert cs.tiers_to_compare("medium") == ("medium",)


# ── summarize_pairs: agreement / confusion matrix / move counts ────────────────
def test_summarize_pairs_all_agree():
    pairs = [("high", "high"), ("medium", "medium"), ("low", "low")]
    s = cs.summarize_pairs(pairs)
    assert (s["total"], s["same"], s["up"], s["down"]) == (3, 3, 0, 0)
    assert s["agreement_rate"] == 1.0
    assert s["matrix"]["high"]["high"] == 1
    assert s["matrix"]["low"]["low"] == 1


def test_summarize_pairs_counts_up_and_down_moves():
    # medium→high is an up-move; high→low is a down-move; low→low is unchanged.
    pairs = [("medium", "high"), ("high", "low"), ("low", "low")]
    s = cs.summarize_pairs(pairs)
    assert (s["total"], s["same"], s["up"], s["down"]) == (3, 1, 1, 1)
    assert abs(s["agreement_rate"] - 1/3) < 1e-9
    assert s["matrix"]["medium"]["high"] == 1
    assert s["matrix"]["high"]["low"] == 1


def test_summarize_pairs_skips_unrecognized_ratings():
    # A failed score (None / unknown) on either side is skipped, not counted as a move.
    pairs = [("high", "high"), (None, "high"), ("medium", "bogus")]
    s = cs.summarize_pairs(pairs)
    assert s["total"] == 1 and s["same"] == 1


def test_summarize_pairs_empty_is_zero_not_crash():
    s = cs.summarize_pairs([])
    assert s["total"] == 0 and s["agreement_rate"] == 0.0


# ── _factor_detail_lines: printable breakdown, fixed dims first, signed scores ──
def test_factor_detail_lines_orders_fixed_first_and_signs_scores():
    factors = [
        {"dimension": "growth", "score": 1, "note": "expanding"},                  # extra → last
        {"dimension": "compensation", "score": 0, "note": "no comp"},              # fixed
        {"dimension": "role_requirements_fit", "score": 2, "note": "great scope"}, # fixed
        {"dimension": "location", "score": -1, "note": ""},                        # fixed, no note
    ]
    lines = cs._factor_detail_lines(factors)
    dims = [ln.split()[1] for ln in lines]                 # each line: "  +2  <dimension> — note"
    # fixed dims in canonical order (requirements < compensation < location), extras after
    assert dims == ["role_requirements_fit", "compensation", "location", "growth"]
    assert "+2" in lines[0] and "great scope" in lines[0]
    assert "+0" not in lines[1] and " 0" in lines[1]       # a 0 is shown unsigned
    assert "-1" in lines[2] and "—" not in lines[2]        # negative sign; no note dash when blank


def test_factor_detail_lines_none_or_empty_is_no_lines():
    assert cs._factor_detail_lines(None) == []
    assert cs._factor_detail_lines([]) == []


# ── _factor_sum: code-computed QA total (never fed back to the scorer) ──────────
def test_factor_sum_adds_scores():
    factors = [
        {"dimension": "role_requirements_fit", "score": 2, "note": ""},
        {"dimension": "role_interest_fit", "score": -2, "note": ""},
        {"dimension": "location", "score": 1, "note": ""},
    ]
    assert cs._factor_sum(factors) == 1


def test_factor_sum_none_or_empty():
    assert cs._factor_sum(None) is None
    assert cs._factor_sum([]) is None
