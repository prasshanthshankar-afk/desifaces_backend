from app.services.safety_service import SafetyService


def test_firearms_are_hard_blocked_before_generation():
    safety = SafetyService()

    blocked_prompts = [
        "woman with gun and arms",
        "portrait holding a pistol",
        "editorial model with rifle",
        "person carrying a shotgun",
        "fashion portrait with firearm",
    ]

    for prompt in blocked_prompts:
        ok, reason = safety.check_keywords(prompt)
        assert ok is False
        assert "PROMPT_POLICY_BLOCKED" in reason
        assert "weapons" in reason


def test_blood_and_gore_are_hard_blocked_before_generation():
    safety = SafetyService()

    blocked_prompts = [
        "bloody portrait",
        "person covered in blood",
        "gory cinematic scene",
        "open wound with realistic lighting",
        "graphic injury portrait",
    ]

    for prompt in blocked_prompts:
        ok, reason = safety.check_keywords(prompt)
        assert ok is False
        assert "PROMPT_POLICY_BLOCKED" in reason


def test_benign_arms_language_is_not_blocked_as_firearm_request():
    safety = SafetyService()

    ok, reason = safety.check_keywords(
        "woman with arms crossed, professional portrait, realistic lighting"
    )

    assert ok is True
    assert reason == ""


def test_safe_prompt_reinforces_no_weapons_blood_or_gore():
    safety = SafetyService()

    prompt = safety.build_safe_prompt("professional portrait")

    assert "no firearms" in prompt
    assert "no guns" in prompt
    assert "no blood" in prompt
    assert "no gore" in prompt
