from app.context import project_safe_story_context
from app.schemas import AssistantContextLocator


def test_context_projection_drops_ids_urls_and_real_names():
    raw = {
        "account_id": "secret-account",
        "project_id": "secret-project",
        "story_id": "secret-story",
        "creation_type": "story",
        "context_scope": "scene_participant",
        "title": "A Story",
        "concise_summary": "A short story",
        "participant_context": [
            {
                "participant_id": "p1",
                "display_name": "Real Person Name",
                "kind": "person",
                "locale": "en-AU",
                "primary_face_media_id": "m1",
                "voice_profile_ref": "provider-internal",
                "persona": {"role": "partner"},
            }
        ],
        "scene_context": [{"scene_id": "s1", "sequence": 1, "state": "audio"}],
        "dialogue_context": [
            {
                "turn_id": "t1",
                "scene_id": "s1",
                "sequence": 1,
                "speaker_participant_id": "p1",
                "speaker_display_name": "Real Person Name",
                "text": "hello",
                "locale": "en-AU",
            }
        ],
        "generation_context": [
            {
                "status": "failed",
                "signed_url": "https://example.invalid/private",
                "provider_request_id": "provider-secret",
            }
        ],
        "allowed_assistant_actions": ["generate_audio"],
    }
    locator = AssistantContextLocator(
        surface="mobile",
        screen="story_audio",
        story_id="00000000-0000-0000-0000-000000000001",
    )
    safe = project_safe_story_context(raw, locator)
    wire = str(safe)
    assert "secret-account" not in wire
    assert "Real Person Name" not in wire
    assert "example.invalid" not in wire
    assert "provider-secret" not in wire
    assert safe["participants"][0]["alias"] == "Participant 1"
    assert safe["dialogue"][0]["speaker"] == "Participant 1"
    assert safe["generation"][0]["status"] == "failed"
