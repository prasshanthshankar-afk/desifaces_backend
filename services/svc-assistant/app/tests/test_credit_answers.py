from app.service import operational_credit_answer


def _context():
    return {
        "surface": "mobile",
        "screen": "face_studio",
        "context_scope": "live_user_application_state",
        "pricing": {
            "plan": {"plan_name": "Free"},
            "credits": {
                "total_available": 3364,
                "total_reserved": 0,
                "wallet_available": 3364,
            },
            "runway": {
                "available_credits": 3364,
                "reserved_credits": 0,
                "estimates": [
                    {
                        "studio": "face",
                        "mode": "standard",
                        "label": "Face generation",
                        "unit": "runs",
                        "baseline_display_qty": 1,
                        "estimated_credits_for_baseline_qty": 20,
                        "estimated_credits_per_display_unit": 20,
                        "remaining_units": 168,
                    },
                    {
                        "studio": "audio",
                        "mode": "standard",
                        "label": "Speech generation",
                        "unit": "kchars",
                        "baseline_display_qty": 1,
                        "estimated_credits_for_baseline_qty": 8,
                        "estimated_credits_per_display_unit": 8,
                        "remaining_units": 420,
                    },
                    {
                        "studio": "video",
                        "mode": "standard",
                        "label": "Talking video",
                        "unit": "seconds",
                        "baseline_display_qty": 10,
                        "estimated_credits_for_baseline_qty": 100,
                        "estimated_credits_per_display_unit": 10,
                        "remaining_units": 336,
                    },
                ],
            },
        },
    }


def test_credit_balance_is_available_from_face_studio_context():
    answer = operational_credit_answer("what's my total credit available?", _context())
    assert answer is not None
    assert "**3364 credits available**" in answer
    assert "**0 reserved**" in answer
    assert "doesn’t include" not in answer
    assert "open" not in answer.lower()


def test_cross_feature_capacity_question_uses_face_audio_and_video_runway():
    answer = operational_credit_answer(
        "will I be able to create 25 face, audios and video contents?",
        _context(),
    )
    assert answer is not None
    assert "**3364 credits available**" in answer
    assert "**Face — Face generation:**" in answer
    assert "**168 face runs**" in answer
    assert "**25 face generations fit**" in answer
    assert "**Audio — Speech generation:**" in answer
    assert "**420 1K-character audio blocks**" in answer
    assert "priced by **kchars**, not by item count" in answer
    assert "**Video — Talking video:**" in answer
    assert "**336 video seconds**" in answer
    assert "priced by **seconds**, not by item count" in answer
    assert "script length and video duration" in answer


def test_general_non_pricing_question_does_not_trigger_credit_answer():
    assert operational_credit_answer("How do I make a Face Studio portrait?", _context()) is None
