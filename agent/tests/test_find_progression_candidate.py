from unittest.mock import patch

from tools.find_progression_candidate import (
    find_candidate,
    find_progression_candidate,
    is_plateau,
    weight_streak,
)


def _week(**exercises):
    """Build a week dict from {template_id: (max_weight_kg, mean_reps, best_est_1rm)}."""
    return {
        "exercises": [
            {
                "exercise_template_id": tid,
                "exercise_title": tid,
                "max_weight_kg": vals[0],
                "mean_reps": vals[1],
                "best_est_1rm": vals[2] if len(vals) > 2 else None,
            }
            for tid, vals in exercises.items()
        ]
    }


# ---- weight_streak ----------------------------------------------------------

def test_weight_streak_counts_consecutive_same_weight_weeks():
    weeks = [
        _week(A=(30, 15)),
        _week(A=(40, 15)),
        _week(A=(40, 15)),
        _week(A=(40, 15)),
    ]
    streak, qualifying = weight_streak(weeks, "A")
    assert streak == 3
    assert qualifying == 3


def test_weight_streak_breaks_on_weight_change():
    weeks = [
        _week(A=(25, 10)),
        _week(A=(30, 10)),
        _week(A=(40, 7.67)),
        _week(A=(40, 8.67)),
    ]
    streak, qualifying = weight_streak(weeks, "A")
    assert streak == 2  # last two weeks both at 40kg
    assert qualifying == 0  # neither reaches 9 reps


def test_weight_streak_breaks_on_missing_week():
    weeks = [
        _week(A=(45, 10)),
        _week(),  # not logged this week
        _week(A=(45, 9)),
        _week(A=(45, 9)),
    ]
    streak, qualifying = weight_streak(weeks, "A")
    assert streak == 2  # missing week 2 breaks continuity with week 1
    assert qualifying == 2


def test_weight_streak_zero_when_not_in_latest_week():
    weeks = [_week(A=(45, 10)), _week()]
    assert weight_streak(weeks, "A") == (0, 0)


def test_weight_streak_zero_for_bodyweight_exercise():
    weeks = [_week(A=(None, 15))]
    assert weight_streak(weeks, "A") == (0, 0)


# ---- is_plateau ---------------------------------------------------------------

def test_is_plateau_true_for_non_increasing_1rm():
    weeks = [_week(A=(45, 8, 60)), _week(A=(45, 8, 58)), _week(A=(45, 8, 58)), _week(A=(45, 8, 55))]
    assert is_plateau(weeks, "A") is True


def test_is_plateau_false_if_any_increase():
    weeks = [_week(A=(45, 8, 55)), _week(A=(45, 8, 60))]
    assert is_plateau(weeks, "A") is False


def test_is_plateau_false_if_missing_a_week():
    weeks = [_week(A=(45, 8, 60)), _week(), _week(A=(45, 8, 55))]
    assert is_plateau(weeks, "A") is False


def test_is_plateau_false_for_single_week():
    weeks = [_week(A=(45, 8, 60))]
    assert is_plateau(weeks, "A") is False


# ---- find_candidate: selection logic -------------------------------------------

def test_find_candidate_picks_highest_qualifying_weeks():
    weeks = [
        _week(A=(40, 15), B=(45, 9)),
        _week(A=(40, 15), B=(45, 9)),
        _week(A=(40, 15), B=(45, 7)),
        _week(A=(40, 15), B=(45, 9)),
    ]
    result = find_candidate(weeks)
    assert result == {"exercise_template_id": "A", "reason": "reps"}


def test_find_candidate_ignores_exercise_with_climbing_weight():
    """The exact real-data case: weight rising every week must never
    qualify, no matter how high mean_reps is, because racha_peso never
    reaches 2."""
    weeks = [
        _week(A=(25, 10)),
        _week(A=(30, 10)),
        _week(A=(40, 7.67)),
        _week(A=(40, 8.67)),
    ]
    assert find_candidate(weeks) is None


def test_find_candidate_ignores_exercise_with_sudden_weight_drop():
    """The real Empuje de Caderas case: old qualifying weeks must not
    resurrect a candidate once the most recent week's weight changed."""
    weeks = [
        _week(A=(27, 10)),
        _week(A=(36, 9)),
        _week(A=(36, 7.67)),
        _week(A=(10, 8)),  # sudden drop this week
    ]
    assert find_candidate(weeks) is None


def test_find_candidate_falls_back_to_plateau_when_no_reps_candidate():
    weeks = [
        _week(A=(45, 8, 60)),
        _week(A=(45, 8, 58)),
        _week(A=(45, 8, 56)),
        _week(A=(45, 8, 54)),
    ]
    result = find_candidate(weeks)
    assert result == {"exercise_template_id": "A", "reason": "plateau"}


def test_find_candidate_plateau_picks_largest_drop():
    weeks = [
        _week(A=(45, 8, 60), B=(20, 8, 30)),
        _week(A=(45, 8, 59), B=(20, 8, 28)),
        _week(A=(45, 8, 58), B=(20, 8, 26)),
        _week(A=(45, 8, 57), B=(20, 8, 24)),
    ]
    result = find_candidate(weeks)
    assert result == {"exercise_template_id": "B", "reason": "plateau"}


def test_find_candidate_none_when_nothing_qualifies():
    weeks = [_week(A=(45, 6, 55)), _week(A=(45, 7, 56))]
    assert find_candidate(weeks) is None


def test_find_candidate_ignores_exercise_not_in_latest_week():
    weeks = [
        _week(A=(45, 10, 60)),
        _week(A=(45, 9, 60)),
        _week(A=(45, 9, 60)),
        _week(B=(20, 8, 25)),  # A dropped this week (new machine variant etc.)
    ]
    assert find_candidate(weeks) is None


def test_find_candidate_empty_history():
    assert find_candidate([]) is None


def test_find_candidate_tie_break_is_deterministic_by_template_id():
    weeks = [
        _week(B=(40, 15), A=(40, 15)),
        _week(B=(40, 15), A=(40, 15)),
    ]
    result = find_candidate(weeks)
    assert result == {"exercise_template_id": "A", "reason": "reps"}


# ---- find_progression_candidate (tool wrapper) ---------------------------------

@patch("tools.find_progression_candidate.fetch_history")
def test_tool_returns_candidate_none_when_nothing_qualifies(mock_fetch):
    mock_fetch.return_value = {"weeks": [_week(A=(45, 6))]}
    assert find_progression_candidate() == {"candidate": None}


@patch("tools.find_progression_candidate.fetch_history")
def test_tool_returns_exercise_title_and_reason(mock_fetch):
    mock_fetch.return_value = {
        "weeks": [
            _week(A=(40, 15)),
            _week(A=(40, 15)),
        ]
    }
    result = find_progression_candidate()
    assert result["exercise_template_id"] == "A"
    assert result["reason"] == "reps"
    assert result["exercise_title"] == "A"
