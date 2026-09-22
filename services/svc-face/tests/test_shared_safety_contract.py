from desifaces_shared.safety import (
    SafetyFinding,
    SafetyStatus,
    category_finding,
    decision_from_findings,
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
