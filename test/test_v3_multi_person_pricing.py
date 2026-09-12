from __future__ import annotations

from pathlib import Path

import pytest

from desifaces_shared.pricing.orchestration import PricingPreviewSpec
from desifaces_shared.pricing.multi_person import (
    AUDIO_MULTI_PERSON,
    FACE_MULTI_PERSON,
    FUSION_MULTI_PERSON,
    audio_units_from_chars,
    fusion_units_from_seconds,
    participant_count,
    select_multi_person_pricing,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("studio", "sku", "quantity_param"),
    [
        ("face", FACE_MULTI_PERSON, "num_edits"),
        ("audio", AUDIO_MULTI_PERSON, "chars_1k"),
        ("fusion", FUSION_MULTI_PERSON, "minutes"),
    ],
)
def test_single_person_does_not_select_multi_person_sku(
    studio: str,
    sku: str,
    quantity_param: str,
) -> None:
    assert select_multi_person_pricing(
        studio=studio,
        participant_count_value=1,
        natural_units=3,
    ) is None


@pytest.mark.parametrize("count", [2, 3, 4, 5, 8])
@pytest.mark.parametrize(
    ("studio", "expected_sku", "quantity_param"),
    [
        ("face", FACE_MULTI_PERSON, "num_edits"),
        ("audio", AUDIO_MULTI_PERSON, "chars_1k"),
        ("fusion", FUSION_MULTI_PERSON, "minutes"),
    ],
)
def test_all_multi_person_counts_use_one_sku_per_studio_and_scale_quantity(
    count: int,
    studio: str,
    expected_sku: str,
    quantity_param: str,
) -> None:
    selection = select_multi_person_pricing(
        studio=studio,
        participant_count_value=count,
        natural_units=3,
    )
    assert selection is not None
    assert selection.sku_code == expected_sku
    assert selection.variant_code == expected_sku
    assert selection.participant_count == count
    assert selection.quantity_param == quantity_param
    assert selection.metadata["participant_count_in_sku"] is False

    if studio == "fusion":
        assert selection.billable_units == 3 * count
        assert selection.variant_params == {quantity_param: str(3 * count)}
        assert selection.metadata["participant_scaling"] == "natural_units_x_participants"
    elif studio == "face":
        # Each participant identity is already an independent premium Face job.
        # Multiplying that job again by total cast size would double-charge Face.
        assert selection.billable_units == 3
        assert selection.variant_params == {quantity_param: "3"}
        assert selection.metadata["participant_scaling"] == "per_character_natural_usage"
    else:
        # Audio is already metered by aggregate generated characters.
        assert selection.billable_units == 3
        assert selection.variant_params == {quantity_param: "3"}
        assert selection.metadata["participant_scaling"] == "aggregate_natural_usage"


def test_participant_count_is_explicit_and_not_inferred_from_conversational_prose() -> None:
    assert participant_count(2) == 2
    assert participant_count(5) == 5
    assert participant_count({"participant_count": 5}) == 5
    assert participant_count({"participants": [{}, {}, {}, {}]}) == 4
    assert participant_count({"subject_composition": "two_people"}) == 2
    assert participant_count({"pricing_context": {"multi_person": True}}) == 2
    assert participant_count('{"speaker_count": 3}') == 3
    assert participant_count({"context": '{"participant_count": 4}'}) == 4
    assert participant_count("a discussion among five friends") == 1


def test_native_meter_units_remain_studio_specific() -> None:
    assert audio_units_from_chars(1) == 1
    assert audio_units_from_chars(1000) == 1
    assert audio_units_from_chars(1001) == 2
    assert audio_units_from_chars(2500) == 3

    assert fusion_units_from_seconds(1) == 1
    assert fusion_units_from_seconds(60) == 1
    assert fusion_units_from_seconds(61) == 2
    assert fusion_units_from_seconds(121) == 3


def test_audio_preview_adapter_respects_shared_preview_spec_contract() -> None:
    # PricingPreviewSpec has sku_code + units + meta only. Multi-person Audio
    # quantity must therefore be carried in meta['chars_1k']; introducing
    # variant_code/variant_params kwargs would break preview construction.
    fields = set(PricingPreviewSpec.__dataclass_fields__)
    assert "sku_code" in fields
    assert "units" in fields
    assert "meta" in fields
    assert "variant_code" not in fields
    assert "variant_params" not in fields

    source = (
        ROOT
        / "services/svc-audio/app/app/services/multi_person_pricing_policy.py"
    ).read_text(encoding="utf-8")
    assert 'kwargs["sku_code"] = AUDIO_MULTI_PERSON' in source
    assert 'out["chars_1k"] = str(units)' in source
    assert 'kwargs["variant_code"]' not in source
    assert 'kwargs["variant_params"]' not in source


def test_audio_preview_and_reserve_share_multi_person_context() -> None:
    source = (
        ROOT
        / "services/svc-audio/app/app/services/multi_person_pricing_policy.py"
    ).read_text(encoding="utf-8")
    assert "def _multi_person_meta" in source
    assert "original_preview_spec = routes.PricingPreviewSpec" in source
    assert "original_reserve_spec = tts_module.PricingReserveSpec" in source
    assert "tts_module.PricingReserveSpec = reserve_spec_wrapped" in source
    assert '"participant_count": int(count)' in source
    assert '"participant_scaling": "aggregate_natural_usage"' in source


def test_runtime_policies_are_installed_for_face_audio_and_fusion() -> None:
    expected = {
        "services/svc-face/app/app/main.py": "install_multi_person_pricing_policy()",
        "services/svc-audio/app/app/main.py": "install_multi_person_pricing_policy()",
        "services/svc-fusion/app/app/main.py": "install_multi_person_pricing_policy()",
    }
    for rel_path, marker in expected.items():
        source = (ROOT / rel_path).read_text(encoding="utf-8")
        assert marker in source, rel_path


def test_face_and_fusion_propagate_exact_variant_quantity_to_pricing_meta() -> None:
    for rel_path in (
        "services/svc-face/app/app/services/multi_person_pricing_policy.py",
        "services/svc-fusion/app/app/services/multi_person_pricing_policy.py",
    ):
        source = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "meta.update(selection.variant_params)" in source
        assert '"meta": meta' in source


def test_migration_has_three_participant_agnostic_skus_and_native_quantity_contracts() -> None:
    source = (
        ROOT / "migrations/2026_08_30_multi_person_premium_pricing.sql"
    ).read_text(encoding="utf-8")

    for sku in (FACE_MULTI_PERSON, AUDIO_MULTI_PERSON, FUSION_MULTI_PERSON):
        assert sku in source

    # Participant counts must never proliferate catalog identifiers.
    for forbidden in ("_MP2", "_MP3", "_MP4", "_MP5"):
        assert forbidden not in source

    assert "'FACE_MULTI_PERSON',   'FACE_EDIT_PREMIUM_RUN'" in source
    assert "'AUDIO_MULTI_PERSON',  'AUDIO_TTS_1K_CHARS'" in source
    assert "'FUSION_MULTI_PERSON', 'FUSION_TALK_MIN'" in source
    assert "'num_edits', 1.25" in source
    assert "'chars_1k',  1.25" in source
    assert "'minutes',   1.25" in source
    assert "'participant_count_in_sku', false" in source
    assert "max_qty = NULL" in source


def test_runtime_policy_sources_preserve_single_person_fallback() -> None:
    for rel_path in (
        "services/svc-face/app/app/services/multi_person_pricing_policy.py",
        "services/svc-fusion/app/app/services/multi_person_pricing_policy.py",
    ):
        source = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "if count < 2:" in source
        assert "return pricing" in source

    audio_source = (
        ROOT / "services/svc-audio/app/app/services/multi_person_pricing_policy.py"
    ).read_text(encoding="utf-8")
    assert "if _participant_count_ctx.get() >= 2:" in audio_source
    assert "self.VARIANT_CODE = AUDIO_MULTI_PERSON" in audio_source
