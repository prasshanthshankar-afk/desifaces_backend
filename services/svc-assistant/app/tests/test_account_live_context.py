from app.context import project_safe_live_context
from app.schemas import AssistantContextLocator
from app.service import operational_account_answer


def _context():
    locator = AssistantContextLocator(surface="web", screen="dashboard")
    return project_safe_live_context(
        {
            "plan": {"plan_name": "Pro Monthly"},
            "credits": {"total_available": 12394, "total_reserved": 0},
        },
        [],
        [],
        locator,
        library={
            "total": 6,
            "partial": False,
            "items": [
                {"studio": "face", "status": "ready", "created_at": "2026-10-01T12:00:00Z"},
                {"studio": "audio", "status": "ready", "created_at": "2026-10-01T12:05:00Z"},
                {
                    "studio": "video",
                    "asset_class": "group_video",
                    "workflow_kind": "group_photo_conversation",
                    "status": "ready",
                    "created_at": "2026-10-01T12:10:00Z",
                    "video_url": "https://signed.invalid/private.mp4",
                    "media_asset_id": "secret-media-id",
                },
            ],
        },
        spending={
            "period": "month",
            "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
            "credits": {
                "consumed": 77,
                "refunded": 0,
                "purchased": 0,
                "available": 12394,
                "reserved": 0,
            },
            "money": {
                "paid": 0.0,
                "currency": "USD",
                "credit_purchases": 0.0,
                "subscriptions": 0.0,
                "invoices": 0.0,
                "refunds": 0.0,
                "payment_method": "secret-card",
            },
            "categories": [
                {"category": "Video", "credits": 50, "percent": 64.9, "transactions": 1},
                {"category": "Face", "credits": 24, "percent": 31.2, "transactions": 1},
                {"category": "Voice", "credits": 3, "percent": 3.9, "transactions": 1},
            ],
        },
        transactions={
            "items": [
                {
                    "id": "secret-transaction-id",
                    "occurred_at": "2026-10-01T12:10:00Z",
                    "type": "usage",
                    "category": "Video",
                    "label": "Video usage",
                    "credits": -50,
                    "money": None,
                    "currency": "USD",
                    "status": "completed",
                    "channel": "web",
                    "sku_code": "internal-sku",
                }
            ]
        },
    )


def test_account_context_projects_spending_and_saved_work_without_sensitive_references():
    safe = _context()
    wire = str(safe)

    assert safe["spending"]["period"] == "month"
    assert safe["spending"]["credits"]["consumed"] == 77
    assert safe["spending"]["money"]["paid"] == 0.0
    assert safe["spending"]["categories"][0]["category"] == "Video"

    assert safe["saved_work"]["total"] == 6
    assert safe["saved_work"]["counts"]["face"] == 1
    assert safe["saved_work"]["counts"]["audio"] == 1
    assert safe["saved_work"]["counts"]["video"] == 1
    assert safe["saved_work"]["counts"]["group_video"] == 1

    for forbidden in (
        "signed.invalid",
        "private.mp4",
        "secret-media-id",
        "secret-card",
        "secret-transaction-id",
        "internal-sku",
    ):
        assert forbidden not in wire


def test_piku_answers_monthly_spending_from_authenticated_context():
    answer = operational_account_answer("Where did my credits go this month?", _context())
    assert answer is not None
    assert "**77 credits**" in answer
    assert "**USD 0**" in answer
    assert "Video: 50 credits" in answer
    assert "Face: 24 credits" in answer
    assert "Voice: 3 credits" in answer


def test_piku_answers_saved_work_from_authenticated_context():
    answer = operational_account_answer("What did I create recently in saved work?", _context())
    assert answer is not None
    assert "**6 saved work items**" in answer
    assert "Face: 1" in answer
    assert "Audio: 1" in answer
    assert "Video: 1" in answer
    assert "Group Videos: 1" in answer
    assert "secret" not in answer.lower()
