from desifaces_shared.v3.participant_refs import normalize_participant_reference


def test_normalizes_llm_participant_prefix_and_slug_variants() -> None:
    names = ("Ananya", "Ravi", "Meera Iyer")
    assert normalize_participant_reference("participant:ananya", names) == "Ananya"
    assert normalize_participant_reference("PARTICIPANT/RAVI", names) == "Ravi"
    assert normalize_participant_reference("participant_meera-iyer", names) == "Meera Iyer"


def test_unknown_reference_remains_rejectable_by_canonical_validation() -> None:
    names = ("Ananya", "Ravi")
    assert normalize_participant_reference("participant:someone-else", names) == "participant:someone-else"


def test_ambiguous_slug_reference_is_not_silently_resolved() -> None:
    names = ("A B", "A-B")
    assert normalize_participant_reference("participant:a-b", names) == "participant:a-b"
