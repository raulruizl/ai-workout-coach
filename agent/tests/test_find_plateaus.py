from unittest.mock import patch

from tools.find_plateaus import find_all, find_plateaus


def _week(**exercises):
    return {
        "exercises": [
            {
                "exercise_template_id": tid,
                "exercise_title": tid,
                "best_est_1rm": vals[0],
            }
            for tid, vals in exercises.items()
        ]
    }


def test_find_all_returns_plateaued_exercises():
    weeks = [
        _week(A=(60,), B=(30,)),
        _week(A=(58,), B=(31,)),
        _week(A=(56,), B=(32,)),
        _week(A=(54,), B=(33,)),
    ]
    result = find_all(weeks)
    assert result == [{"exercise_template_id": "A", "exercise_title": "A", "best_est_1rm_drop": 6.0}]


def test_find_all_empty_when_nothing_plateaued():
    weeks = [_week(A=(50,)), _week(A=(55,))]
    assert find_all(weeks) == []


def test_find_all_sorted_by_largest_drop_first():
    weeks = [
        _week(A=(60,), B=(30,)),
        _week(A=(59,), B=(20,)),
    ]
    result = find_all(weeks)
    assert [r["exercise_template_id"] for r in result] == ["B", "A"]


def test_find_all_ignores_exercise_missing_from_latest_week():
    weeks = [_week(A=(60,)), _week()]
    assert find_all(weeks) == []


def test_find_all_empty_history():
    assert find_all([]) == []


@patch("tools.find_plateaus.fetch_history")
def test_tool_wraps_find_all(mock_fetch):
    mock_fetch.return_value = {"weeks": [_week(A=(60,)), _week(A=(55,))]}
    result = find_plateaus()
    assert result["plateaus"] == [{"exercise_template_id": "A", "exercise_title": "A", "best_est_1rm_drop": 5.0}]
