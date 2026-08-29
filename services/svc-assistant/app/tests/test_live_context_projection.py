from app.context import project_safe_live_context
from app.schemas import AssistantContextLocator


def test_live_context_exposes_status_and_credits_without_identity_or_media_references():
    locator = AssistantContextLocator(surface="mobile", screen="dashboard")
    home = {
        "user_id": "private-user-id",
        "header": {"email": "person@example.com"},
        "plan": {"plan_name": "Pro Monthly", "customer_id": "cus_private"},
        "credits": {
            "total_available": 3364,
            "total_reserved": 0,
            "wallet_available": 3364,
            "payment_method": "card_private",
        },
        "video_carousel": [
            {
                "title": "Jane Doe private video",
                "status": "completed",
                "created_at": "2026-08-29T17:30:00Z",
                "video_url": "https://signed.example/video.mp4?secret=1",
                "thumbnail_url": "https://signed.example/poster.jpg?secret=2",
                "source_job_id": "private-job-id",
            }
        ],
        "face_carousel": [{"image_url": "https://signed.example/face.png"}],
    }
    studio_jobs = [
        {
            "studio": "face",
            "status": "succeeded",
            "people_mode": "single_or_unspecified",
            "created_at": "2026-08-29T17:00:00Z",
            "updated_at": "2026-08-29T17:01:00Z",
        }
    ]
    longform_jobs = [
        {
            "studio": "longform",
            "status": "completed",
            "stage": "finalized",
            "people_mode": "multi_person",
            "created_at": "2026-08-29T17:20:00Z",
            "updated_at": "2026-08-29T17:31:00Z",
            "final_output_available": True,
        }
    ]

    safe = project_safe_live_context(home, studio_jobs, longform_jobs, locator)
    wire = str(safe)

    assert safe["context_scope"] == "live_user_application_state"
    assert safe["live_context_available"] is True
    assert safe["pricing"]["plan"]["plan_name"] == "Pro Monthly"
    assert safe["pricing"]["credits"]["total_available"] == 3364
    assert safe["dashboard"]["recent_final_video_count"] == 1
    assert safe["generation"][0]["people_mode"] == "multi_person"
    assert safe["generation"][0]["status"] == "completed"
    assert safe["generation"][0]["stage"] == "finalized"
    assert safe["generation"][0]["final_output_available"] is True

    for forbidden in (
        "private-user-id",
        "person@example.com",
        "cus_private",
        "card_private",
        "Jane Doe",
        "signed.example",
        "private-job-id",
        "video.mp4",
        "poster.jpg",
        "face.png",
    ):
        assert forbidden not in wire


def test_live_context_filters_jobs_to_current_studio():
    locator = AssistantContextLocator(surface="web", screen="audio_studio")
    safe = project_safe_live_context(
        {},
        [
            {"studio": "face", "status": "failed", "updated_at": "2026-08-29T17:00:00Z"},
            {"studio": "audio", "status": "processing", "updated_at": "2026-08-29T17:10:00Z"},
            {"studio": "fusion", "status": "queued", "updated_at": "2026-08-29T17:20:00Z"},
        ],
        [],
        locator,
    )

    assert len(safe["generation"]) == 1
    assert safe["generation"][0]["kind"] == "audio"
    assert safe["generation"][0]["status"] == "processing"
