from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from app.db import get_pool
from app.repos import locale_catalog_repo as locale_repo_module
from app.repos.locale_context_repo import LocaleContextRepository
from app.repos.tts_catalog_repo import TTSCatalogRepository
from app.services.locale_context_resolver import LocaleContextResolver
from app.services.locale_resolver import LocaleResolver
from app.services.tts_model_resolver import TTSModelResolver
from app.services.tts_resolution_planner import (
    TTSResolutionPlanError,
    TTSResolutionPlanRequest,
    TTSResolutionPlanner,
)
from app.services.tts_voice_resolver import TTSVoiceResolver


def get_locale_repository_class() -> type:
    """
    Support the repository's current canonical class name while failing
    clearly if the module contract changes.
    """
    for name in (
        "LocaleCatalogRepository",
        "LocaleCatalogRepo",
    ):
        candidate = getattr(
            locale_repo_module,
            name,
            None,
        )
        if candidate is not None:
            return candidate

    available = sorted(
        name
        for name, value in vars(
            locale_repo_module
        ).items()
        if isinstance(value, type)
    )

    raise RuntimeError(
        "locale_catalog_repository_class_not_found:"
        + ",".join(available)
    )


def require(
    condition: bool,
    message: str,
) -> None:
    if not condition:
        raise AssertionError(message)


async def resolve_and_validate(
    *,
    planner: TTSResolutionPlanner,
    label: str,
    request: TTSResolutionPlanRequest,
    expected_locale: str,
    expected_gender: str,
    expected_voice: str | None = None,
) -> dict[str, Any]:
    plan = await planner.resolve(request)

    require(
        plan.canonical_locale == expected_locale,
        (
            f"{label}:canonical_locale:"
            f"expected={expected_locale}:"
            f"actual={plan.canonical_locale}"
        ),
    )

    require(
        plan.voice_gender == expected_gender,
        (
            f"{label}:voice_gender:"
            f"expected={expected_gender}:"
            f"actual={plan.voice_gender}"
        ),
    )

    require(
        bool(plan.provider_code),
        f"{label}:missing_provider_code",
    )

    require(
        bool(plan.model_code),
        f"{label}:missing_model_code",
    )

    require(
        bool(plan.voice_name),
        f"{label}:missing_voice_name",
    )

    if expected_voice is not None:
        require(
            plan.voice_name == expected_voice,
            (
                f"{label}:voice_name:"
                f"expected={expected_voice}:"
                f"actual={plan.voice_name}"
            ),
        )

    result = asdict(plan)

    print(
        f"{label}="
        + json.dumps(
            result,
            sort_keys=True,
            default=str,
        )
    )

    return result


async def main() -> None:
    pool = await get_pool()

    try:
        locale_repository_class = (
            get_locale_repository_class()
        )

        locale_repository = (
            locale_repository_class(pool)
        )

        locale_resolver = LocaleResolver(
            locale_repository
        )

        catalog = TTSCatalogRepository(pool)

        context_resolver = LocaleContextResolver(
            LocaleContextRepository(pool)
        )

        planner = TTSResolutionPlanner(
            locale_resolver=locale_resolver,
            model_resolver=TTSModelResolver(
                catalog
            ),
            voice_resolver=TTSVoiceResolver(
                catalog
            ),
            context_resolver=context_resolver,
        )

        revision_before = (
            await catalog.get_masterdata_revision()
        )

        print(
            "masterdata_revision_before="
            f"{revision_before}"
        )

        require(
            revision_before > 0,
            "invalid_masterdata_revision",
        )

        male = await resolve_and_validate(
            planner=planner,
            label="hi_in_male",
            request=TTSResolutionPlanRequest(
                requested_locale="hi-IN",
                text_length=100,
                output_format="mp3",
                requested_gender="male",
            ),
            expected_locale="hi-IN",
            expected_gender="male",
            expected_voice=(
                "hi-IN-MadhurNeural"
            ),
        )

        female = await resolve_and_validate(
            planner=planner,
            label="hi_in_female",
            request=TTSResolutionPlanRequest(
                requested_locale="hi-IN",
                text_length=100,
                output_format="mp3",
                requested_gender="female",
            ),
            expected_locale="hi-IN",
            expected_gender="female",
            expected_voice=(
                "hi-IN-SwaraNeural"
            ),
        )

        tamil = await resolve_and_validate(
            planner=planner,
            label="ta_in_user_in_us",
            request=TTSResolutionPlanRequest(
                requested_locale="ta-IN",
                text_length=100,
                output_format="mp3",
                requested_gender="female",
                country_code="US",
                region_code="VA",
            ),
            expected_locale="ta-IN",
            expected_gender="female",
        )

        explicit_voice = (
            await resolve_and_validate(
                planner=planner,
                label="explicit_hi_voice",
                request=(
                    TTSResolutionPlanRequest(
                        requested_locale="hi-IN",
                        text_length=100,
                        output_format="mp3",
                        requested_voice=(
                            "hi-IN-MadhurNeural"
                        ),
                        requested_gender="male",
                    )
                ),
                expected_locale="hi-IN",
                expected_gender="male",
                expected_voice=(
                    "hi-IN-MadhurNeural"
                ),
            )
        )

        # Geography must not overwrite explicitly selected locale.
        require(
            tamil["canonical_locale"]
            == "ta-IN",
            "country_context_overrode_locale",
        )

        require(
            tamil["country_code"] == "US",
            "country_context_not_preserved",
        )

        # The executable provider/model is database-selected.
        require(
            male["provider_code"]
            == female["provider_code"]
            == tamil["provider_code"]
            == explicit_voice["provider_code"],
            "inconsistent_enabled_provider",
        )

        require(
            male["model_code"]
            == female["model_code"]
            == tamil["model_code"]
            == explicit_voice["model_code"],
            "inconsistent_enabled_model",
        )

        contextual_hindi = (
            await resolve_and_validate(
                planner=planner,
                label="hindi_with_in_context",
                request=(
                    TTSResolutionPlanRequest(
                        requested_locale="Hindi",
                        text_length=100,
                        output_format="mp3",
                        requested_gender="female",
                        country_code="IN",
                    )
                ),
                expected_locale="hi-IN",
                expected_gender="female",
                expected_voice=(
                    "hi-IN-SwaraNeural"
                ),
            )
        )

        require(
            contextual_hindi["country_code"]
            == "IN",
            "hindi_context_country_not_preserved",
        )

        # Generic language with NO explicit context remains generic.
        # It may fail closed today while still remaining eligible for
        # language-level multilingual providers in the future.
        # Report current generic-language behavior without guessing.
        try:
            generic_plan = await planner.resolve(
                TTSResolutionPlanRequest(
                    requested_locale="Hindi",
                    text_length=100,
                    output_format="mp3",
                    requested_gender="female",
                )
            )

            print(
                "generic_hindi_resolution="
                + json.dumps(
                    asdict(generic_plan),
                    sort_keys=True,
                    default=str,
                )
            )
        except TTSResolutionPlanError as exc:
            print(
                "generic_hindi_resolution="
                f"FAIL_CLOSED:{exc}"
            )

        revision_after = (
            await catalog.get_masterdata_revision()
        )

        print(
            "masterdata_revision_after="
            f"{revision_after}"
        )

        require(
            revision_after == revision_before,
            (
                "read_only_validation_changed_"
                "masterdata_revision"
            ),
        )

        print("live_resolution_validation=PASS")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
