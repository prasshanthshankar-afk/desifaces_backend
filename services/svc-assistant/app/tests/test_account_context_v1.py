from app.context import project_safe_live_context
from app.schemas import AssistantContextLocator


def test_account_context_projection_keeps_real_time_facts_and_drops_customer_content():
    locator = AssistantContextLocator(surface="web", screen="dashboard")

    safe = project_safe_live_context(
        {
            "plan": {"plan_name": "Pro Monthly", "customer_id": "cus_private"},
            "credits": {"total_available": 12394, "total_reserved": 0},
            "runway_summary": {"available_credits": 12394},
            "face_carousel": [{"image_url": "https://secret.example/face.png"}],
            "video_carousel": [],
        },
        [],
        [],
        locator,
        library={
            "items": [
                {
                    "title": "Private portrait prompt",
                    "asset_class": "group_photo",
                    "studio": "face",
                    "status": "saved",
                    "preview_url": "https://secret.example/group.png",
                    "created_at": "2026-10-01T10:00:00Z",
                },
                {
                    "title": "Private audio title",
                    "asset_class": "multi_person_audio",
                    "studio": "audio",
                    "status": "saved",
                },
            ]
        },
        spending={
            "period": "month",
            "credits": {"consumed": 77, "available": 12394, "reserved": 0},
            "money": {"paid": 0.0, "currency": "USD", "credit_purchases": 0.0},
            "categories": [{"category": "Video", "total": 50}],
        },
        billing={
            "current_subscription": {
                "plan_name": "Pro Monthly",
                "subscription_state": "active",
                "customer_id": "cus_secret",
                "payment_method": "pm_secret",
            }
        },
        notifications={
            "unread_count": 2,
            "items": [
                {
                    "id": "notification-secret-id",
                    "title": "Private title",
                    "body": "Private notification text",
                    "category": "jobs",
                    "priority": "important",
                    "event_type": "GENERATION_COMPLETED",
                    "created_at": "2026-10-01T11:00:00Z",
                    "is_read": False,
                    "image_url": "https://secret.example/notification.png",
                }
            ],
        },
        stories=[
            {
                "story_id": "story-secret-id",
                "thread_id": "thread-secret-id",
                "title": "Private story title",
                "state": "active",
                "workflow_state": "awaiting_review",
                "current_stage": "audio",
                "attention_state": "awaiting_review",
                "continue_path": "/app/multi-person?story=secret",
                "updated_at": "2026-10-01T11:10:00Z",
            }
        ],
    )

    wire = str(safe)

    assert safe["context_freshness"] == "request_time_read_only"
    assert safe["pricing"]["credits"]["total_available"] == 12394
    assert safe["account"]["spending"]["credits"]["consumed"] == 77
    assert safe["account"]["library"]["counts"]["group_photo"] == 1
    assert safe["account"]["library"]["counts"]["multi_person_audio"] == 1
    assert safe["account"]["notifications"]["unread_count"] == 2
    assert safe["account"]["stories"]["count"] == 1
    assert safe["account"]["stories"]["recent"][0]["attention_state"] == "awaiting_review"

    for forbidden in (
        "cus_private",
        "cus_secret",
        "pm_secret",
        "Private portrait prompt",
        "Private audio title",
        "Private notification text",
        "Private title",
        "Private story title",
        "story-secret-id",
        "thread-secret-id",
        "secret.example",
        "notification-secret-id",
        "continue_path",
    ):
        assert forbidden not in wire


def test_account_context_includes_safe_navigation_actions():
    locator = AssistantContextLocator(surface="web", screen="dashboard")
    safe = project_safe_live_context({}, [], [], locator)

    assert "open_saved_work" in safe["allowed_actions"]
    assert "open_plans_usage" in safe["allowed_actions"]
    assert "open_multi_person" in safe["allowed_actions"]
