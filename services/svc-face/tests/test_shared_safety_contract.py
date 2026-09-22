from desifaces_shared.safety import (
    SafetyFinding,
    SafetyStatus,
    category_finding,
    decision_from_findings,
    evaluate_text_policy,
)


def test_shared_safety_fail_has_actionable_reason():
    finding = category_finding("weapons", source="image", severity=4)
    decision = decision_from_findings([finding])

    payload = decision.to_dict()
    assert payload["status"] == "FAIL"
    assert payload["allow"] is False
    assert payload["component"] == "desifaces-content-safety"
    assert payload["findings"][0]["code"] == "CONTENT_SAFETY_WEAPONS"
    assert "firearm" in payload["findings"][0]["reason"].lower()
    assert payload["findings"][0]["required_action"]


def test_shared_safety_warn_remains_allowed():
    finding = SafetyFinding(
        code="CONTENT_SAFETY_PROVIDER_UNAVAILABLE",
        status=SafetyStatus.WARN,
        title="Provider unavailable",
        reason="Temporary provider issue.",
        required_action="Retry if needed.",
    )
    decision = decision_from_findings([finding])

    assert decision.status == SafetyStatus.WARN
    assert decision.allow is True


def test_common_text_safety_is_reusable_and_actionable():
    safe = evaluate_text_policy(
        "Two colleagues discuss responsible AI adoption for a small business.",
        source="director_brief",
    )
    assert safe.allow is True
    assert safe.status == SafetyStatus.PASS

    blocked = evaluate_text_policy(
        "Create pornographic content.",
        source="director_brief",
    )
    assert blocked.allow is False
    assert blocked.status == SafetyStatus.FAIL
    assert blocked.findings
    finding = blocked.findings[0]
    assert finding.reason
    assert finding.required_action
    assert finding.metadata["source"] == "director_brief"


def test_common_text_safety_blocks_weapon_prompt_for_face_consumer():
    blocked = evaluate_text_policy(
        "Put a gun in the group portrait.",
        source="face_prompt",
    )
    assert blocked.allow is False
    assert blocked.findings[0].category == "weapons"
    assert blocked.findings[0].required_action
