#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="${EXT_API:-df-v3-svc-fusion-extension}"
FACE_WORKER="${FACE_WORKER:-df-v3-svc-face-worker}"
FUSION_WORKER="${FUSION_WORKER:-df-v3-svc-fusion-worker}"

for c in "$EXT_API" "$FACE_WORKER" "$FUSION_WORKER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c" >&2; exit 2; }
done

echo "============================================================"
echo " desifaces V3 — FACE + VIDEO PERFORMANCE PROOF (READ ONLY)"
echo "============================================================"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo
echo "===== 1. RUNTIME CONFIG — NON-SECRET ====="
python_env(){
  local c="$1" key="$2"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" | awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/,""); print; found=1} END{if(!found) print "<unset>"}'
}
printf 'face_variant_concurrency=%s\n' "$(python_env "$FACE_WORKER" DF_FACE_VARIANT_CONCURRENCY)"
printf 'face_image_model_t2i=%s\n' "$(python_env "$FACE_WORKER" OPENAI_IMAGE_MODEL_T2I)"
printf 'face_image_quality=%s\n' "$(python_env "$FACE_WORKER" OPENAI_IMAGE_QUALITY)"
printf 'face_output_moderation_retries=%s\n' "$(python_env "$FACE_WORKER" DF_FACE_OUTPUT_MODERATION_RETRIES)"
printf 'kling_standard_model=%s\n' "$(python_env "$FUSION_WORKER" FAL_KLING_AVATAR_STANDARD_MODEL)"
printf 'kling_pro_model=%s\n' "$(python_env "$FUSION_WORKER" FAL_KLING_AVATAR_PRO_MODEL)"
printf 'kling_premium_use_pro=%s\n' "$(python_env "$FUSION_WORKER" KLING_TALKING_VIDEO_PREMIUM_USE_PRO)"
printf 'fusion_provider_timeout=%s\n' "$(python_env "$FUSION_WORKER" DF_FUSION_PROVIDER_TIMEOUT_SECONDS)"
printf 'fal_key_present=%s\n' "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$FUSION_WORKER" | awk -F= '$1=="FAL_KEY" || $1=="FAL_API_KEY" {if(length($2)>0) x=1} END{print x?"yes":"no"}')"
printf 'heygen_key_present=%s\n' "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$FUSION_WORKER" | awk -F= '$1=="HEYGEN_API_KEY" || $1=="DF_HEYGEN_API_KEY" {if(length($2)>0) x=1} END{print x?"yes":"no"}')"

echo
echo "===== 2. DATABASE TIMING PROOF ====="
docker exec -i "$EXT_API" python - <<'PY'
import asyncio, json
from datetime import datetime, timezone
from app.db import get_db_pool


def jd(v):
    if isinstance(v, dict): return v
    if not v: return {}
    try: return json.loads(v)
    except Exception: return {}

def age(a,b=None):
    if not a: return None
    b=b or datetime.now(timezone.utc)
    if getattr(a,'tzinfo',None) is None: a=a.replace(tzinfo=timezone.utc)
    if getattr(b,'tzinfo',None) is None: b=b.replace(tzinfo=timezone.utc)
    return round((b-a).total_seconds(),1)

def clean_error(v, n=260):
    s=str(v or '').replace('\n',' ').strip()
    return s[:n]

async def table_cols(c, table):
    rows=await c.fetch("select column_name from information_schema.columns where table_schema='public' and table_name=$1",table)
    return {r['column_name'] for r in rows}

async def main():
    p=await get_db_pool()
    async with p.acquire() as c:
        async with c.transaction(readonly=True):
            now=datetime.now(timezone.utc)
            print('--- FACE: latest jobs ---')
            faces=await c.fetch("""
              select id::text,status,created_at,updated_at,payload_json,meta_json,error_code,error_message
              from public.studio_jobs
              where studio_type='face'
              order by created_at desc limit 5
            """)
            for i,r in enumerate(faces,1):
                payload=jd(r['payload_json']); meta=jd(r['meta_json']); vs=jd(meta.get('variants_state'))
                requested=int(meta.get('variants_requested') or payload.get('num_variants') or 1)
                states=[]
                for k in sorted(vs,key=lambda x:int(x) if str(x).isdigit() else 999):
                    v=jd(vs[k]); states.append(f"v{k}:{v.get('status','?')}" + (f":{clean_error(v.get('error_message'),120)}" if v.get('error_message') else ''))
                outs=await c.fetch("select variant_number from public.face_job_outputs where job_id=$1::uuid order by variant_number",r['id'])
                print(f"FACE_JOB[{i}] id={r['id']} status={r['status']} requested={requested} outputs={[int(x['variant_number']) for x in outs]} elapsed_s={age(r['created_at'],r['updated_at'])} age_now_s={age(r['created_at'],now)} error_code={r['error_code'] or ''}")
                print('  variant_state=' + (' | '.join(states) if states else '<none>'))
                runs=await c.fetch("""
                  select provider,provider_status,provider_job_id,created_at,updated_at,request_json,response_json,meta_json
                  from public.provider_runs where job_id=$1::uuid order by created_at
                """,r['id'])
                for pr in runs:
                    rq=jd(pr['request_json']); pm=jd(pr['meta_json']); rr=jd(pr['response_json'])
                    vn=rq.get('variant_number') or rq.get('variant') or '?'
                    retries=pm.get('output_moderation_retry_count')
                    print(f"  FACE_PROVIDER variant={vn} provider={pr['provider']} status={pr['provider_status']} elapsed_s={age(pr['created_at'],pr['updated_at'])} age_now_s={age(pr['created_at'],now)} moderation_retries={retries if retries is not None else ''} provider_job_id_present={'yes' if pr['provider_job_id'] else 'no'}")

            print('--- VIDEO: latest longform jobs ---')
            jobs=await c.fetch("""
              select id::text,status,created_at,updated_at,total_segments,completed_segments,error_code,error_message,tags
              from public.longform_jobs order by created_at desc limit 5
            """)
            segcols=await table_cols(c,'longform_segments')
            for i,j in enumerate(jobs,1):
                print(f"VIDEO_JOB[{i}] id={j['id']} status={j['status']} total={j['total_segments']} completed={j['completed_segments']} elapsed_s={age(j['created_at'],j['updated_at'])} age_now_s={age(j['created_at'],now)} error_code={j['error_code'] or ''}")
                select=['id::text','segment_index','status','duration_sec','locked_at','fusion_job_id::text','provider_job_id','error_code','error_message']
                if 'created_at' in segcols: select.append('created_at')
                if 'updated_at' in segcols: select.append('updated_at')
                segs=await c.fetch(f"select {','.join(select)} from public.longform_segments where job_id=$1::uuid order by segment_index",j['id'])
                for s in segs:
                    locked_age=age(s['locked_at'],now) if s['locked_at'] else None
                    print(f"  SEG idx={s['segment_index']} status={s['status']} dur={s['duration_sec']} locked_age_s={locked_age} fusion_job_id={s['fusion_job_id'] or ''} provider_job_id_present={'yes' if s['provider_job_id'] else 'no'} error={clean_error(s['error_message'],160)}")
                    fj=s['fusion_job_id']
                    if fj:
                        core=await c.fetchrow("select status,created_at,updated_at,error_code,error_message,payload_json,meta_json from public.studio_jobs where id=$1::uuid",fj)
                        if core:
                            print(f"    CORE_FUSION status={core['status']} elapsed_s={age(core['created_at'],core['updated_at'])} age_now_s={age(core['created_at'],now)} error_code={core['error_code'] or ''} error={clean_error(core['error_message'],160)}")
                        prs=await c.fetch("""
                          select provider,provider_status,provider_job_id,created_at,updated_at,request_json,response_json,meta_json
                          from public.provider_runs where job_id=$1::uuid order by created_at
                        """,fj)
                        for pr in prs:
                            rq=jd(pr['request_json']); rr=jd(pr['response_json']); pm=jd(pr['meta_json'])
                            model=rq.get('model_id') or rr.get('provider_model_name') or pm.get('provider_model_name') or ''
                            print(f"    VIDEO_PROVIDER provider={pr['provider']} model={model} status={pr['provider_status']} elapsed_s={age(pr['created_at'],pr['updated_at'])} age_now_s={age(pr['created_at'],now)} provider_job_id_present={'yes' if pr['provider_job_id'] else 'no'}")

asyncio.run(main())
PY

echo
echo "===== 3. RECENT FAILURE / RETRY SIGNALS (BOUNDED) ====="
echo "--- Face worker ---"
docker logs --since 45m "$FACE_WORKER" 2>&1 | grep -E 'Variant failed|face_provider_output_moderation_block|Processing creator platform job|Job processing failed' | tail -n 40 || true
echo "--- Fusion worker ---"
docker logs --since 45m "$FUSION_WORKER" 2>&1 | grep -E 'kling|provider|queue|submitted|processing|succeeded|failed|timeout' | tail -n 60 | sed -E 's/(api[_-]?key|authorization|bearer|token)[=:][^ ,]+/\1=<redacted>/Ig' || true

echo
echo "============================================================"
echo " PERFORMANCE PROOF COLLECTION COMPLETE — NO MUTATIONS"
echo "============================================================"
