from unittest.mock import patch

from tools.summarize_consistency import classify_trend, summarize, summarize_consistency


def _week(week, workout_count, total_sets, total_volume_kg):
    return {"week": week, "workout_count": workout_count, "total_sets": total_sets,
            "total_volume_kg": total_volume_kg}


# ---- classify_trend -----------------------------------------------------------

def test_classify_trend_dropping():
    assert classify_trend([10, 10, 10, 5]) == "dropping"  # 5 / 10 = 0.5 < 0.7


def test_classify_trend_rising():
    assert classify_trend([10, 10, 10, 15]) == "rising"  # 15 / 10 = 1.5 > 1.3


def test_classify_trend_steady():
    assert classify_trend([10, 10, 10, 11]) == "steady"


def test_classify_trend_insufficient_data_single_week():
    assert classify_trend([10]) == "insufficient_data"


def test_classify_trend_insufficient_data_zero_prior_average():
    assert classify_trend([0, 0, 5]) == "insufficient_data"


# ---- summarize ------------------------------------------------------------------

def test_summarize_builds_series_and_trends():
    weeks = [
        _week("2026-06-08", 3, 45, 1400),
        _week("2026-06-15", 3, 52, 1500),
        _week("2026-06-22", 3, 53, 1550),
        _week("2026-06-29", 3, 34, 2200),
    ]
    result = summarize(weeks)
    assert result["week"] == ["2026-06-08", "2026-06-15", "2026-06-22", "2026-06-29"]
    assert result["workout_count"] == [3, 3, 3, 3]
    assert result["workout_count_trend"] == "steady"  # 3 / 3 = 1.0
    assert result["total_sets_trend"] == "dropping"  # 34 / 50 = 0.68 < 0.7
    assert result["total_volume_kg_trend"] == "rising"  # 2200 / 1483.3 = 1.48 > 1.3


def test_summarize_empty_weeks():
    result = summarize([])
    assert result["week"] == []
    assert result["workout_count_trend"] == "insufficient_data"


@patch("tools.summarize_consistency.fetch_history")
def test_tool_wraps_summarize(mock_fetch):
    mock_fetch.return_value = {"weeks": [_week("2026-06-08", 3, 45, 1400)]}
    result = summarize_consistency()
    assert result["workout_count"] == [3]
    assert result["workout_count_trend"] == "insufficient_data"
