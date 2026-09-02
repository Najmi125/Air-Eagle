"""Why a seat could not be filled: readable first, complete underneath.

Reported from live use (2026-09-02). One rotation's reason was every
attempted pair concatenated into a paragraph, the same commander
rejection repeating in every line, because the search tries that
commander against every second pilot in turn and records each failure
whole.

Two properties are pinned here and they pull against each other:

  * THE SUMMARY NAMES THE ROOT CAUSE, with the seat asymmetry right —
    a blocked Commander makes every Second Pilot reason noise, and a
    usable Commander makes them the answer.
  * THE RECORD LOSES NOTHING. uncovered_seats.reason is the only
    surviving explanation of an unfilled seat, so it is regulatory
    evidence: the summary is PREPENDED and every trial line survives
    character for character.

DB-free, and the summariser is a pure function of the trials — which
is the point. It is computed from structured facts held at the moment
each trial fails, never by parsing the sentence back apart.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.roster_generator_service import (
    RejectedTrial, build_uncovered_reason, summarize_rejected_trials,
)

_MEDICAL = "CPT-05's MEDICAL expired 2026-08-31, not valid for duty date 2026-09-07"
_SIM = "FO-01's SIM expired 2026-08-01, not valid for duty date 2026-09-07"


def _pair(commander, second_pilot, commander_reason=None,
          second_pilot_reason=None, pair_reason=None):
    detail = "; ".join(
        part for part in (
            f"commander: {commander_reason}" if commander_reason else None,
            f"second pilot: {second_pilot_reason}" if second_pilot_reason else None,
            pair_reason,
        ) if part) or "no detail"
    return RejectedTrial(
        commander_id=commander, second_pilot_id=second_pilot,
        commander_reason=commander_reason, second_pilot_reason=second_pilot_reason,
        pair_reason=pair_reason,
        text=f"{commander}+{second_pilot} (REJECTED): {detail}")


# ------------------------------------------------------------------
# The seat asymmetry
# ------------------------------------------------------------------

def test_a_blocked_commander_is_reported_once_not_once_per_second_pilot():
    """THE reported defect. One grounded commander tried against three
    second pilots produced three near-identical lines; the useful
    sentence names the commander once."""
    trials = [
        _pair("CPT-05", "FO-01", commander_reason=_MEDICAL, second_pilot_reason=_SIM),
        _pair("CPT-05", "FO-03", commander_reason=_MEDICAL),
        _pair("CPT-05", "FO-04", commander_reason=_MEDICAL),
    ]
    summary = summarize_rejected_trials(trials)

    # No "CPT-05: CPT-05's ..." — the reason already says whose it is.
    assert summary == f"No eligible Commander — {_MEDICAL}"
    assert summary.count("CPT-05") == 1, "the repetition is the whole complaint"


def test_second_pilot_reasons_are_dropped_when_every_commander_is_blocked():
    """Noise, precisely: whoever the second pilot was, the pair failed
    for the commander. Naming them points a controller at the wrong
    renewal."""
    trials = [
        _pair("CPT-05", "FO-01", commander_reason=_MEDICAL, second_pilot_reason=_SIM),
        _pair("CPT-05", "FO-03", commander_reason=_MEDICAL, second_pilot_reason=_SIM),
    ]
    assert "FO-01" not in summarize_rejected_trials(trials)
    assert "SIM" not in summarize_rejected_trials(trials)


def test_second_pilots_are_named_when_a_commander_was_available():
    """The opposite case, where who and why IS the answer."""
    trials = [
        _pair("CPT-01", "FO-01", second_pilot_reason=_SIM),
        _pair("CPT-01", "FO-03", second_pilot_reason="FO-03's rest is insufficient"),
    ]
    summary = summarize_rejected_trials(trials)

    assert summary.startswith("No eligible Second Pilot")
    assert "FO-01" in summary and "FO-03" in summary


def test_a_partly_blocked_commander_pool_does_not_read_as_no_commander():
    """The distinction that keeps the summary honest. CPT-05 is
    grounded, CPT-01 is merely unable to pair — reporting "no eligible
    Commander" would send a controller to renew a medical when the real
    blocker was the second pilots."""
    trials = [
        _pair("CPT-05", "FO-01", commander_reason=_MEDICAL),
        _pair("CPT-01", "FO-01", second_pilot_reason=_SIM),
    ]
    summary = summarize_rejected_trials(trials)

    assert not summary.startswith("No eligible Commander"), summary
    assert summary.startswith("No eligible Second Pilot")
    # Only trials whose commander was usable say anything about the
    # second pilots — a second pilot never tried against a usable
    # commander has not been shown to be the problem.
    assert "FO-01" in summary


def test_a_pair_level_block_is_named_as_such():
    """Neither pilot is individually unavailable; the combination is —
    the age-pairing rule being the live example. Blaming one seat would
    be wrong in both directions."""
    trials = [_pair("CPT-01", "FO-01",
                    pair_reason="both pilots are over 60 on this duty date")]
    summary = summarize_rejected_trials(trials)

    assert summary.startswith("No legal pairing")
    assert "over 60" in summary


def test_the_single_seat_search_names_the_seat_it_was_filling():
    """When one seat is already crewed the search is one-sided, and the
    summary should say which seat it failed to fill."""
    trials = [RejectedTrial(second_pilot_id="FO-01", second_pilot_reason=_SIM,
                            text=f"FO-01 (REJECTED): {_SIM}")]
    summary = summarize_rejected_trials(trials, seat="SECOND_PILOT")

    assert summary.startswith("No eligible Second Pilot")
    assert "FO-01" in summary


def test_many_blocked_crew_are_capped_rather_than_listed_in_full():
    """Replacing one unreadable paragraph with a shorter unreadable
    paragraph is not a fix."""
    trials = [_pair(f"CPT-{i:02d}", "FO-01", commander_reason=f"reason {i}")
              for i in range(1, 9)]
    summary = summarize_rejected_trials(trials)

    assert "other crew member(s)" in summary
    assert len(summary) < 300, summary


def test_no_trials_at_all_still_says_something_true():
    assert summarize_rejected_trials([]) == "No candidates in pool"
    assert build_uncovered_reason([]) == "No candidates in pool"


# ------------------------------------------------------------------
# The record
# ------------------------------------------------------------------

def test_every_trial_survives_verbatim_in_the_stored_reason():
    """uncovered_seats.reason is regulatory evidence and the only
    surviving explanation of an unfilled seat. The summary is
    PREPENDED, never substituted."""
    trials = [
        _pair("CPT-05", "FO-01", commander_reason=_MEDICAL, second_pilot_reason=_SIM),
        _pair("CPT-05", "FO-03", commander_reason=_MEDICAL),
        _pair("CPT-01", "FO-04", second_pilot_reason="FO-04's route check expired"),
    ]
    stored = build_uncovered_reason(trials)

    for trial in trials:
        assert trial.text in stored, f"lost from the record: {trial.text}"


def test_the_stored_reason_leads_with_the_summary_and_counts_the_trials():
    trials = [
        _pair("CPT-05", "FO-01", commander_reason=_MEDICAL),
        _pair("CPT-05", "FO-03", commander_reason=_MEDICAL),
    ]
    stored = build_uncovered_reason(trials)

    assert stored.startswith("No eligible Commander — CPT-05's MEDICAL")
    assert "Tried 2 combination(s)" in stored
    # The detail follows the headline rather than replacing it.
    assert stored.index("Detail:") < stored.index("CPT-05+FO-01")


def test_the_summary_is_derived_and_not_the_status():
    """Belt and braces on the alert-volume lesson: collapsing is a
    display concern. The count of trials in the record must reflect
    every combination tried, not the number the summary names."""
    trials = [_pair(f"CPT-{i:02d}", "FO-01", commander_reason=f"reason {i}")
              for i in range(1, 9)]
    stored = build_uncovered_reason(trials)

    assert "Tried 8 combination(s)" in stored
    assert all(trial.text in stored for trial in trials)
