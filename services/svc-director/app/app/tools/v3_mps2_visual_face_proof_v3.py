from __future__ import annotations

import asyncio
import os
import re
from typing import Any
from uuid import UUID

from df_contracts.v3.director import PlannedParticipant

from app.tools import v3_mps2_visual_face_proof_v2 as proof


REQUIRED_CREDITS = 10
TEST_USER_EMAIL = str(
    os.getenv("DF_V3_E2E_TEST_USER_EMAIL") or "test_apple_iap_test1@desifaces.ai"
).strip().lower()
_ALLOWED_GENDERS = {"male", "female"}
_SENSITIVE_KEY_TOKENS = {
    "account", "billing", "credential", "email", "password", "payment",
    "phone", "secret", "token", "user",
}
_VISUAL_PRIORITY = (
    "identity brief",
    "portrait framing",
    "expression",
    "face shape",
    "brows",
    "eyes",
    "eye colour",
    "eye color",
    "nose",
    "lips",
    "jaw and chin",
    "hair",
    "lighting",
    "distinguishing cues",
    "photorealism",
    "body reference",
    "resemblance constraint",
    "identity independence",
)
_CONTINUITY_PRIORITY = ("identity lock", "wardrobe lock")


def _sensitive(key: str) -> bool:
    tokens = {
        token for token in re.split(r"[^a-z0-9]+", str(key or "").strip().lower()) if token
    }
    return bool(tokens & _SENSITIVE_KEY_TOKENS) or "id" in tokens


def _safe_map(value: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in (value or {}).items():
        key = str(raw_key or "").strip()
        if not key or _sensitive(key) or raw_value is None:
            continue
        text = str(raw_value).strip()
        if text:
            out[key.lower()] = text[:420]
    return out


def _explicit_gender(hint: dict[str, Any]) -> str | None:
    value = str(hint.get("gender") or hint.get("gender_presentation") or "").strip().lower()
    return value if value in _ALLOWED_GENDERS else None


def _explicit_age(hint: dict[str, Any]) -> str | None:
    for key in ("age", "age_range", "age_range_code", "age_presentation"):
        value = hint.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:80]
    return None


def _append(parts: list[str], sentence: str, *, max_chars: int = 1500) -> None:
    sentence = " ".join(str(sentence or "").split()).strip()
    if not sentence:
        return
    candidate = " ".join([*parts, sentence])
    if len(candidate) <= max_chars:
        parts.append(sentence)


def compile_face_input(
    *,
    participant: PlannedParticipant,
    participant_hint: dict[str, Any] | None = None,
    language: str = "en",
    num_variants: int = 1,
) -> dict[str, Any]:
    """Identity-first Face prompt compiler for the MPS2 visual proof.

    Story mechanics do not consume the provider prompt budget. Identity geometry,
    expression, hair, lighting, distinguishing cues, photorealism and continuity
    are prioritized as complete sentences; the prompt is never blindly truncated.
    """
    hint = dict(participant_hint or {})
    gender = _explicit_gender(hint)
    age = _explicit_age(hint)
    visual = _safe_map(participant.visual_direction)
    continuity = _safe_map(participant.continuity)

    parts: list[str] = []
    _append(parts, f"Create exactly one photorealistic identity-reference portrait for {participant.display_name}.")
    if age:
        _append(parts, f"Explicit user age: {age}.")
    _append(
        parts,
        "Do not infer ethnicity, skin tone, religion, attire, occupation, socioeconomic status, facial anatomy, or personality from geography, locale, name, or family relationship.",
    )
    _append(parts, "No other story participant may appear in the image.")

    for key in _VISUAL_PRIORITY:
        value = visual.get(key)
        if value:
            _append(parts, f"{key.title()}: {value}.")
    for key in _CONTINUITY_PRIORITY:
        value = continuity.get(key)
        if value:
            _append(parts, f"{key.title()}: {value}.")

    _append(
        parts,
        "Treat the resulting face as a durable recurring-character identity reference; preserve natural pores, believable eyes, fine hair detail, realistic age detail, and non-synthetic texture.",
    )

    payload: dict[str, Any] = {
        "mode": "text-to-image",
        "language": language or "en",
        "subject_composition_code": "single_person",
        "num_variants": max(1, min(int(num_variants), 4)),
        "aspect_ratio": "9:16",
        "user_prompt": " ".join(parts),
    }
    if gender:
        payload["gender"] = gender
    return payload


async def _active_actor(pool) -> tuple[UUID, UUID]:
    if not TEST_USER_EMAIL:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=test_user_email_missing")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            with spendable as (
              select user_id,
                coalesce(sum(greatest(remaining_amount-reserved_amount,0)) filter (
                  where status='active' and (expires_at is null or expires_at>now())
                ),0)::numeric as available_credits
              from public.pricing_credit_lots
              group by user_id
            )
            select bam.user_id,bam.billing_account_id,
                   coalesce(s.available_credits,0)::numeric as available_credits,
                   lower(u.email) as email
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            join core.users u on u.id=bam.user_id
            left join spendable s on s.user_id=bam.user_id
            where bam.status='active' and ba.status='active'
              and lower(u.email)=lower($1::text)
            order by bam.is_default desc,
              case bam.role when 'owner' then 0 when 'finance_admin' then 1 else 2 end,
              bam.created_at asc
            limit 1
            """,
            TEST_USER_EMAIL,
        )
    if not row:
        raise RuntimeError(
            f"MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_not_found:{TEST_USER_EMAIL}"
        )
    available = int(row["available_credits"] or 0)
    if available < REQUIRED_CREDITS:
        raise RuntimeError(
            "MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_underfunded:"
            f"required={REQUIRED_CREDITS}:available={available}"
        )
    if str(row["email"] or "").strip().lower() != TEST_USER_EMAIL:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=canonical_test_actor_email_mismatch")

    print(f"MPS2_VISUAL_TEST_ACTOR=PASS:email={TEST_USER_EMAIL}")
    print(f"MPS2_VISUAL_ACTOR_BALANCE=PASS:available_credits={available}")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


proof._active_actor = _active_actor
proof.compile_participant_face_studio_input = compile_face_input


if __name__ == "__main__":
    try:
        asyncio.run(proof.main())
    except proof.ParticipantFaceBridgeError as exc:
        raise SystemExit(f"MPS2_VISUAL_FACE_BRIDGE_FAIL={exc}") from exc
