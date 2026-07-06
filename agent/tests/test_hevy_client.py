from tools.hevy_client import build_updated_routine_body

ROUTINE = {
    "id": "routine-1",
    "title": "Push",
    "exercises": [
        {
            "index": 0,
            "title": "Bench Press (Dumbbell)",
            "notes": None,
            "exercise_template_id": "3601968B",
            "superset_id": None,
            "sets": [
                {"index": 0, "type": "warmup", "weight_kg": 10, "reps": 10,
                 "distance_meters": None, "duration_seconds": None, "custom_metric": None},
                {"index": 1, "type": "normal", "weight_kg": 18, "reps": 10,
                 "distance_meters": None, "duration_seconds": None, "custom_metric": None},
            ],
            "rest_seconds": 0,
        },
        {
            "index": 1,
            "title": "Chest Fly (Machine)",
            "notes": None,
            "exercise_template_id": "78683336",
            "superset_id": None,
            "sets": [
                {"index": 0, "type": "normal", "weight_kg": 66, "reps": 9,
                 "distance_meters": None, "duration_seconds": None, "custom_metric": None},
            ],
            "rest_seconds": 60,
        },
    ],
}


def test_updates_only_normal_sets_of_target_exercise():
    body = build_updated_routine_body(ROUTINE, "3601968B", weight_kg=20.5, reps=8)
    target = body["routine"]["exercises"][0]

    assert target["sets"][0]["weight_kg"] == 10  # warmup untouched
    assert target["sets"][1]["weight_kg"] == 20.5
    assert target["sets"][1]["reps"] == 8


def test_does_not_touch_other_exercises():
    body = build_updated_routine_body(ROUTINE, "3601968B", weight_kg=20.5, reps=8)
    other = body["routine"]["exercises"][1]

    assert other["sets"][0]["weight_kg"] == 66


def test_strips_index_and_exercise_level_title():
    body = build_updated_routine_body(ROUTINE, "3601968B", weight_kg=20.5, reps=8)
    exercise = body["routine"]["exercises"][0]

    assert "title" not in exercise
    assert all("index" not in s for s in exercise["sets"])


def test_preserves_routine_title_and_exercise_template_id():
    body = build_updated_routine_body(ROUTINE, "3601968B", weight_kg=20.5, reps=8)

    assert body["routine"]["title"] == "Push"
    assert body["routine"]["exercises"][0]["exercise_template_id"] == "3601968B"
    assert body["routine"]["exercises"][1]["exercise_template_id"] == "78683336"
