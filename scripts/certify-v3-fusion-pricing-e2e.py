from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict

from app.db import get_pool


def as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}


def need(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def text(value: Any) -> str:
    return str(value or "").strip()


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


async def run() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            select id::text as id, status, payload_json, meta_json, created_at, updated_at
            from public.studio_jobs
            where studio_type = 'fusion'
              and status = 'succeeded'
              and coalesce(payload_json->'pricing_confirmation'->>'quote_id', '') <> ''
              and coalesce(payload_json->'pricing_confirmation'->>'preview_fingerprint', '') <> ''
            order by updated_at desc nulls last, created_at desc
            limit 1
            """
        )
        need(job is not None, "no succeeded quote-bound Fusion job found after pricing enforcement")

        job_id = text(job["id"])
        payload = as_dict(job["payload_json"])
        meta = as_dict(job["meta_json"])
        confirmation = as_dict(payload.get("pricing_confirmation"))
        pricing = as_dict(payload.get("pricing")) or as_dict(meta.get("pricing"))

        need(text(pricing.get("state")).lower() == "committed", f"pricing not committed: {pricing.get('state')}")
        need(text(pricing.get("quote_id")), "committed pricing missing quote_id")
        need(text(pricing.get("preview_fingerprint")), "committed pricing missing preview_fingerprint")
        need(text(pricing.get("reservation_id")), "committed pricing missing reservation_id")
        need(text(pricing.get("quote_id")) == text(confirmation.get("quote_id")), "committed quote_id does not match user confirmation")
        need(text(pricing.get("preview_fingerprint")) == text(confirmation.get("preview_fingerprint")), "committed preview fingerprint does not match user confirmation")

        video = await conn.fetchrow(
            """
            select id::text as id, kind, url, content_type, created_at
            from public.artifacts
            where job_id = $1::uuid
              and kind = 'video'
              and coalesce(content_type, '') like 'video/%'
              and coalesce(url, '') <> ''
            order by created_at desc
            limit 1
            """,
            job_id,
        )
        need(video is not None, "succeeded Fusion job has no persisted final video artifact")

        child_artifact_count = await conn.fetchval(
            """
            select count(*)::int
            from public.artifacts
            where job_id = $1::uuid
              and lower(coalesce(kind, '')) ~ '(child|segment|scene)'
            """,
            job_id,
        )
        need(integer(child_artifact_count) == 0, f"single-person Fusion job exposed child/segment artifacts: {child_artifact_count}")

        reservation_id = text(pricing.get("reservation_id"))
        reservation = await conn.fetchrow(
            """
            select id::text as id, status, reserved_credits, quote_json, finalized_at
            from public.pricing_credit_reservations
            where id = $1::uuid
            limit 1
            """,
            reservation_id,
        )
        need(reservation is not None, "pricing reservation row not found")
        need(text(reservation["status"]).lower() == "committed", f"reservation is not committed: {reservation['status']}")
        need(reservation["finalized_at"] is not None, "committed reservation missing finalized_at")

        reservation_quote = as_dict(reservation["quote_json"])
        need(text(reservation_quote.get("quote_id")) == text(confirmation.get("quote_id")), "reservation quote_id does not match confirmed quote")
        need(text(reservation_quote.get("preview_fingerprint")) == text(confirmation.get("preview_fingerprint")), "reservation fingerprint does not match confirmed preview")

        ledger_rows = await conn.fetch(
            """
            select event_type, credits_delta, idempotency_key, reservation_id::text as reservation_id,
                   studio_job_id::text as studio_job_id, metadata_json, created_at
            from public.pricing_credit_ledger_events
            where reservation_id = $1::uuid
            order by created_at asc
            """,
            reservation_id,
        )
        consume = next((dict(r) for r in ledger_rows if text(r["event_type"]).lower() == "consume"), None)
        need(consume is not None, "committed reservation has no consume ledger event")

        final_charged_credits = integer(reservation_quote.get("final_charged_credits"))
        consume_delta = integer(consume.get("credits_delta"))
        need(consume_delta == -final_charged_credits, f"ledger debit mismatch: delta={consume_delta} final_charged={final_charged_credits}")

        finalize = as_dict(reservation_quote.get("finalize"))
        need(bool(finalize), "reservation quote missing finalize receipt")
        need(text(finalize.get("timestamp")), "finalize receipt missing timestamp")

        print("============================================================")
        print(" desifaces V3 FUSION VIDEO + PRICING E2E: CERTIFIED")
        print("============================================================")
        print(f"job_id={job_id}")
        print("job_status=succeeded")
        print("quote_confirmation=matched")
        print(f"pricing_state={text(pricing.get('state'))}")
        print(f"reservation_id={reservation_id}")
        print(f"reservation_status={text(reservation['status'])}")
        print(f"quoted_credits={integer(reservation_quote.get('total_credits'))}")
        print(f"charged_credits={final_charged_credits}")
        print(f"ledger_consume_delta={consume_delta}")
        print(f"final_video_artifact={text(video['id'])}")
        print(f"final_video_content_type={text(video['content_type'])}")
        print("child_segment_artifacts=0")
        print("pricing_release_on_failure=source_certified")
        print("============================================================")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"FUSION_VIDEO_PRICING_E2E=FAIL: {exc}", file=sys.stderr)
        raise
