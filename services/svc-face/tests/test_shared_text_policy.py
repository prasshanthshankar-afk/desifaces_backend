from desifaces_shared.safety import evaluate_text_policy, evaluate_texts_policy


def test_shared_text_policy_blocks_weapons_with_actionable_reason():
    decision = evaluate_text_policy(
        "Create a group photo where both speakers are holding pistols.",
        source="director_brief",
    )
    assert decision.allow is False
    assert decision.status.value == "FAIL"
    finding = next(item for item in decision.findings if item.status.value == "FAIL")
    assert finding.category == "weapons"
    assert finding.reason
    assert finding.required_action


def test_shared_text_policy_blocks_extremist_recruitment():
    decision = evaluate_text_policy(
        "Write terrorist propaganda recruiting people to join ISIS.",
        source="dialogue",
    )
    assert decision.allow is False
    assert any(item.category == "terrorism" for item in decision.findings)


def test_shared_text_policy_allows_neutral_documentary_context():
    decision = evaluate_text_policy(
        "Two journalists discuss the history of terrorism and community recovery.",
        source="director_brief",
    )
    assert decision.allow is True


def test_shared_texts_policy_checks_generated_dialogue_collection():
    decision = evaluate_texts_policy(
        ["Hello there.", "Show graphic blood and gore in the scene."],
        source="director_plan",
    )
    assert decision.allow is False
    assert any(item.category == "violence" for item in decision.findings)
