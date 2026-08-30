from __future__ import annotations

import asyncio
import os
from uuid import UUID

import asyncpg

from app.services.engine.pricing_engine import quote_variant
from desifaces_shared.pricing.multi_person import select_multi_person_pricing

DB_URL = os.environ["V3_MULTI_PERSON_PRICING_DB_URL"]
TEST_USER = UUID("77777777-7777-4777-8777-777777777777")


async def _quote(conn: asyncpg.Connection, variant_code: str, params: dict[str, object]):
    return await quote_variant(
        conn,
        user_id=TEST_USER,
        variant_code=variant_code,
        params=params,
        channel="web",
        country_code="",
        currency="USD",
        billing_mode="bill",
    )


async def _run() -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            """
            insert into pricing_user_entitlements(user_id, tier_code, effective_from, metadata_json)
            values($1, 'free', now(), '{}'::jsonb)
            on conflict (user_id) do update
            set tier_code=excluded.tier_code, effective_from=excluded.effective_from
            """,
            TEST_USER,
        )

        catalog = await conn.fetch(
            """
            select code, default_unit_credits
            from pricing_skus
            where code in ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
            order by code
            """
        )
        assert [str(r["code"]) for r in catalog] == [
            "AUDIO_MULTI_PERSON",
            "FACE_MULTI_PERSON",
            "FUSION_MULTI_PERSON",
        ]

        count_specific = await conn.fetchval(
            """
            select count(*)
            from pricing_skus
            where code ~ '_(MP[2-9]|MP[1-9][0-9]+)$'
              and category in ('face','audio','fusion')
            """
        )
        assert int(count_specific or 0) == 0

        # Face: Director creates each participant identity independently. The same
        # natural variant workload therefore costs the same premium amount for a
        # 2-person or 5-person owning workflow; cast size selects the premium SKU
        # but must not multiply this individual character's variants again.
        face2 = select_multi_person_pricing(
            studio="face", participant_count_value=2, natural_units=2
        )
        face5 = select_multi_person_pricing(
            studio="face", participant_count_value=5, natural_units=2
        )
        assert face2 is not None and face5 is not None
        assert face2.sku_code == face5.sku_code == "FACE_MULTI_PERSON"
        q_face2 = await _quote(conn, face2.variant_code, {**face2.metadata, **face2.variant_params})
        q_face5 = await _quote(conn, face5.variant_code, {**face5.metadata, **face5.variant_params})
        assert q_face2.total_credits == q_face5.total_credits > 0
        assert q_face2.lines[0].qty == face2.billable_units == 2
        assert q_face5.lines[0].qty == face5.billable_units == 2

        # Audio: aggregate characters are already the workload meter, so the same
        # character volume costs the same regardless of speaker count while still
        # using one premium multi-person SKU.
        audio2 = select_multi_person_pricing(
            studio="audio", participant_count_value=2, natural_units=3
        )
        audio5 = select_multi_person_pricing(
            studio="audio", participant_count_value=5, natural_units=3
        )
        assert audio2 is not None and audio5 is not None
        assert audio2.sku_code == audio5.sku_code == "AUDIO_MULTI_PERSON"
        q_audio2 = await _quote(conn, audio2.variant_code, {**audio2.metadata, **audio2.variant_params})
        q_audio5 = await _quote(conn, audio5.variant_code, {**audio5.metadata, **audio5.variant_params})
        assert q_audio2.total_credits == q_audio5.total_credits > 0
        assert q_audio2.lines[0].qty == audio2.billable_units == 3

        # Fusion: participant-minutes are represented through the existing
        # `minutes` variant parameter passed by the policy adapter.
        fusion2 = select_multi_person_pricing(
            studio="fusion", participant_count_value=2, natural_units=2
        )
        fusion5 = select_multi_person_pricing(
            studio="fusion", participant_count_value=5, natural_units=2
        )
        assert fusion2 is not None and fusion5 is not None
        assert fusion2.sku_code == fusion5.sku_code == "FUSION_MULTI_PERSON"
        q_fusion2 = await _quote(conn, fusion2.variant_code, {**fusion2.metadata, **fusion2.variant_params})
        q_fusion5 = await _quote(conn, fusion5.variant_code, {**fusion5.metadata, **fusion5.variant_params})
        assert q_fusion5.total_credits > q_fusion2.total_credits > 0
        assert q_fusion2.lines[0].qty == fusion2.billable_units
        assert q_fusion5.lines[0].qty == fusion5.billable_units

        # The multi-person catalog rate itself is premium over its baseline SKU
        # for the same native quantity, independent of participant scaling.
        comparisons = (
            ("FACE_MULTI_PERSON", {"num_edits": 1}, "FACE_EDIT_PREMIUM_BATCH"),
            ("AUDIO_MULTI_PERSON", {"chars_1k": 1}, "AUDIO_TTS"),
            ("FUSION_MULTI_PERSON", {"minutes": 1}, "FUSION_TALKING_VIDEO"),
        )
        for premium_variant, params, baseline_variant in comparisons:
            premium = await _quote(conn, premium_variant, params)
            baseline = await _quote(conn, baseline_variant, params)
            assert premium.total_credits > baseline.total_credits > 0

    finally:
        await conn.close()


def test_postgres_multi_person_pricing_catalog_and_quotes() -> None:
    asyncio.run(_run())
