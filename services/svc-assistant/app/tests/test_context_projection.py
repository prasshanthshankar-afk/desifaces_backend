from app.context import project_safe_story_context
from app.schemas import AssistantContextLocator


def test_context_projection_minimizes_customer_content_and_drops_sensitive_data():
    raw = {
        "account_id": "secret-account",
        "project_id": "secret-project",
        "story_id": "secret-story",
        "creation_type": "story",
        "context_scope": "scene_participant",
        "title": "A Story About Jane Doe",
        "concise_summary": "Jane lives at a private address",
        "participant_context": [
            {
                "participant_id": "p1",
                "display_name": "Real Person Name",
                "kind": "person",
                "locale": "en-AU",
                "primary_face_media_id": "m1",
                "voice_profile_ref": "provider-internal",
                "persona": {"role": "partner", "birth_date": "2000-01-01"},
                "continuity": {"home_address": "123 Private Street"},
            }
        ],
        "scene_context": [
            {
                "scene_id": "s1",
                "sequence": 1,
                "state": "audio",
                "title": "Jane at home",
                "summary": "Private scene prose",
                "setting": {"address": "123 Private Street"},
            }
        ],
        "dialogue_context": [
            {
                "turn_id": "t1",
                "scene_id": "s1",
                "sequence": 1,
                "speaker_participant_id": "p1",
                "speaker_display_name": "Real Person Name",
                "text": "Call me at 415-555-1212",
                "locale": "en-AU",
                "emotion": "warm",
            }
        ],
        "generation_context": [
            {
                "status": "failed",
                "locale": "en-AU",
                "voice_gender": "male",
                "signed_url": "https://example.invalid/private",
                "provider_request_id": "provider-secret",
            }
        ],
        "pricing_context": {
            "currency": "USD",
            "credits_required": 12,
            "customer_email": "person@example.com",
            "payment_method": "card-secret",
            "internal_note": "do not expose",
        },
        "allowed_assistant_actions": ["generate_audio", "check_price"],
    }
    locator = AssistantContextLocator(
        surface="mobile",
        screen="story_audio",
        story_id="00000000-0000-0000-0000-000000000001",
    )
    safe = project_safe_story_context(raw, locator)
    wire = str(safe)

    for forbidden in (
        "secret-account",
        "secret-project",
        "secret-story",
        "Real Person Name",
        "Jane Doe",
        "private address",
        "123 Private Street",
        "Jane at home",
        "Private scene prose",
        "415-555-1212",
        "example.invalid",
        "provider-secret",
        "person@example.com",
        "card-secret",
        "do not expose",
        "2000-01-01",
    ):
        assert forbidden not in wire

    assert safe["participants"][0] == {
        "alias": "Participant 1",
        "kind": "person",
        "locale": "en-AU",
    }
    assert safe["scenes"][0] == {"sequence": 1, "state": "audio"}
    assert safe["dialogue"][0]["speaker"] == "Participant 1"
    assert safe["dialogue"][0]["has_text"] is True
    assert safe["dialogue"][0]["text_length"] == len("Call me at 415-555-1212")
    assert safe["generation"][0]["status"] == "failed"
    assert safe["generation"][0]["locale"] == "en-AU"
    assert safe["generation"][0]["voice_gender"] == "male"
    assert safe["pricing"] == {"currency": "USD", "credits_required": 12}
