from app.services.safety_service import SAFETY_POLICY_VERSION, SafetyService


EXACT_REPORTED_PROMPT = (
    "woamn with gun and arms, high-quality portrait, realistic lighting, "
    "clean composition, culturally respectful, family-friendly, "
    "culturally respectful, authentic geo us az visual cues, high-quality "
    "portrait, realistic lighting, clean composition, culturally respectful, "
    "family-friendly, culturally respectful"
)


def test_reported_gun_prompt_is_hard_blocked() -> None:
    safety = SafetyService()

    allowed, reason = safety.check_keywords(EXACT_REPORTED_PROMPT)

    assert not allowed
    assert "weapons" in reason.lower()
    assert "gun" in reason.lower() or "firearm" in reason.lower()


def test_firearm_and_weapon_aliases_are_hard_blocked() -> None:
    safety = SafetyService()

    blocked_prompts = [
        "woman holding a rifle in a fashion portrait",
        "man posing with a pistol",
        "editorial portrait with a firearm",
        "person carrying a shotgun",
        "model holding a weapon",
        "portrait with ammunition on the table",
    ]

    for prompt in blocked_prompts:
        allowed, reason = safety.check_keywords(prompt)
        assert not allowed, prompt
        assert "weapons" in reason.lower()


def test_blood_and_gore_are_hard_blocked() -> None:
    safety = SafetyService()

    blocked_prompts = [
        "portrait with blood on the face",
        "bloody fashion scene",
        "person bleeding from an open wound",
        "gory cinematic portrait",
        "blood splatter on clothing",
        "open wounds visible on the subject",
    ]

    for prompt in blocked_prompts:
        allowed, reason = safety.check_keywords(prompt)
        assert not allowed, prompt
        assert "violence" in reason.lower()


def test_safe_body_arms_and_arizona_are_not_false_positives() -> None:
    safety = SafetyService()

    safe_prompts = [
        "woman with arms crossed, professional portrait",
        "woman in Arizona desert, realistic editorial photography",
        "athlete raising both arms in celebration",
        "fashion portrait with a camera as a prop",
    ]

    for prompt in safe_prompts:
        allowed, reason = safety.check_keywords(prompt)
        assert allowed, reason


def test_safe_prompt_reinforces_no_weapons_or_gore() -> None:
    safety = SafetyService()
    prompt = safety.build_safe_prompt("adult fashion editorial portrait").lower()

    assert "do not include guns" in prompt
    assert "firearms" in prompt
    assert "blood" in prompt
    assert "gore" in prompt


def test_safety_policy_version_is_explicit() -> None:
    assert SAFETY_POLICY_VERSION == "2026-09-21-no-weapons-blood-gore-v1"
