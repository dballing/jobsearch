"""Posting-plausibility veto: the scorer's fraud axis, and the guardrails that keep it from
becoming a general suspicion dial.

Background. A join.com posting put a real German ISP's name and real company blurb on a
fabricated fully-remote US role; every individual fact checked out, so the scorer rated it `high`
and it cost an application. The fix is asymmetric on purpose, because the obvious version of it is
WORSE than the miss: a model that cannot look anything up can't distinguish an obscure, foreign,
or blandly-named employer from a fake one, and a false positive silently discards a real
opportunity the user never sees. So the prompt grounds the judgment solely in whether the posting
contradicts ITSELF, denies the model recognition-based reasoning outright, and makes the factor's
ABSENCE the default (an extra axis, not one of the mandatory FACTOR_DIMENSIONS).

These are prompt-contract and ordering tests — they pin the invariants a future prompt edit could
erode. They can't verify the model's live judgment; `tools/compare_scoring.sh` does that.
"""
import json

import pytest

from viability import (FACTOR_DIMENSIONS, PLAUSIBILITY_DIMENSION,
                       RESUME_COMPETITIVENESS_DIMENSION, _SYSTEM_BOILERPLATE, parse_factors)

BOILERPLATE = _SYSTEM_BOILERPLATE
NOTE = BOILERPLATE[BOILERPLATE.index("Posting-plausibility note"):]


def test_dimension_name():
    assert PLAUSIBILITY_DIMENSION == "posting_plausibility"


def test_is_an_extra_axis_not_a_mandatory_dimension():
    # The crux of "presume genuine": because it isn't one of the required six, a plausible posting
    # emits NO plausibility factor at all. Promoting it into FACTOR_DIMENSIONS would force the
    # model to render a verdict on every job and invite it to find a little doubt everywhere.
    assert PLAUSIBILITY_DIMENSION not in FACTOR_DIMENSIONS
    assert PLAUSIBILITY_DIMENSION != RESUME_COMPETITIVENESS_DIMENSION


def test_note_is_present_and_names_the_dimension():
    assert "Posting-plausibility note" in BOILERPLATE
    assert f'"{PLAUSIBILITY_DIMENSION}"' in NOTE


def test_note_states_the_presumption_of_validity():
    assert "PRESUME EVERY POSTING IS GENUINE" in NOTE
    # The reasoning the model must NOT use, and the reason why.
    assert "never from whether you recognize the employer" in NOTE
    assert "cannot look anything up" in NOTE


@pytest.mark.parametrize("not_evidence", [
    "never heard of",            # unknown employer
    "foreign",                   # non-domestic employer
    "regional",                  # a small local business
    "ambiguous",                 # an ambiguously-named company
    "cannot identify at all",    # an unidentifiable company
    "machine-written",           # AI-sounding prose
    "templated",                 # boilerplate copy
    "missing salary",            # sparse posting
    "aggregator",                # arrived via an intermediary
])
def test_note_explicitly_neutralizes_the_false_positive_triggers(not_evidence):
    # The load-bearing half of the note. Each of these is a property of a GENUINE obscure posting,
    # and each is the kind of thing a model reaches for when asked "is this real?". If any of these
    # stops being explicitly neutral, the veto starts eating legitimate small/foreign employers —
    # the exact failure this feature was designed around.
    assert not_evidence in NOTE
    assert "NONE of the following is evidence of anything" in NOTE


def test_an_unusually_good_offer_is_not_suspicious():
    # Guards the most costly false positive of all: a strong, fully-remote, well-paid role is the
    # thing the user is searching FOR. A plausibility axis that reads "too good to be true" as a
    # warning would suppress precisely the best matches.
    assert "unusually GOOD" in NOTE
    assert "NEVER be treated as a warning sign" in NOTE


def test_veto_is_binary_with_no_middle_setting():
    # No -1 rung, because a "slightly suspicious" option is how a presumption of validity erodes:
    # mild doubt is cheap to emit and would accumulate across ordinary obscure postings.
    assert "NO middle setting" in NOTE
    assert "never -1 and never positive" in NOTE
    assert "OMIT the factor entirely" in NOTE


def test_veto_requires_an_internal_contradiction_with_no_benign_explanation():
    assert "CONTRADICTION INSIDE THIS POSTING" in NOTE
    # The escape hatch that keeps the bar high: if an ordinary corporate explanation fits, no veto.
    assert "DEFEATS the veto" in NOTE
    for benign in ("subsidiary", "holding company", "rebrand", "translation", "copy-paste"):
        assert benign in NOTE


def test_expansion_is_only_benign_when_the_posting_says_so():
    # The loophole that made the first version useless. With "an expansion into a new market" in
    # the unconditional benign list, the model could always construct that explanation for a
    # cross-border posting — measured 0/5 on the known-fabricated listing. Re-admitting it ONLY
    # when the posting itself claims the expansion took it to 5/5. Keep this conditional.
    assert "Expansion into that country is an ordinary explanation ONLY if the posting" in NOTE
    benign_para = NOTE[NOTE.index("An ordinary explanation"):NOTE.index("DEFEATS the veto")]
    assert "expansion" not in benign_para.lower()


def test_the_cross_border_check_is_mandatory_to_perform():
    # Absent-by-default (an extra axis, no mandatory report) is what makes "presume genuine" cheap
    # — but it also meant the scorer never ENGAGED with the question on a posting where everything
    # else fit, so the veto never fired. Naming one concrete check it MUST run is the fix.
    assert "MUST actually perform rather than assume" in NOTE
    assert "employer's OWN self-description does not place it in" in NOTE
    # Self-limiting, which is why it doesn't cost false positives: a real multinational says so,
    # and a role located where the employer operates is untouched.
    assert "multinational, global, or already operating" in NOTE
    assert "does NOT trip this" in NOTE


def test_veto_is_wired_into_the_rating_veto_rule():
    # The rating is explicitly NOT an average of the factors, so a -2 alone wouldn't guarantee
    # 'low'; the veto paragraph is what forces it. Without this line the factor could fire at -2
    # and still leave a 'medium' rating on a posting we believe is fabricated.
    assert "the posting-plausibility note below fires" in BOILERPLATE
    assert "VETO" in NOTE


# ── UI render order ──────────────────────────────────────────────────────────
def _ordered(dims):
    import app
    raw = json.dumps([{"dimension": d, "score": -2 if d == PLAUSIBILITY_DIMENSION else 1,
                       "note": "n"} for d in dims])
    return [f["dimension"] for f in app._viability_factors(raw)]


def test_plausibility_renders_first():
    # A "we think this listing is fabricated" verdict invalidates every fit judgment below it, so
    # it has to be the first thing read — not buried alphabetically among the extras.
    out = _ordered(["compensation", RESUME_COMPETITIVENESS_DIMENSION, "travel_burden",
                    PLAUSIBILITY_DIMENSION, "role_interest_fit"])
    assert out[0] == PLAUSIBILITY_DIMENSION
    assert out[-1] == RESUME_COMPETITIVENESS_DIMENSION
    # The fixed six still precede the model-chosen extras in between.
    assert out.index("role_interest_fit") < out.index("compensation") < out.index("travel_burden")


def test_order_unchanged_when_plausibility_absent():
    # The common case: no plausibility factor at all, and nothing about the existing order moves.
    out = _ordered(["travel_burden", "compensation", RESUME_COMPETITIVENESS_DIMENSION,
                    "role_requirements_fit"])
    assert out == ["role_requirements_fit", "compensation", "travel_burden",
                   RESUME_COMPETITIVENESS_DIMENSION]


def test_parse_factors_keeps_a_minus_two_plausibility_verbatim():
    # parse_factors never clamps; an off-scale or wrong-signed value must stay visible as prompt
    # disobedience rather than be silently normalized into looking correct.
    got = parse_factors([{"dimension": PLAUSIBILITY_DIMENSION, "score": -2, "note": "fabricated"}])
    assert got == [{"dimension": PLAUSIBILITY_DIMENSION, "score": -2.0, "note": "fabricated"}]
    off = parse_factors([{"dimension": PLAUSIBILITY_DIMENSION, "score": -1, "note": "eh"}])
    assert off[0]["score"] == -1.0
