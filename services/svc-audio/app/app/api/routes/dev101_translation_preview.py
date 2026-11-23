"""DEV-only provider translation drafts for existing Director HITL.

No canonical writes, no TTS, no billing and no approval authority.
The Director remains responsible for checking the checkpointed plan,
source fingerprint and user-approved localization snapshot.
"""
from __future__ import annotations

import os
from typing import Literal
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_claims
from app.db import get_pool
from app.services.dev_provider_translation import (
    DevTranslationRejected,
    selected_for_dev,
    translate_approved,
)
from app.services.dev_translation_provider_adapters import (
    ProviderTranslationError,
)

router = APIRouter(
    prefix="/api/audio/dev101",
    tags=["dev101-translation"],
)


class PreviewTurn(BaseModel):
    turn_id: str = Field(min_length=1, max_length=100)
    speaker: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=1000)
    speaker_gender: Literal["male", "female"]


class TranslationPreviewIn(BaseModel):
    source_locale: str = Field(min_length=2, max_length=40)
    target_locale: str = Field(min_length=2, max_length=40)
    scene_context: str = Field(default="", max_length=4000)
    turns: list[PreviewTurn] = Field(min_length=1, max_length=12)


def validate_preview_request(body: TranslationPreviewIn) -> None:
    source = body.source_locale.strip()
    target = body.target_locale.strip()

    if (
        not source
        or not target
        or source.casefold() == "auto"
        or source.casefold() == target.casefold()
    ):
        raise ValueError("explicit_distinct_language_pair_required")

    ids = [turn.turn_id for turn in body.turns]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_dialogue_turn_ids")

    if sum(len(turn.text) for turn in body.turns) > 6000:
        raise ValueError("translation_preview_batch_too_large")


@router.post(
    "/director-translation-preview",
    include_in_schema=False,
)
async def preview_director_translation(
    body: TranslationPreviewIn,
    claims: dict = Depends(get_current_claims),
    pool: asyncpg.Pool = Depends(get_pool),
):
    runtime = os.getenv("DF_RUNTIME_ENV", "").strip().lower()
    if runtime not in {"dev", "development", "prod", "production"}:
        raise HTTPException(404, "translation_preview_unavailable")

    if runtime in {"prod", "production"}:
        if (
            os.getenv(
                "DF_PROD_TRANSLATION_PREVIEW_ENABLED", ""
            ).strip().lower() != "true"
        ):
            raise HTTPException(404, "translation_preview_unavailable")

    # Never interpret an internal service bearer as a user approval.
    if claims.get("is_service") or claims.get("token_type") == "service":
        raise HTTPException(403, "user_access_token_required")

    try:
        user_id = UUID(str(claims.get("sub") or ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(401, "invalid_user_identity") from exc

    if runtime in {"dev", "development"}:
        try:
            if not selected_for_dev(str(user_id)):
                raise HTTPException(403, "dev_translation_cohort_required")
        except DevTranslationRejected as exc:
            raise HTTPException(403, str(exc)) from exc
    else:
        # Production authorization does not rely on DEV host identity.
        # Invalid/empty allowlist entries grant no access.
        allowed_ids = set()
        configured = os.getenv(
            "DF_PROD_TRANSLATION_PREVIEW_USER_IDS", ""
        )
        for value in configured.split(","):
            try:
                allowed_ids.add(UUID(value.strip()))
            except (ValueError, TypeError, AttributeError):
                continue

        if user_id not in allowed_ids:
            raise HTTPException(
                403, "production_translation_preview_cohort_required"
            )

    try:
        validate_preview_request(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # The JWT must identify an existing user, not just contain a UUID.
    async with pool.acquire() as conn:
        user_exists = await conn.fetchval(
            "SELECT 1 FROM core.users WHERE id=$1",
            user_id,
        )
    if not user_exists:
        raise HTTPException(401, "user_not_found")

    transcript = "\n".join(
        f"{turn.speaker}: {turn.text}"
        for turn in body.turns
    )

    drafts = []
    previous_provider = None
    experimental_used = False

    for index, turn in enumerate(body.turns):
        context = {
            "scene_context": body.scene_context,
            "full_audio_script": transcript,
            "previous_turn": (
                body.turns[index - 1].text if index else ""
            ),
            "next_turn": (
                body.turns[index + 1].text
                if index + 1 < len(body.turns) else ""
            ),
        }

        try:
            translated, provider, model = await translate_approved(
                pool,
                text=turn.text,
                source_locale=body.source_locale.strip(),
                target_locale=body.target_locale.strip(),
                speaker_gender=turn.speaker_gender,
                original_context=context,
                require_natural_speech_check=True,
            )
        except DevTranslationRejected as exc:
            # Explicit DEV-only experimental route discovery for languages
            # without a certified DB direction. Never fall back from a
            # configured provider error or an explicitly rejected route.
            if (
                runtime in {"dev", "development"}
                and os.getenv(
                    "DF_DEV_GLOBAL_TRANSLATION_ENABLED", ""
                ).strip().lower() == "true"
                and str(exc) == "no_approved_translation_direction"
            ):
                from app.services.dev_global_translation import (
                    DevGlobalRouteUnavailable,
                    translate_global_dev_draft,
                )
                try:
                    translated, provider, model = (
                        await translate_global_dev_draft(
                            pool,
                            text=turn.text,
                            source_locale=body.source_locale.strip(),
                            target_locale=body.target_locale.strip(),
                            speaker_gender=turn.speaker_gender,
                            original_context=context,
                        )
                    )
                    experimental_used = True
                except DevGlobalRouteUnavailable as candidate_exc:
                    raise HTTPException(
                        422, str(candidate_exc)
                    ) from candidate_exc
                except (httpx.HTTPError, ProviderTranslationError) as provider_exc:
                    raise HTTPException(
                        502, "translation_provider_unavailable"
                    ) from provider_exc
            else:
                raise HTTPException(422, str(exc)) from exc
        except (httpx.HTTPError, ProviderTranslationError) as exc:
            raise HTTPException(
                502, "translation_provider_unavailable"
            ) from exc

        if previous_provider is not None:
            if previous_provider != (provider, model):
                raise HTTPException(
                    409, "translation_route_changed_during_preview"
                )
        previous_provider = (provider, model)

        drafts.append({
            "turn_id": turn.turn_id,
            "speaker": turn.speaker,
            "source_text": turn.text,
            "text": translated,
        })

    return {
        "source_locale": body.source_locale.strip(),
        "target_locale": body.target_locale.strip(),
        "status": "pending_hitl",
        "approved": False,
        "provider": previous_provider[0],
        "model": previous_provider[1],
        "turns": drafts,
        "contextual_quality_certified": False,
        "experimental_translation_route": experimental_used,
    }
