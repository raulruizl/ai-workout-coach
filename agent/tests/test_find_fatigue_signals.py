from unittest.mock import patch

from tools.find_fatigue_signals import find_all, find_fatigue_signals


def _week(**exercises):
    """exercises: {template_id: (max_weight_kg, total_volume_kg)}"""
    return {
        "exercises": [
            {
                "exercise_template_id": tid,
                "exercise_title": tid,
                "max_weight_kg": vals[0],
                "total_volume_kg": vals[1],
            }
            for tid, vals in exercises.items()
        ]
    }


def test_find_all_flags_rising_volume_flat_weight():
    weeks = [
        _week(A=(45, 1000)),
        _week(A=(45, 1200)),
        _week(A=(45, 1400)),
        _week(A=(45, 1600)),
    ]
    result = find_all(weeks)
    assert result == [{
        "exercise_template_id": "A",
        "exercise_title": "A",
        "total_volume_kg_change": 600.0,
        "max_weight_kg_change": 0.0,
    }]


def test_find_all_flags_rising_volume_dropping_weight():
    weeks = [_week(A=(50, 1000)), _week(A=(45, 1600))]
    result = find_all(weeks)
    assert result[0]["exercise_template_id"] == "A"
    assert result[0]["max_weight_kg_change"] == -5.0


def test_find_all_ignores_when_weight_also_rising():
    weeks = [_week(A=(45, 1000)), _week(A=(50, 1600))]
    assert find_all(weeks) == []


def test_find_all_ignores_when_volume_not_rising():
    weeks = [_week(A=(45, 1600)), _week(A=(45, 1000))]
    assert find_all(weeks) == []


def test_find_all_skips_exercise_with_single_week():
    weeks = [_week(A=(45, 1000)), _week()]
    assert find_all(weeks) == []


def test_find_all_empty_history():
    assert find_all([]) == []


def test_find_all_sorted_by_largest_volume_increase():
    weeks = [
        _week(A=(45, 1000), B=(20, 500)),
        _week(A=(45, 1100), B=(20, 900)),
    ]
    result = find_all(weeks)
    assert [r["exercise_template_id"] for r in result] == ["B", "A"]


@patch("tools.find_fatigue_signals.fetch_history")
def test_tool_wraps_find_all(mock_fetch):
    mock_fetch.return_value = {"weeks": [_week(A=(45, 1000)), _week(A=(45, 1200))]}
    result = find_fatigue_signals()
    assert result["fatigue_signals"][0]["exercise_template_id"] == "A"
