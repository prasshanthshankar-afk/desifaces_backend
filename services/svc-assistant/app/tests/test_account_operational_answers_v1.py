from app.service import operational_account_answer


def _context():
    return {
        "pricing": {
            "plan": {"plan_name": "Pro Monthly"},
            "credits": {"total_available": 12394, "total_reserved": 0},
        },
        "account": {
            "spending": {
                "credits": {"consumed": 77},
                "money": {"paid": 0.0, "currency": "USD"},
            },
            "library": {
                "total_visible": 5,
                "counts": {"face": 2, "video": 1, "group_photo": 2},
            },
            "notifications": {
                "unread_count": 3,
                "category_counts": {"jobs": 2, "billing": 1},
            },
            "stories": {
                "count": 2,
                "recent": [
                    {
                        "attention_state": "awaiting_review",
                        "current_stage": "audio",
                        "workflow_state": "awaiting_review",
                    },
                    {
                        "attention_state": "active",
                        "current_stage": "face",
                    },
                ],
            },
            "billing": {
                "current_subscription": {
                    "plan_name": "Pro Monthly",
                    "subscription_state": "active",
                }
            },
        },
    }


def test_spending_answer_is_account_specific():
    answer = operational_account_answer("What have I used and paid this month?", _context())
    assert "77 credits" in answer
    assert "$0" in answer


def test_library_answer_is_account_specific():
    answer = operational_account_answer("What is in my saved work?", _context())
    assert "5 items" in answer
    assert "2 Faces" in answer
    assert "2 Group Photos" in answer


def test_notification_answer_is_account_specific():
    answer = operational_account_answer("Do I have unread notifications?", _context())
    assert "3 unread notifications" in answer
    assert "jobs" in answer


def test_story_answer_reports_review_attention():
    answer = operational_account_answer("Which recent stories need review?", _context())
    assert "2 recent Multi-Person stories" in answer
    assert "1 need review" in answer
    assert "audio" in answer


def test_plan_answer_uses_authenticated_context():
    answer = operational_account_answer("What plan am I on?", _context())
    assert "Pro Monthly" in answer
    assert "active" in answer
