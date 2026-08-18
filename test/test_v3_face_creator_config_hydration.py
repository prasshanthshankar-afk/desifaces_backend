from app.domain.creator_platform_models import RegionDB, UseCaseDB


def test_use_case_text_fields_normalize_legacy_list_shape():
    row = UseCaseDB(
        id="00000000-0000-0000-0000-000000000001",
        code="test_use_case",
        display_name={"en": "Test"},
        category="test",
        prompt_base="test prompt",
        mood_descriptors=["warm", "natural"],
        target_audience=["general"],
        recommended_formats=[],
        industry_focus=[],
        created_at="2026-08-18T00:00:00Z",
    )
    assert row.mood_descriptors == "warm, natural"
    assert row.target_audience == "general"


def test_region_model_does_not_require_legacy_demographic_columns():
    row = RegionDB(
        id="00000000-0000-0000-0000-000000000002",
        code="nri_global",
        display_name={"en": "Global diaspora"},
        prompt_base="Geography may inform setting only.",
        cultural_markers={},
        is_active=True,
        sort_order=0,
    )
    assert row.sub_region is None
    assert row.ethnicity_notes is None
    assert row.typical_skin_tones == []
