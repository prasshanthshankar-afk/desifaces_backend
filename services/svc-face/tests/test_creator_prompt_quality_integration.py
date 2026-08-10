import asyncio

from app.services.creator_prompt_service import CreatorPromptService


class FakeSafety:
    def build_safe_prompt(self, prompt: str) -> str:
        return prompt

    def get_safety_negative_prompt(self) -> str:
        return "unsafe content"


class FakeTranslator:
    pass


class FakeConfigRepo:
    def __init__(self) -> None:
        self.formats = {
            "PORTRAIT": {
                "code": "PORTRAIT",
                "width": 1024,
                "height": 1536,
                "aspect_ratio": "2:3",
            },
            "LANDSCAPE": {
                "code": "LANDSCAPE",
                "width": 1536,
                "height": 1024,
                "aspect_ratio": "3:2",
            },
            "WIDE": {
                "code": "WIDE",
                "width": 1536,
                "height": 864,
                "aspect_ratio": "16:9",
            },
        }

        self.use_cases = {
            "CREATOR": {
                "code": "CREATOR",
                "prompt_base": "high-quality human creator photography",
            }
        }

        self.regions = {
            "TN": {
                "code": "TN",
                "display_name": "Tamil Nadu",
                "country_code": "IN",
            },
            "KL": {
                "code": "KL",
                "display_name": "Kerala",
                "country_code": "IN",
            },
            "RJ": {
                "code": "RJ",
                "display_name": "Rajasthan",
                "country_code": "IN",
            },
        }

        self.age_ranges = {
            "45_54": {
                "code": "45_54",
                "prompt_descriptor": "45 to 54 years old",
            }
        }

        self.skin_tones = {
            "MEDIUM_BROWN": {
                "code": "MEDIUM_BROWN",
                "prompt_descriptor": "medium brown skin tone",
            }
        }

        self.contexts = {
            "WEDDING": {
                "code": "WEDDING",
                "prompt_base": "elegant culturally coherent wedding environment",
            }
        }

        self.clothing = {
            "SARI": {
                "code": "SARI",
                "prompt_base": "elegant blue silk sari with realistic fabric construction",
            }
        }

    async def get_image_format_by_code(self, code):
        return self.formats.get(code)

    async def get_image_formats(self):
        return list(self.formats.values())

    async def get_use_case_by_code(self, code):
        return self.use_cases.get(code)

    async def get_use_cases(self):
        return list(self.use_cases.values())

    async def get_age_range_by_code(self, code):
        return self.age_ranges.get(code)

    async def get_region_by_code(self, code):
        return self.regions.get(code)

    async def get_skin_tone_by_code(self, code):
        return self.skin_tones.get(code)

    async def get_style_by_code(self, code):
        return None

    async def get_context_by_code(self, code):
        return self.contexts.get(code)

    async def get_clothing_by_code(self, code):
        return self.clothing.get(code)

    async def get_platform_requirements_by_code(self, code):
        return None

    async def get_variations_by_use_case(self, **kwargs):
        return {}


def build(request):
    service = CreatorPromptService(
        db_pool=None,
        safety=FakeSafety(),
        translator=FakeTranslator(),
        config_repo=FakeConfigRepo(),
    )

    return asyncio.run(
        service.build_variants(
            request_dict=dict(request),
            job_seed=123456,
        )
    )


def base_request(**overrides):
    request = {
        "mode": "text-to-image",
        "image_format_code": "PORTRAIT",
        "use_case_code": "CREATOR",
        "num_variants": 1,
        "user_prompt": "Create a natural photograph of the person.",
    }
    request.update(overrides)
    return request


def test_tamil_nadu_t2i_uses_india_and_global_quality() -> None:
    variants, _ = build(
        base_request(
            country_code="IN",
            region_code="TN",
            gender="female",
            user_prompt=(
                "Create a natural expressive photograph in Tamil Nadu."
            ),
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()

    assert variant["visual_profile"]["applied_profiles"] == [
        "global.premium_human",
        "community.india.premium_human",
    ]

    assert "from tamil nadu" in prompt
    assert "indian cultural context" in prompt
    assert "natural skin texture" in prompt
    assert "realistic hands and fingers" in prompt


def test_kerala_landscape_full_body_preserves_composition() -> None:
    user_prompt = (
        "Full-body environmental photograph beside a Kerala backwater, "
        "landscape orientation, eye-level camera, natural daylight."
    )

    variants, _ = build(
        base_request(
            country_code="IN",
            region_code="KL",
            image_format_code="WIDE",
            user_prompt=user_prompt,
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()

    assert user_prompt.lower() in prompt
    assert variant["technical_specs"]["aspect_ratio"] == "16:9"

    assert "camera angle" in prompt
    assert "viewpoint" in prompt
    assert "orientation" in prompt
    assert "aspect ratio" in prompt

    assert "premium photorealistic human imagery" in prompt
    assert "editorial portrait" not in prompt
    assert "reel-ready" not in prompt


def test_rajasthan_low_angle_environment_remains_authoritative() -> None:
    user_prompt = (
        "Environmental full-body photograph in Rajasthan, low-angle camera, "
        "wide composition, subject walking naturally through the scene."
    )

    variants, _ = build(
        base_request(
            country_code="IN",
            region_code="RJ",
            image_format_code="LANDSCAPE",
            user_prompt=user_prompt,
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()

    assert user_prompt.lower() in prompt
    assert "low-angle camera" in prompt
    assert "wide composition" in prompt
    assert variant["technical_specs"]["aspect_ratio"] == "3:2"

    assert variant["visual_profile"]["applied_profiles"] == [
        "global.premium_human",
        "community.india.premium_human",
    ]


def test_us_tennessee_does_not_activate_india_profile() -> None:
    variants, _ = build(
        base_request(
            country_code="US",
            region_code="TN",
            user_prompt=(
                "Natural environmental photograph in Tennessee."
            ),
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()
    negative = variant["negative_prompt"].lower()

    assert variant["visual_profile"]["applied_profiles"] == [
        "global.premium_human",
    ]

    assert "indian cultural context" not in prompt
    assert "stereotyped or caricatured indian appearance" not in negative
    assert "mismatched regional attire" not in negative


def test_global_landscape_stays_global_and_camera_neutral() -> None:
    user_prompt = (
        "Wide landscape environmental photograph in California at sunset, "
        "three-quarter body composition and side camera viewpoint."
    )

    variants, _ = build(
        base_request(
            country_code="US",
            image_format_code="WIDE",
            user_prompt=user_prompt,
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()

    assert variant["visual_profile"]["applied_profiles"] == [
        "global.premium_human",
    ]

    assert user_prompt.lower() in prompt
    assert "indian cultural context" not in prompt
    assert "requested shot size" in prompt
    assert "camera angle" in prompt
    assert "viewpoint" in prompt
    assert variant["technical_specs"]["aspect_ratio"] == "16:9"


def test_india_i2i_preserves_source_demographics_and_identity() -> None:
    user_edit = (
        "Change only the outfit to an elegant blue sari and update the "
        "background to a refined wedding setting."
    )

    variants, _ = build(
        base_request(
            mode="image-to-image",
            country_code="IN",
            region_code="TN",
            age_range_code="45_54",
            skin_tone_code="MEDIUM_BROWN",
            gender="female",
            context_code="WEDDING",
            clothing_style_code="SARI",
            user_prompt=user_edit,
        )
    )

    variant = variants[0]
    prompt = variant["prompt"].lower()

    assert variant["visual_profile"]["applied_profiles"] == [
        "global.premium_human",
        "community.india.premium_human",
    ]

    # Source image, not prompt demographics, owns identity.
    assert "edit the input photo: keep the same person/identity" in prompt
    assert "exact identity" in prompt
    assert "facial geometry" in prompt
    assert "skin tone and gender presentation" in prompt

    assert "45 to 54 years old" not in prompt
    assert "female person" not in prompt
    assert "from tamil nadu" not in prompt
    assert "medium brown skin tone" not in prompt

    # Requested non-identity edits and cultural-quality guidance remain.
    assert "elegant blue silk sari" in prompt
    assert "wedding environment" in prompt
    assert "regionally coherent" in prompt

    # User edit remains the final and therefore highest-specificity edit
    # instruction inside CreatorPromptService.
    assert prompt.endswith(user_edit.lower())

    negative = variant["negative_prompt"].lower()
    assert "different person" in negative
    assert "identity drift" in negative
    assert "wrong gender" in negative
