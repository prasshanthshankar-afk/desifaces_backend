from app.services.community_visual_profile import (
    CommunityVisualProfileResolver,
)


def test_india_profile_layers_on_global_premium_profile() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "IN",
            "region_code": "TN",
        }
    )

    assert profile.applied_profiles == (
        "global.premium_human",
        "community.india.premium_human",
    )
    assert profile.t2i_demographic_fragments
    assert profile.t2i_quality_fragments
    assert profile.i2i_quality_fragments
    assert profile.negative_fragments


def test_global_profile_does_not_inject_india_profile() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "US",
        }
    )

    assert profile.applied_profiles == (
        "global.premium_human",
    )
    assert not profile.t2i_demographic_fragments


def test_india_can_resolve_from_existing_region_context() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "region_code": "KERALA",
        }
    )

    assert "community.india.premium_human" in profile.applied_profiles


def test_i2i_profile_explicitly_preserves_identity() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "IN",
        }
    )

    combined = " ".join(profile.i2i_quality_fragments).lower()

    assert "exact identity" in combined
    assert "preserving the source person's exact identity" in combined


def test_quality_profile_contains_portrait_realism_constraints() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "IN",
        }
    )

    positive = " ".join(profile.t2i_quality_fragments).lower()
    negative = " ".join(profile.negative_fragments).lower()

    assert "natural skin texture" in positive
    assert "realistic hands and fingers" in positive
    assert "cinematic lighting" in positive
    assert "plastic skin" in negative
    assert "malformed hands" in negative



def test_known_non_india_country_blocks_ambiguous_india_region_codes() -> None:
    resolver = CommunityVisualProfileResolver()

    # These codes are valid India aliases but also valid US state codes.
    for region_code in ("TN", "GA", "OR", "MN", "LA"):
        profile = resolver.resolve(
            {
                "country_code": "US",
                "region_code": region_code,
            }
        )

        assert profile.applied_profiles == (
            "global.premium_human",
        )


def test_global_quality_profile_preserves_requested_camera_composition() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "US",
        }
    )

    positive = " ".join(profile.t2i_quality_fragments).lower()

    assert "camera angle" in positive
    assert "viewpoint" in positive
    assert "orientation" in positive
    assert "aspect ratio" in positive


def test_i2i_quality_never_requests_demographic_reinterpretation() -> None:
    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "IN",
            "region_code": "TN",
        }
    )

    combined = " ".join(profile.i2i_quality_fragments).lower()

    assert "exact identity" in combined
    assert "facial geometry" in combined
    assert "skin tone" in combined
    assert "gender presentation" in combined

    # Community I2I guidance must concern styling/coherence, not
    # reinterpretation of the person's ethnicity or facial anatomy.
    assert "infer or exaggerate facial anatomy" not in combined


def test_explicit_country_overrides_conflicting_resolved_region_country() -> None:
    # Simulates the real CreatorPromptService path where region_code="TN"
    # may resolve to the Tamil Nadu DB row even though the request explicitly
    # says country_code="US".
    region = {
        "code": "TN",
        "display_name": "Tamil Nadu",
        "country_code": "IN",
    }

    profile = CommunityVisualProfileResolver().resolve(
        {
            "country_code": "US",
            "region_code": "TN",
        },
        region=region,
    )

    assert profile.applied_profiles == (
        "global.premium_human",
    )


def test_region_country_can_infer_profile_when_request_country_missing() -> None:
    region = {
        "code": "TN",
        "display_name": "Tamil Nadu",
        "country_code": "IN",
    }

    profile = CommunityVisualProfileResolver().resolve(
        {
            "region_code": "TN",
        },
        region=region,
    )

    assert profile.applied_profiles == (
        "global.premium_human",
        "community.india.premium_human",
    )
