#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

CONTAINER="${FUSION_EXTENSION_CONTAINER:-df-svc-fusion-extension}"
docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $CONTAINER"; exit 2; }

echo "============================================================"
echo " desifaces DEV — EXACT PARENT VIDEO PROVIDER COGS"
echo " READ ONLY / REPROBES APPROVED AUDIO"
echo "============================================================"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"

docker exec -i "$CONTAINER" python - <<'PY'
import asyncio, json, math, os
from decimal import Decimal
from uuid import UUID
import asyncpg

from app.api.routes.v3_scene_pricing import _approved_audio_rows, _measure_audio_rows

RATES = {
    "sync3": Decimal("0.1333"),
    "veed_fabric_480p": Decimal("0.08"),
    "veed_fabric_720p": Decimal("0.15"),
    "omnihuman_v15": Decimal("0.16"),
    "kling_standard": Decimal("0.0562"),
    "kling_pro": Decimal("0.115"),
}

def d(v):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")

def obj(v):
    if v is None:
        return {}
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(v)
    except Exception:
        return {}

def provider_rate(provider, request_json, meta_json):
    provider = str(provider or "").strip().lower()
    request_json = request_json or {}
    meta_json = meta_json or {}
    if provider == "sync3":
        return RATES["sync3"], "sync3_image_to_video"
    if provider == "veed_fabric":
        resolution = str(
            request_json.get("resolution")
            or meta_json.get("resolution")
            or meta_json.get("provider_resolution")
            or ""
        ).strip().lower()
        if resolution == "480p":
            return RATES["veed_fabric_480p"], "veed_fabric_480p"
        return RATES["veed_fabric_720p"], "veed_fabric_720p_or_unknown"
    if provider == "omnihuman_v15":
        return RATES["omnihuman_v15"], "omnihuman_v15"
    if provider == "kling":
        raw = json.dumps({"request": request_json, "meta": meta_json}, sort_keys=True).lower()
        if "/pro" in raw or '"pro"' in raw:
            return RATES["kling_pro"], "kling_pro"
        if "/standard" in raw or '"standard"' in raw:
            return RATES["kling_standard"], "kling_standard"
        return RATES["kling_pro"], "kling_unknown_conservative_pro"
    return None, "unknown"

async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    async with pool.acquire() as conn:
        parents = await conn.fetch("""
            select r.id::text reservation_id,
                   r.created_at,
                   r.quote_json,
                   nullif(r.quote_json->'params'->>'external_ref_id','') stage_run_id,
                   nullif(r.quote_json->'params'->>'actual_audio_duration_sec','')::numeric priced_audio_sec,
                   nullif(r.quote_json->>'total_credits','')::numeric total_credits
            from public.pricing_credit_reservations r
            where r.created_at >= now()-interval '30 days'
              and r.status='committed'
              and r.quote_json->>'service_action'='fusion.video.generate'
              and r.quote_json->'params'->>'external_ref_type'='v3_scene_stage'
            order by r.created_at desc
        """)

        min_credit = await conn.fetchval("""
            select min(value) from (
              select p.price_money /
                nullif(coalesce(
                  nullif(p.metadata_json->>'included_credits_total','')::numeric,
                  nullif(p.metadata_json->>'grant_credits','')::numeric,
                  case when p.interval_code='yearly'
                       then t.monthly_grant_credits::numeric*12
                       else t.monthly_grant_credits::numeric end
                ),0) value
              from public.pricing_plan_prices p
              join public.pricing_tiers t on t.code=p.tier_code
              where p.is_active=true and p.is_public=true
                and upper(p.currency)='USD' and p.price_money>0
              union all
              select price_money/nullif(credits::numeric,0)
              from public.pricing_credit_packs
              where coalesce(is_active,true)=true
                and upper(currency)='USD'
                and price_money>0 and credits>0
            ) x
        """)
        min_credit = d(min_credit)

        print("minimum_realized_usd_per_credit=", min_credit, sep="")
        print()
        print("===== EXACT PARENT RECONCILIATION =====")

        all_ok = True
        for p in parents:
            stage_id = p["stage_run_id"]
            stage = await conn.fetchrow("""
                select s.stage_run_id,s.scene_id,s.workflow_id,
                       w.account_id,w.project_id
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                where s.stage_run_id=$1::uuid
            """, stage_id)
            if not stage:
                print(f"PARENT={p['reservation_id']} RESULT=FAIL stage_not_found")
                all_ok = False
                continue

            rows = await _approved_audio_rows(
                conn,
                scene_id=UUID(str(stage["scene_id"])),
                workflow_id=UUID(str(stage["workflow_id"])),
                account_id=UUID(str(stage["account_id"])),
                project_id=UUID(str(stage["project_id"])),
            )
            measured, total, lineage = await _measure_audio_rows(rows)
            by_turn = {str(x["dialogue_turn_id"]): d(x["duration_sec"]) for x in measured}

            children = await conn.fetch("""
                select sj.id::text child_job_id,
                       sj.status child_status,
                       coalesce(
                         sj.payload_json->'tags'->>'dialogue_turn_id',
                         sj.payload_json->'pricing_context'->>'segment_id',
                         sj.payload_json->'billing_context'->>'segment_id',
                         sj.payload_json->'pricing'->>'segment_id'
                       ) dialogue_turn_id
                from public.studio_jobs sj
                where sj.studio_type='fusion'
                  and (
                    sj.payload_json->'tags'->>'stage_run_id'=$1
                    or sj.payload_json->'pricing_context'->>'billing_parent_job_id'=$1
                    or sj.payload_json->'billing_context'->>'billing_parent_job_id'=$1
                    or sj.payload_json->'pricing'->>'parent_job_id'=$1
                  )
                order by sj.created_at,sj.id
            """, stage_id)

            total_cogs = Decimal("0")
            unknown = []
            attempts = 0
            billed_attempts = 0
            provider_counts = {}
            for child in children:
                turn_id = str(child["dialogue_turn_id"] or "")
                duration = by_turn.get(turn_id)
                runs = await conn.fetch("""
                    select provider,provider_status,provider_job_id,request_json,response_json,meta_json
                    from public.provider_runs
                    where job_id=$1::uuid
                    order by created_at
                """, child["child_job_id"])

                for run in runs:
                    attempts += 1
                    provider = str(run["provider"] or "").strip().lower()
                    provider_counts[provider] = provider_counts.get(provider,0)+1
                    request_json = obj(run["request_json"])
                    meta_json = obj(run["meta_json"])
                    rate, basis = provider_rate(provider, request_json, meta_json)

                    # A provider_job_id or terminal success proves provider work was submitted.
                    chargeable = bool(run["provider_job_id"]) or str(run["provider_status"] or "").lower() in {
                        "submitted","processing","running","succeeded","success","completed","complete","ready"
                    }
                    if not chargeable:
                        continue
                    billed_attempts += 1
                    if duration is None or rate is None:
                        unknown.append({
                            "child_job_id": child["child_job_id"],
                            "turn_id": turn_id,
                            "provider": provider,
                            "duration": str(duration) if duration is not None else None,
                            "basis": basis,
                            "status": run["provider_status"],
                        })
                        continue
                    total_cogs += duration * rate

            parent_total = d(p["priced_audio_sec"])
            reprobe_total = d(total)
            total_credits = d(p["total_credits"])
            revenue = total_credits * min_credit
            margin = revenue - total_cogs
            break_even_credits = int((total_cogs/min_credit).to_integral_value(rounding="ROUND_CEILING")) if min_credit>0 else None
            delta = abs(parent_total-reprobe_total)
            lineage_ok = delta <= Decimal("0.05")
            complete = lineage_ok and not unknown and len(children)>0

            print(json.dumps({
                "created_at": str(p["created_at"]),
                "reservation_id": p["reservation_id"],
                "stage_run_id": stage_id,
                "turns": len(by_turn),
                "children": len(children),
                "provider_attempts": attempts,
                "chargeable_attempts": billed_attempts,
                "providers": provider_counts,
                "priced_audio_sec": float(parent_total),
                "reprobed_audio_sec": float(reprobe_total),
                "duration_delta_sec": float(delta),
                "provider_cogs_usd": float(total_cogs.quantize(Decimal("0.0001"))),
                "credits": int(total_credits),
                "minimum_realized_revenue_usd": float(revenue.quantize(Decimal("0.0001"))),
                "minimum_realized_margin_usd": float(margin.quantize(Decimal("0.0001"))),
                "break_even_credits_at_min_realized_value": break_even_credits,
                "unknown_attempts": unknown,
                "result": "PASS" if complete else "FAIL",
            }, sort_keys=True))
            if not complete:
                all_ok = False

        print()
        print("EXACT_PARENT_VIDEO_COGS_RECONCILIATION=" + ("PASS" if all_ok else "FAIL"))
    await pool.close()

asyncio.run(main())
PY

echo
echo "============================================================"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
