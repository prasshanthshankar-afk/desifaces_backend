#!/usr/bin/env bash
set -euo pipefail

# Paid V3 Story Audio performance certification.
# Safe to run only while the fixed benchmark is still clean.
# It re-previews the exact 28 Audio stages, requires the reviewed $0.84 USD
# exposure, dispatches all 28 concurrently using Python (no nested shell quoting),
# waits for owner-service completion, syncs all outputs concurrently into Director,
# validates one committed charge per Audio job, measures actual worker overlap,
# and STOPS with 28 Audio outputs awaiting HITL review. Fusion is never dispatched.

WORKFLOW_ID="${WORKFLOW_ID:-06c5d43e-7bbc-4cb4-aef3-9df36886da3b}"
EXPECTED_TOTAL="${EXPECTED_TOTAL:-0.84}"
EXPECTED_CURRENCY="${EXPECTED_CURRENCY:-USD}"
EXPECTED_SOURCE_LOCALE="${EXPECTED_SOURCE_LOCALE:-en-PK}"
EXPECTED_TARGET_LOCALE="${EXPECTED_TARGET_LOCALE:-ur-PK}"
DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
AUDIO_URL="${AUDIO_URL:-http://127.0.0.1:18004}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-audio-performance-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.tsv "$RUN_DIR"/*.txt 2>/dev/null || true

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
psql_tsv() {
  compose exec -T desifaces-db \
    psql -X -At -F $'\t' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
fail() { echo "ERROR: $*" >&2; exit 1; }

echo "============================================================"
echo " V3 AUDIO PARALLEL PERFORMANCE RUN"
echo " workflow: $WORKFLOW_ID"
echo " reviewed/live maximum exposure: $EXPECTED_TOTAL $EXPECTED_CURRENCY"
echo " source locale: $EXPECTED_SOURCE_LOCALE"
echo " target speech locale: $EXPECTED_TARGET_LOCALE"
echo " Fusion: DISABLED"
echo " auto-approval: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"

curl -fsS "$AUDIO_URL/api/health" >/dev/null || fail "svc-audio unhealthy"
curl -fsS "$DIRECTOR_URL/api/health" >/dev/null || fail "svc-director unhealthy"

worker_running="$(docker inspect -f '{{.State.Running}}' df-v3-svc-audio-worker 2>/dev/null || true)"
worker_restarts="$(docker inspect -f '{{.RestartCount}}' df-v3-svc-audio-worker 2>/dev/null || true)"
[[ "$worker_running" == "true" ]] || fail "df-v3-svc-audio-worker is not running"
echo "AUDIO_WORKER_RUNNING=$worker_running"
echo "AUDIO_WORKER_RESTARTS=${worker_restarts:-unknown}"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
audio_attempts="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
pending_audio="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='pending';")"
fusion_pending="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='fusion' and state='pending';")"
authored="$(psql_scalar "
select coalesce(string_agg(distinct dt.locale, ',' order by dt.locale),'')
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
voice_state="$(psql_scalar "
select count(distinct p.participant_id) || '|' ||
       count(distinct p.participant_id) filter (
         where p.voice_locale='${EXPECTED_TARGET_LOCALE}'
           and coalesce(nullif(btrim(p.voice_profile_ref),''),'')<>''
       )
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
IFS='|' read -r participant_count configured_participants <<<"$voice_state"

echo "ACTIVE_GENERATION_JOBS=$active_jobs"
echo "AUDIO_ATTEMPTS=$audio_attempts"
echo "AUDIO_PENDING=$pending_audio"
echo "FUSION_PENDING=$fusion_pending"
echo "AUTHORED_AUDIO_LOCALES=$authored"
echo "VOICE_CONFIGURED=$configured_participants/$participant_count"

[[ "$active_jobs" == "0" ]] || fail "active generation/pricing jobs exist"
[[ "$audio_attempts" == "0" ]] || fail "benchmark already has Audio attempts; do not retry blindly"
[[ "$pending_audio" == "28" ]] || fail "expected 28 pending Audio stages"
[[ "$fusion_pending" == "1" ]] || fail "expected one untouched pending Fusion stage"
[[ "$authored" == "$EXPECTED_SOURCE_LOCALE" ]] || fail "authored locale changed: $authored"
[[ "$participant_count" == "2" && "$configured_participants" == "2" ]] || fail "benchmark voices are not fully configured"
echo "PREPAID_SAFETY_GATE=PASS"

# Fresh auth only.
unset DF_BEARER_TOKEN DF_X_USER_ID || true
export DF_EMAIL CORE_URL
read -rsp "Enter test-account password: " DF_PASSWORD
echo
export DF_PASSWORD
LOGIN_EXPORTS="$(python3 scripts/df_login_exports.py)"
LOGIN_RC=$?
unset DF_PASSWORD
[[ "$LOGIN_RC" -eq 0 ]] || fail "authentication failed"
eval "$LOGIN_EXPORTS"
unset LOGIN_EXPORTS
[[ -n "${DF_BEARER_TOKEN:-}" ]] || fail "fresh bearer token missing"
export DF_BEARER_TOKEN WORKFLOW_ID AUDIO_URL DIRECTOR_URL RUN_DIR
export EXPECTED_TOTAL EXPECTED_CURRENCY EXPECTED_SOURCE_LOCALE EXPECTED_TARGET_LOCALE
echo "AUTH_FRESH=PASS"

psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='pending'
order by created_at,stage_run_id;" > "$RUN_DIR/stages.txt"
[[ "$(wc -l < "$RUN_DIR/stages.txt" | tr -d ' ')" == "28" ]] || fail "stage discovery did not return 28 stages"

# Fresh concurrent preview. Non-billable. Produces exact confirmations only after
# validating the Story Audio source/target/translation contract for every stage.
python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

run_dir=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
workflow=os.environ['WORKFLOW_ID']
base=os.environ['DIRECTOR_URL'].rstrip('/')
expected_total=Decimal(os.environ['EXPECTED_TOTAL'])
expected_currency=os.environ['EXPECTED_CURRENCY']
source_locale=os.environ['EXPECTED_SOURCE_LOCALE']
target_locale=os.environ['EXPECTED_TARGET_LOCALE']
stages=[x.strip() for x in open(os.path.join(run_dir,'stages.txt'),encoding='utf-8') if x.strip()]


def post(stage: str):
    url=f'{base}/api/director/studio-workflows/{workflow}/audio-stages/{stage}/pricing-preview'
    req=urllib.request.Request(url,data=b'',method='POST',headers={'Authorization':f'Bearer {token}','Accept':'application/json'})
    start=int(time.time()*1000)
    try:
        with urllib.request.urlopen(req,timeout=60) as resp:
            body=resp.read().decode('utf-8')
            code=resp.status
    except urllib.error.HTTPError as exc:
        body=exc.read().decode('utf-8','replace')
        code=exc.code
    end=int(time.time()*1000)
    return stage,code,start,end,body

results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
    futures=[ex.submit(post,s) for s in stages]
    for fut in concurrent.futures.as_completed(futures):
        results.append(fut.result())

bad=[r for r in results if r[1] != 200]
if bad:
    for stage,code,_,_,body in sorted(bad):
        print(f'PREVIEW_FAILURE stage={stage} HTTP={code} body={body[:1200]}',file=sys.stderr)
    raise SystemExit(20)

rows=[]
total=Decimal('0')
currencies=set()
starts=[]
for stage,code,start,end,body in sorted(results):
    starts.append(start)
    payload=json.loads(body)
    inp=payload.get('studio_input') or {}
    if inp.get('source_language') != source_locale:
        raise SystemExit(f'{stage}: source_language={inp.get("source_language")!r}, expected {source_locale!r}')
    if inp.get('target_locale') != target_locale:
        raise SystemExit(f'{stage}: target_locale={inp.get("target_locale")!r}, expected {target_locale!r}')
    if inp.get('voice_locale') != target_locale:
        raise SystemExit(f'{stage}: voice_locale={inp.get("voice_locale")!r}, expected {target_locale!r}')
    if inp.get('translate') is not True:
        raise SystemExit(f'{stage}: translate must be true: {inp}')
    envelope=payload.get('pricing') or {}
    canonical=envelope.get('pricing') or {}
    quote=str(envelope.get('quote_id') or canonical.get('quote_id') or '')
    fp=str(envelope.get('preview_fingerprint') or canonical.get('preview_fingerprint') or '')
    amount=Decimal(str(canonical.get('estimated_amount')))
    currency=str(canonical.get('currency') or '')
    if not quote:
        raise SystemExit(f'{stage}: missing quote_id')
    total += amount
    if currency: currencies.add(currency)
    rows.append((stage,quote,fp,str(amount),currency))

if len(rows) != 28: raise SystemExit(f'expected 28 previews, got {len(rows)}')
if total != expected_total: raise SystemExit(f'price changed: expected {expected_total}, got {total}')
if currencies != {expected_currency}: raise SystemExit(f'currency mismatch: {sorted(currencies)}')
with open(os.path.join(run_dir,'dispatch-confirmations.tsv'),'w',encoding='utf-8') as fh:
    for row in rows: fh.write('\t'.join(row)+'\n')
print('LIVE_AUDIO_QUOTES=28')
print(f'LIVE_AUDIO_TOTAL={total}')
print(f'LIVE_AUDIO_CURRENCY={expected_currency}')
print('AUDIO_STUDIO_INPUT_CONTRACT=PASS')
print(f'PREVIEW_REQUEST_START_SPREAD_MS={max(starts)-min(starts)}')
PY

# The caller can set PAYMENT_CONFIRMATION in the command line to avoid a second
# prompt. Otherwise prompt interactively. Exact match is required.
if [[ -z "${PAYMENT_CONFIRMATION:-}" ]]; then
  echo
echo "============================================================"
  echo " PAYMENT CONFIRMATION REQUIRED"
  echo " 28 Audio generations; exact live total $EXPECTED_TOTAL $EXPECTED_CURRENCY"
  echo "============================================================"
  read -r -p "Type exactly 'PAY $EXPECTED_TOTAL $EXPECTED_CURRENCY' to dispatch: " PAYMENT_CONFIRMATION
fi
[[ "$PAYMENT_CONFIRMATION" == "PAY $EXPECTED_TOTAL $EXPECTED_CURRENCY" ]] || {
  echo "PAYMENT_NOT_CONFIRMED=STOP"
  exit 0
}
echo "PAYMENT_CONFIRMATION=ACCEPTED"

# Robust concurrent dispatch implemented in Python. No nested bash/jq quoting.
if ! python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

run_dir=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
workflow=os.environ['WORKFLOW_ID']
base=os.environ['DIRECTOR_URL'].rstrip('/')
rows=[]
for line in open(os.path.join(run_dir,'dispatch-confirmations.tsv'),encoding='utf-8'):
    stage,quote,fp,amount,currency=line.rstrip('\n').split('\t')
    rows.append((stage,quote,fp,amount,currency))


def dispatch(row):
    stage,quote,fp,amount,currency=row
    payload=json.dumps({'quote_id':quote,'preview_fingerprint':fp or None,'user_confirmed':True}).encode('utf-8')
    url=f'{base}/api/director/studio-workflows/{workflow}/audio-stages/{stage}/dispatch'
    req=urllib.request.Request(url,data=payload,method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json','Accept':'application/json'})
    start=int(time.time()*1000)
    try:
        with urllib.request.urlopen(req,timeout=120) as resp:
            body=resp.read().decode('utf-8')
            code=resp.status
    except urllib.error.HTTPError as exc:
        body=exc.read().decode('utf-8','replace')
        code=exc.code
    except Exception as exc:
        body=json.dumps({'transport_error':str(exc)})
        code=0
    end=int(time.time()*1000)
    return stage,code,start,end,amount,currency,body

results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
    futures=[ex.submit(dispatch,row) for row in rows]
    for fut in concurrent.futures.as_completed(futures):
        results.append(fut.result())

with open(os.path.join(run_dir,'dispatch-results.json'),'w',encoding='utf-8') as fh:
    json.dump([{'stage':s,'code':c,'start_ms':st,'end_ms':en,'amount':a,'currency':cur,'body':b} for s,c,st,en,a,cur,b in results],fh,indent=2)

bad=[r for r in results if r[1] != 200]
if bad:
    for stage,code,_,_,_,_,body in sorted(bad):
        print(f'DISPATCH_FAILURE stage={stage} HTTP={code} body={body[:1200]}',file=sys.stderr)
    raise SystemExit(30)

jobs=[]; starts=[]; ends=[]
for stage,code,start,end,amount,currency,body in sorted(results):
    payload=json.loads(body)
    job=str(payload.get('audio_job_id') or '')
    attempt=str(payload.get('attempt_id') or '')
    if not job or not attempt:
        raise SystemExit(f'{stage}: dispatch response missing job/attempt: {payload}')
    starts.append(start); ends.append(end)
    jobs.append((stage,job,attempt,str(start),str(end),amount,currency))
with open(os.path.join(run_dir,'audio-jobs.tsv'),'w',encoding='utf-8') as fh:
    for row in jobs: fh.write('\t'.join(row)+'\n')
print('AUDIO_DISPATCHES=28')
print(f'CLIENT_DISPATCH_START_SPREAD_MS={max(starts)-min(starts)}')
print(f'CLIENT_DISPATCH_RESPONSE_SPREAD_MS={max(ends)-min(ends)}')
print(f'CLIENT_DISPATCH_WALL_MS={max(ends)-min(starts)}')
PY
then
  attempts_after_error="$(psql_scalar "
select count(*) from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
  active_after_error="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
  echo "HOLD_PARTIAL_DISPATCH_AUDIO_ATTEMPTS=$attempts_after_error" >&2
  echo "HOLD_PARTIAL_DISPATCH_ACTIVE_JOBS=$active_after_error" >&2
  echo "DO_NOT_RETRY_BLINDLY=YES" >&2
  exit 30
fi

attempts_now="$(psql_scalar "
select count(*) from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
[[ "$attempts_now" == "28" ]] || fail "expected 28 Director Audio attempts after dispatch, got $attempts_now"
echo "DIRECTOR_ATTEMPTS=28"

# Poll the owner-service jobs concurrently. This is read-only.
python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

run_dir=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
base=os.environ['AUDIO_URL'].rstrip('/')
jobs={}
for line in open(os.path.join(run_dir,'audio-jobs.tsv'),encoding='utf-8'):
    stage,job,attempt,start,end,amount,currency=line.rstrip('\n').split('\t')
    jobs[job]={'stage':stage,'attempt':attempt,'dispatch_start_ms':int(start),'dispatch_end_ms':int(end),'observed':[]}


def get_status(job_id):
    req=urllib.request.Request(f'{base}/api/audio/jobs/{job_id}/status',headers={'Authorization':f'Bearer {token}','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

pending=set(jobs)
terminal_failure={'failed','blocked','canceled','cancelled'}
start=time.time(); last_print=0.0
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
    while pending:
        if time.time()-start > 900:
            raise SystemExit(f'timeout waiting for {len(pending)} Audio jobs')
        futures={ex.submit(get_status,j):j for j in list(pending)}
        now_ms=int(time.time()*1000)
        saw_failure=False
        for fut,j in futures.items():
            try:
                payload=fut.result()
            except Exception as exc:
                jobs[j]['observed'].append({'at_ms':now_ms,'status':'poll_error','error':str(exc)})
                continue
            status=str(payload.get('status') or '').strip().lower()
            obs=jobs[j]['observed']
            if not obs or obs[-1].get('status') != status:
                obs.append({'at_ms':now_ms,'status':status})
            if status == 'succeeded':
                jobs[j]['terminal_status']=status
                jobs[j]['terminal_observed_ms']=now_ms
                jobs[j]['pricing']=payload.get('pricing') or {}
                pending.discard(j)
            elif status in terminal_failure:
                jobs[j]['terminal_status']=status
                jobs[j]['terminal_observed_ms']=now_ms
                jobs[j]['error_code']=payload.get('error_code')
                jobs[j]['error_message']=payload.get('error_message')
                jobs[j]['pricing']=payload.get('pricing') or {}
                pending.discard(j)
                saw_failure=True
        elapsed=time.time()-start
        if elapsed-last_print >= 5 or not pending:
            succeeded=sum(1 for v in jobs.values() if v.get('terminal_status')=='succeeded')
            failed=sum(1 for v in jobs.values() if v.get('terminal_status') in terminal_failure)
            print(f'OWNER_PROGRESS succeeded={succeeded}/28 failed={failed} pending={len(pending)} elapsed_s={elapsed:.1f}',flush=True)
            last_print=elapsed
        if saw_failure: break
        if pending: time.sleep(0.5)

with open(os.path.join(run_dir,'owner-observations.json'),'w',encoding='utf-8') as fh:
    json.dump(jobs,fh,indent=2,sort_keys=True)
failed=[(j,v) for j,v in jobs.items() if v.get('terminal_status') in terminal_failure]
if failed:
    for j,v in failed:
        print(f'OWNER_FAILURE job={j} stage={v["stage"]} status={v.get("terminal_status")} code={v.get("error_code")} message={v.get("error_message")}',file=sys.stderr)
    raise SystemExit(31)
if any(v.get('terminal_status')!='succeeded' for v in jobs.values()):
    raise SystemExit('not all owner jobs reached succeeded')
print('OWNER_AUDIO_SUCCEEDED=28/28')
PY

# Sync successful owner outputs into Director concurrently; non-billable.
python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
run_dir=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
workflow=os.environ['WORKFLOW_ID']
base=os.environ['DIRECTOR_URL'].rstrip('/')
stages=[]
for line in open(os.path.join(run_dir,'audio-jobs.tsv'),encoding='utf-8'):
    stages.append(line.split('\t',1)[0])


def sync(stage):
    url=f'{base}/api/director/studio-workflows/{workflow}/audio-stages/{stage}/sync'
    req=urllib.request.Request(url,data=b'',method='POST',headers={'Authorization':f'Bearer {token}','Accept':'application/json'})
    start=int(time.time()*1000)
    try:
        with urllib.request.urlopen(req,timeout=90) as resp:
            return stage,resp.status,start,int(time.time()*1000),resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        return stage,exc.code,start,int(time.time()*1000),exc.read().decode('utf-8','replace')

results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
    for fut in concurrent.futures.as_completed([ex.submit(sync,s) for s in stages]):
        results.append(fut.result())
bad=[r for r in results if r[1] != 200]
if bad:
    for stage,code,_,_,body in sorted(bad): print(f'SYNC_FAILURE stage={stage} HTTP={code} body={body[:1200]}',file=sys.stderr)
    raise SystemExit(40)
count=0
for stage,code,start,end,body in results:
    p=json.loads(body)
    if p.get('provider_state')!='succeeded' or p.get('stage_state')!='awaiting_review' or not p.get('review_item_id'):
        raise SystemExit(f'unexpected sync payload stage={stage}: {p}')
    count += 1
print(f'DIRECTOR_SYNC_AWAITING_REVIEW={count}/28')
PY

awaiting_review="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='awaiting_review';")"
attempt_succeeded="$(psql_scalar "
select count(*) from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and a.state='succeeded';")"
active_outputs="$(psql_scalar "
select count(*) from public.v3_studio_stage_outputs o
join public.v3_studio_stage_runs s on s.stage_run_id=o.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and o.is_active=true;")"
pending_reviews="$(psql_scalar "
select count(*) from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='pending';")"
[[ "$awaiting_review" == "28" ]] || fail "awaiting_review=$awaiting_review, expected 28"
[[ "$attempt_succeeded" == "28" ]] || fail "succeeded attempts=$attempt_succeeded, expected 28"
[[ "$active_outputs" == "28" ]] || fail "active outputs=$active_outputs, expected 28"
[[ "$pending_reviews" == "28" ]] || fail "pending reviews=$pending_reviews, expected 28"
echo "CANONICAL_AUDIO_OUTPUT_GATE=PASS"

# Server-side timing + exact pricing evidence.
psql_tsv "
select
  s.stage_run_id::text,
  a.attempt_id::text,
  a.provider_job_ref,
  extract(epoch from a.created_at)*1000,
  extract(epoch from a.completed_at)*1000,
  extract(epoch from j.created_at)*1000,
  extract(epoch from nullif(j.meta_json->>'worker_claimed_at','')::timestamptz)*1000,
  extract(epoch from j.updated_at)*1000,
  j.status,
  coalesce(j.payload_json->'pricing'->>'state',j.meta_json->'pricing'->>'state',''),
  coalesce(j.payload_json->'pricing'->>'amount',j.meta_json->'pricing'->>'amount',''),
  coalesce(j.payload_json->'pricing'->>'currency',j.meta_json->'pricing'->>'currency','')
from public.v3_studio_stage_runs s
join public.v3_studio_stage_attempts a on a.stage_run_id=s.stage_run_id
join public.studio_jobs j on j.id::text=a.provider_job_ref
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and a.attempt_no=1
order by a.created_at,s.stage_run_id;" > "$RUN_DIR/server-timing.tsv"

python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import math
import os
import sys
from decimal import Decimal
run_dir=sys.argv[1]
expected_total=Decimal(os.environ['EXPECTED_TOTAL'])
expected_currency=os.environ['EXPECTED_CURRENCY']
rows=[]
for line in open(os.path.join(run_dir,'server-timing.tsv'),encoding='utf-8'):
    p=line.rstrip('\n').split('\t')
    if len(p)!=12: raise SystemExit(f'unexpected timing row: {p}')
    stage,attempt,job,a_created,a_done,j_created,claimed,j_updated,status,pricing_state,amount,currency=p
    def f(x): return float(x) if x else None
    rows.append(dict(stage=stage,attempt=attempt,job=job,a_created=f(a_created),a_done=f(a_done),j_created=f(j_created),claimed=f(claimed),j_updated=f(j_updated),status=status,pricing_state=pricing_state,amount=amount,currency=currency))
if len(rows)!=28: raise SystemExit(f'expected 28 timing rows, got {len(rows)}')
if any(r['status']!='succeeded' for r in rows): raise SystemExit('not all studio_jobs succeeded')
if any(r['claimed'] is None for r in rows): raise SystemExit('worker_claimed_at missing')
not_committed=[r for r in rows if r['pricing_state']!='committed']
if not_committed: raise SystemExit(f'pricing not committed for {len(not_committed)} jobs')
charges=sum((Decimal(r['amount']) for r in rows),Decimal('0'))
if charges != expected_total: raise SystemExit(f'committed total mismatch: expected {expected_total}, got {charges}')
if {r['currency'] for r in rows} != {expected_currency}: raise SystemExit('committed currency mismatch')

attempt_times=[r['a_created'] for r in rows]
job_times=[r['j_created'] for r in rows]
claim_times=[r['claimed'] for r in rows]
queue_wait=[r['claimed']-r['j_created'] for r in rows]
worker_proc=[r['j_updated']-r['claimed'] for r in rows]
owner_total=[r['j_updated']-r['j_created'] for r in rows]
events=[]
for r in rows:
    events.append((r['claimed'],1)); events.append((r['j_updated'],-1))
events.sort(key=lambda x:(x[0],-x[1]))
cur=max_overlap=0
for _,delta in events:
    cur += delta; max_overlap=max(max_overlap,cur)

def pct(values,p):
    xs=sorted(values); k=(len(xs)-1)*p; lo=math.floor(k); hi=math.ceil(k)
    return xs[lo] if lo==hi else xs[lo]*(hi-k)+xs[hi]*(k-lo)
def metric(name,values):
    print(f'{name}_P50_MS={pct(values,.50):.1f}')
    print(f'{name}_P95_MS={pct(values,.95):.1f}')
    print(f'{name}_MAX_MS={max(values):.1f}')
print(f'SERVER_ATTEMPT_CREATE_SPREAD_MS={max(attempt_times)-min(attempt_times):.1f}')
print(f'OWNER_JOB_CREATE_SPREAD_MS={max(job_times)-min(job_times):.1f}')
print(f'WORKER_CLAIM_SPREAD_MS={max(claim_times)-min(claim_times):.1f}')
print(f'MAX_ACTUAL_WORKER_OVERLAP={max_overlap}')
metric('QUEUE_WAIT',queue_wait)
metric('WORKER_PROCESSING',worker_proc)
metric('OWNER_END_TO_END',owner_total)
print(f'OWNER_BATCH_ELAPSED_MS={max(r["j_updated"] for r in rows)-min(r["j_created"] for r in rows):.1f}')
print('PRICING_COMMITTED_JOBS=28')
print(f'PRICING_COMMITTED_TOTAL={charges}')
print(f'PRICING_CURRENCY={expected_currency}')
if max_overlap <= 1: raise SystemExit('PERFORMANCE_FAIL: actual Audio execution was serial')
print('ACTUAL_PARALLEL_EXECUTION=PASS')
PY

active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
[[ "$active_final" == "0" ]] || fail "active Audio work remains: $active_final"

echo
echo "============================================================"
echo " V3 AUDIO PAID PERFORMANCE RUN = PASS"
echo " Audio jobs dispatched               = 28"
echo " owner jobs succeeded                = 28"
echo " Director Audio attempts succeeded   = 28"
echo " Audio outputs awaiting HITL review  = 28"
echo " committed Audio total               = $EXPECTED_TOTAL $EXPECTED_CURRENCY"
echo " actual parallel execution           = PASS"
echo " active generation jobs              = 0"
echo " Fusion dispatch                      = NOT CALLED"
echo " HITL approval                        = NOT PERFORMED"
echo "============================================================"
echo "NEXT: review/approve the 28 Audio outputs, then price the single Fusion parent."
