import json

from app.services.product_visual_policy import evaluate_product_visual_policy


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _payload(result):
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(result)}
                ],
            }
        ]
    }


def test_visual_policy_blocks_firearm(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DF_PRODUCT_VISUAL_POLICY_ENABLED", "true")

    result = {
        "firearm_or_weapon": True,
        "terrorist_or_extremist_promotion": False,
        "nudity_or_explicit_sexual": False,
        "sexual_content_involving_minor": False,
        "graphic_blood_or_gore": False,
        "graphic_violence_or_torture": False,
        "reason": "A handgun is visible.",
    }

    monkeypatch.setattr(
        "app.services.product_visual_policy.requests.post",
        lambda *args, **kwargs: _Response(_payload(result)),
    )

    decision = evaluate_product_visual_policy(b"\xff\xd8\xff" + b"x" * 100)
    assert decision.allow is False
    assert decision.status.value == "FAIL"
    assert any(item.category == "weapons" for item in decision.findings)
    assert all(item.required_action for item in decision.findings)


def test_visual_policy_blocks_extremist_promotion(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    result = {
        "firearm_or_weapon": False,
        "terrorist_or_extremist_promotion": True,
        "nudity_or_explicit_sexual": False,
        "sexual_content_involving_minor": False,
        "graphic_blood_or_gore": False,
        "graphic_violence_or_torture": False,
        "reason": "Promotional extremist imagery is present.",
    }
    monkeypatch.setattr(
        "app.services.product_visual_policy.requests.post",
        lambda *args, **kwargs: _Response(_payload(result)),
    )
    decision = evaluate_product_visual_policy(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    assert decision.allow is False
    assert any(item.category == "terrorism" for item in decision.findings)


def test_visual_policy_passes_clean_image(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    result = {
        "firearm_or_weapon": False,
        "terrorist_or_extremist_promotion": False,
        "nudity_or_explicit_sexual": False,
        "sexual_content_involving_minor": False,
        "graphic_blood_or_gore": False,
        "graphic_violence_or_torture": False,
        "reason": "No unsupported content.",
    }
    monkeypatch.setattr(
        "app.services.product_visual_policy.requests.post",
        lambda *args, **kwargs: _Response(_payload(result)),
    )
    decision = evaluate_product_visual_policy(b"\xff\xd8\xff" + b"x" * 100)
    assert decision.allow is True
    assert decision.status.value == "PASS"
