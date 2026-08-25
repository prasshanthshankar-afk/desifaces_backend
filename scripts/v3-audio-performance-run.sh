#!/usr/bin/env bash
set -euo pipefail

# Paid V3 Story Audio performance certification run.
#
# Safety:
# - fixed benchmark workflow only by default
# - fresh authentication only
# - requires 28 clean pending Audio stages and zero prior attempts
# - re-previews every quote immediately before dispatch
# - requires exact total/currency to match the previously reviewed exposure
# - requires exact interactive payment confirmation
# - dispatches all 28 Audio stages concurrently
# - waits for owner-service terminal status, then syncs Director outputs concurrently
# - STOPS with all successful Audio outputs awaiting HITL review
# - does NOT approve review items and does NOT start Fusion

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
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.http "$RUN_DIR"/*.tsv "$RUN_DIR"/*.txt 2>/dev/null || true

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
psql_tsv() {
  compose exec -T desifaces-db \
    psql -X -At -F $'\t' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

now_ms() { date +%s%3N; }

echo "============================================================"
echo " V3 AUDIO PARALLEL PERFORMANCE RUN"
echo " workflow: $WORKFLOW_ID"
echo " reviewed maximum exposure: $EXPECTED_TOTAL $EXPECTED_CURRENCY"
echo " source locale: $EXPECTED_SOURCE_LOCALE"
echo " target speech locale: $EXPECTED_TARGET_LOCALE"
echo " Fusion: DISABLED"
echo " auto-approval: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"

# Runtime/service gate. No restart is performed here.
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
[[ "$audio_attempts" == "0" ]] || fail "benchmark already has Audio attempts"
[[ "$pending_audio" == "28" ]] || fail "expected 28 pending Audio stages"
[[ "$fusion_pending" == "1" ]] || fail "expected exactly one untouched pending Fusion stage"
[[ "$authored" == "$EXPECTED_SOURCE_LOCALE" ]] || fail "authored locale changed: $authored"
[[ "$participant_count" == "2" && "$configured_participants" == "2" ]] || fail "benchmark voices are not fully configured"
echo "PREPAID_SAFETY_GATE=PASS"

# Fresh auth only; discard any inherited/stale token.
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
export DF_BEARER_TOKEN

echo "AUTH_FRESH=PASS"

# Re-preview all 28 stages in parallel. This is non-billable.
mapfile -t stage_ids < <(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='pending'
order by created_at,stage_run_id;")
[[ "${#stage_ids[@]}" -eq 28 ]] || fail "stage discovery returned ${#stage_ids[@]} instead of 28"

export WORKFLOW_ID DIRECTOR_URL RUN_DIR DF_BEARER_TOKEN
printf '%s\n' "${stage_ids[@]}" | xargs -P 28 -I{} bash -c '
  stage="$1"
  start="$(date +%s%3N)"
  code="$(curl -sS -o "$RUN_DIR/preview-${stage}.json" -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-stages/$stage/pricing-preview")"
  end="$(date +%s%3N)"
  printf "%s\t%s\t%s\t%s\n" "$stage" "$code" "$start" "$end" > "$RUN_DIR/preview-${stage}.http"
' _ {}

cat "$RUN_DIR"/preview-*.http | sort > "$RUN_DIR/preview-http.tsv"
preview_failures="$(awk -F'\t' '$2 != 200 {n++} END {print n+0}' "$RUN_DIR/preview-http.tsv")"
if [[ "$preview_failures" != "0" ]]; then
  echo "PREVIEW_FAILURES=$preview_failures" >&2
  cat "$RUN_DIR/preview-http.tsv" >&2
  for f in "$RUN_DIR"/preview-*.http; do
    [[ "$(cut -f2 "$f")" == "200" ]] && continue
    stage="$(cut -f1 "$f")"
    echo "--- $stage ---" >&2
    cat "$RUN_DIR/preview-${stage}.json" >&2
  done
  exit 20
fi

python3 - "$RUN_DIR" "$EXPECTED_TOTAL" "$EXPECTED_CURRENCY" "$EXPECTED_SOURCE_LOCALE" "$EXPECTED_TARGET_LOCALE" <<'PY'
from __future__ import annotations
import glob, json, os, sys
from decimal import Decimal

run_dir, expected_total, expected_currency, source_locale, target_locale = sys.argv[1:]
rows=[]
total=Decimal('0')
currencies=set()

for path in sorted(glob.glob(os.path.join(run_dir,'preview-*.json'))):
    stage=os.path.basename(path)[len('preview-'):-len('.json')]
    with open(path,encoding='utf-8') as fh:
        payload=json.load(fh)
    inp=payload.get('studio_input') or {}
    if inp.get('source_language') != source_locale:
        raise SystemExit(f'{stage}: source_language={inp.get("source_language")!r}, expected {source_locale!r}')
    if inp.get('target_locale') != target_locale:
        raise SystemExit(f'{stage}: target_locale={inp.get("target_locale")!r}, expected {target_locale!r}')
    if inp.get('voice_locale') != target_locale:
        raise SystemExit(f'{stage}: voice_locale={inp.get("voice_locale")!r}, expected {target_locale!r}')
    if inp.get('translate') is not True:
        raise SystemExit(f'{stage}: translate must be true for {source_locale}->{target_locale}: {inp}')

    envelope=payload.get('pricing') or {}
    canonical=envelope.get('pricing') or {}
    quote=str(envelope.get('quote_id') or canonical.get('quote_id') or '')
    fingerprint=str(envelope.get('preview_fingerprint') or canonical.get('preview_fingerprint') or '')
    amount=Decimal(str(canonical.get('estimated_amount')))
    currency=str(canonical.get('currency') or '')
    if not quote:
        raise SystemExit(f'{stage}: missing quote_id')
    total += amount
    if currency:
        currencies.add(currency)
    rows.append((stage,quote,fingerprint,str(amount),currency))

if len(rows)!=28:
    raise SystemExit(f'expected 28 previews, got {len(rows)}')
if total != Decimal(expected_total):
    raise SystemExit(f'price changed: expected {expected_total}, live total {total}')
if currencies != {expected_currency}:
    raise SystemExit(f'currency mismatch: expected {expected_currency}, got {sorted(currencies)}')

out=os.path.join(run_dir,'dispatch-confirmations.tsv')
with open(out,'w',encoding='utf-8') as fh:
    for r in rows:
        fh.write('\t'.join(r)+'\n')
print(f'LIVE_AUDIO_QUOTES={len(rows)}')
print(f'LIVE_AUDIO_TOTAL={total}')
print(f'LIVE_AUDIO_CURRENCY={expected_currency}')
print('AUDIO_STUDIO_INPUT_CONTRACT=PASS')
PY

echo
echo "============================================================"
echo " PAYMENT CONFIRMATION REQUIRED"
echo " 28 Audio generations"
echo " Maximum reviewed/live total: $EXPECTED_TOTAL $EXPECTED_CURRENCY"
echo " This WILL reserve/consume credits if generation succeeds."
echo "============================================================"
read -r -p "Type exactly 'PAY $EXPECTED_TOTAL $EXPECTED_CURRENCY' to dispatch: " PAYMENT_CONFIRMATION
[[ "$PAYMENT_CONFIRMATION" == "PAY $EXPECTED_TOTAL $EXPECTED_CURRENCY" ]] || {
  echo "PAYMENT_NOT_CONFIRMED=STOP"
  exit 0
}
echo "PAYMENT_CONFIRMATION=ACCEPTED"

# Dispatch all 28 at once using the fresh quote/fingerprint pairs.
DISPATCH_WALL_START_MS="$(now_ms)"
export EXPECTED_TOTAL EXPECTED_CURRENCY
cat "$RUN_DIR/dispatch-confirmations.tsv" | xargs -P 28 -n 5 bash -c '
  stage="$1"; quote="$2"; fp="$3"; amount="$4"; currency="$5"
  body="$(jq -nc --arg q "$quote" --arg f "$fp" '{quote_id:$q,preview_fingerprint:$f,user_confirmed:true}')"
  start="$(date +%s%3N)"
  code="$(curl -sS -o "$RUN_DIR/dispatch-${stage}.json" -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" -H "Content-Type: application/json" \
    -d "$body" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-stages/$stage/dispatch")"
  end="$(date +%s%3N)"
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$stage" "$code" "$start" "$end" "$amount" "$currency" > "$RUN_DIR/dispatch-${stage}.http"
' _
DISPATCH_WALL_END_MS="$(now_ms)"
export DISPATCH_WALL_START_MS DISPATCH_WALL_END_MS

cat "$RUN_DIR"/dispatch-*.http | sort > "$RUN_DIR/dispatch-http.tsv"
dispatch_failures="$(awk -F'\t' '$2 != 200 {n++} END {print n+0}' "$RUN_DIR/dispatch-http.tsv")"
if [[ "$dispatch_failures" != "0" ]]; then
  echo "DISPATCH_FAILURES=$dispatch_failures" >&2
  cat "$RUN_DIR/dispatch-http.tsv" >&2
  for f in "$RUN_DIR"/dispatch-*.http; do
    [[ "$(cut -f2 "$f")" == "200" ]] && continue
    stage="$(cut -f1 "$f")"
    echo "--- $stage ---" >&2
    cat "$RUN_DIR/dispatch-${stage}.json" >&2
  done
  echo "HOLD: partial paid dispatch occurred; do not retry blindly." >&2
  exit 30
fi

python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import glob,json,os,sys
run_dir=sys.argv[1]
rows=[]
starts=[]
ends=[]
for http_path in glob.glob(os.path.join(run_dir,'dispatch-*.http')):
    stage,code,start,end,amount,currency=open(http_path,encoding='utf-8').read().strip().split('\t')
    starts.append(int(start)); ends.append(int(end))
    payload=json.load(open(os.path.join(run_dir,f'dispatch-{stage}.json'),encoding='utf-8'))
    job=str(payload.get('audio_job_id') or '')
    attempt=str(payload.get('attempt_id') or '')
    if not job or not attempt:
        raise SystemExit(f'{stage}: dispatch response missing job/attempt: {payload}')
    rows.append((stage,job,attempt,start,end,amount,currency))
if len(rows)!=28:
    raise SystemExit(f'expected 28 dispatches, got {len(rows)}')
with open(os.path.join(run_dir,'audio-jobs.tsv'),'w',encoding='utf-8') as fh:
    for r in sorted(rows): fh.write('\t'.join(r)+'\n')
print('AUDIO_DISPATCHES=28')
print(f'CLIENT_DISPATCH_START_SPREAD_MS={max(starts)-min(starts)}')
print(f'CLIENT_DISPATCH_RESPONSE_SPREAD_MS={max(ends)-min(ends)}')
print(f'CLIENT_DISPATCH_WALL_MS={max(ends)-min(starts)}')
PY

attempts_now="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
[[ "$attempts_now" == "28" ]] || fail "expected 28 Director Audio attempts after dispatch, got $attempts_now"
echo "DIRECTOR_ATTEMPTS=28"

# Poll all owner-service jobs concurrently without calling Director sync.
# This captures actual owner-service completion independently of page/client state.
export AUDIO_URL
python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import concurrent.futures, json, os, sys, time, urllib.request, urllib.error

run_dir=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
base=os.environ['AUDIO_URL'].rstrip('/')
jobs={}
for line in open(os.path.join(run_dir,'audio-jobs.tsv'),encoding='utf-8'):
    stage,job,attempt,start,end,amount,currency=line.rstrip('\n').split('\t')
    jobs[job]={'stage':stage,'attempt':attempt,'dispatch_start_ms':int(start),'dispatch_end_ms':int(end),'observed':[]}

terminal_success={'succeeded'}
terminal_failure={'failed','blocked','canceled','cancelled'}
start=time.time()
last_print=0.0

def get_status(job_id):
    req=urllib.request.Request(
        f'{base}/api/audio/jobs/{job_id}/status',
        headers={'Authorization':f'Bearer {token}','Accept':'application/json'},
    )
    with urllib.request.urlopen(req,timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))

pending=set(jobs)
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
    while pending:
        if time.time()-start > 900:
            raise SystemExit(f'timeout waiting for {len(pending)} Audio jobs')
        futures={ex.submit(get_status,j):j for j in list(pending)}
        now_ms=int(time.time()*1000)
        failures=[]
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
            if status in terminal_success:
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
                failures.append(j)
        elapsed=time.time()-start
        if elapsed-last_print >= 5 or not pending:
            succeeded=sum(1 for v in jobs.values() if v.get('terminal_status')=='succeeded')
            failed=sum(1 for v in jobs.values() if v.get('terminal_status') in terminal_failure)
            print(f'OWNER_PROGRESS succeeded={succeeded}/28 failed={failed} pending={len(pending)} elapsed_s={elapsed:.1f}',flush=True)
            last_print=elapsed
        if failures:
            break
        if pending:
            time.sleep(0.5)

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

# Sync all successful owner jobs back to Director concurrently. Sync is non-billable.
cut -f1 "$RUN_DIR/audio-jobs.tsv" | xargs -P 28 -I{} bash -c '
  stage="$1"
  start="$(date +%s%3N)"
  code="$(curl -sS -o "$RUN_DIR/sync-${stage}.json" -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-stages/$stage/sync")"
  end="$(date +%s%3N)"
  printf "%s\t%s\t%s\t%s\n" "$stage" "$code" "$start" "$end" > "$RUN_DIR/sync-${stage}.http"
' _ {}

cat "$RUN_DIR"/sync-*.http | sort > "$RUN_DIR/sync-http.tsv"
sync_failures="$(awk -F'\t' '$2 != 200 {n++} END {print n+0}' "$RUN_DIR/sync-http.tsv")"
if [[ "$sync_failures" != "0" ]]; then
  echo "SYNC_FAILURES=$sync_failures" >&2
  cat "$RUN_DIR/sync-http.tsv" >&2
  exit 40
fi

python3 - "$RUN_DIR" <<'PY'
import glob,json,os,sys
run_dir=sys.argv[1]
count=0
for path in glob.glob(os.path.join(run_dir,'sync-*.json')):
    p=json.load(open(path,encoding='utf-8'))
    if p.get('provider_state')!='succeeded' or p.get('stage_state')!='awaiting_review' or not p.get('review_item_id'):
        raise SystemExit(f'unexpected sync payload {path}: {p}')
    count += 1
if count!=28: raise SystemExit(f'expected 28 sync payloads, got {count}')
print('DIRECTOR_SYNC_AWAITING_REVIEW=28/28')
PY

# Canonical stage/review gate.
awaiting_review="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='awaiting_review';")"
attempt_succeeded="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and a.state='succeeded';")"
active_outputs="$(psql_scalar "
select count(*)
from public.v3_studio_stage_outputs o
join public.v3_studio_stage_runs s on s.stage_run_id=o.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and o.is_active=true;")"
pending_reviews="$(psql_scalar "
select count(*)
from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='pending';")"
[[ "$awaiting_review" == "28" ]] || fail "awaiting_review=$awaiting_review, expected 28"
[[ "$attempt_succeeded" == "28" ]] || fail "succeeded attempts=$attempt_succeeded, expected 28"
[[ "$active_outputs" == "28" ]] || fail "active outputs=$active_outputs, expected 28"
[[ "$pending_reviews" == "28" ]] || fail "pending reviews=$pending_reviews, expected 28"

echo "CANONICAL_AUDIO_OUTPUT_GATE=PASS"

# Extract server-side timing and pricing evidence for this exact run.
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
where s.workflow_id='${WORKFLOW_ID}'::uuid
  and s.stage_type='audio'
  and a.attempt_no=1
order by a.created_at,s.stage_run_id;" > "$RUN_DIR/server-timing.tsv"

python3 - "$RUN_DIR" "$EXPECTED_TOTAL" "$EXPECTED_CURRENCY" <<'PY'
from __future__ import annotations
import json,math,os,statistics,sys
from decimal import Decimal

run_dir, expected_total, expected_currency=sys.argv[1:]
rows=[]
for line in open(os.path.join(run_dir,'server-timing.tsv'),encoding='utf-8'):
    p=line.rstrip('\n').split('\t')
    if len(p)!=12:
        raise SystemExit(f'unexpected timing row: {p}')
    stage,attempt,job,a_created,a_done,j_created,claimed,j_updated,status,pricing_state,amount,currency=p
    def f(x): return float(x) if x else None
    rows.append(dict(stage=stage,attempt=attempt,job=job,a_created=f(a_created),a_done=f(a_done),j_created=f(j_created),claimed=f(claimed),j_updated=f(j_updated),status=status,pricing_state=pricing_state,amount=amount,currency=currency))
if len(rows)!=28:
    raise SystemExit(f'expected 28 timing rows, got {len(rows)}')
if any(r['status']!='succeeded' for r in rows):
    raise SystemExit('not all studio_jobs succeeded')
if any(r['claimed'] is None for r in rows):
    raise SystemExit('worker_claimed_at missing on one or more jobs')

# Pricing must be committed exactly once per Audio owner job.
not_committed=[r for r in rows if r['pricing_state']!='committed']
if not_committed:
    raise SystemExit(f'pricing not committed for {len(not_committed)} jobs: {[r["job"] for r in not_committed]}')
charges=sum((Decimal(r['amount']) for r in rows),Decimal('0'))
if charges != Decimal(expected_total):
    raise SystemExit(f'committed Audio total mismatch: expected {expected_total}, got {charges}')
if {r['currency'] for r in rows} != {expected_currency}:
    raise SystemExit(f'committed currency mismatch: {sorted({r["currency"] for r in rows})}')

attempt_times=[r['a_created'] for r in rows]
job_times=[r['j_created'] for r in rows]
claim_times=[r['claimed'] for r in rows]
queue_wait=[r['claimed']-r['j_created'] for r in rows]
owner_total=[r['j_updated']-r['j_created'] for r in rows]
worker_proc=[r['j_updated']-r['claimed'] for r in rows]

# Exact max overlap of claimed->terminal intervals proves actual worker concurrency.
events=[]
for r in rows:
    events.append((r['claimed'],1))
    events.append((r['j_updated'],-1))
events.sort(key=lambda x:(x[0],-x[1]))
cur=max_overlap=0
for _,delta in events:
    cur += delta
    max_overlap=max(max_overlap,cur)

def percentile(values,p):
    xs=sorted(values)
    if not xs: return 0.0
    k=(len(xs)-1)*p
    lo=math.floor(k); hi=math.ceil(k)
    if lo==hi: return xs[lo]
    return xs[lo]*(hi-k)+xs[hi]*(k-lo)

def metric(name,values):
    print(f'{name}_P50_MS={percentile(values,0.50):.1f}')
    print(f'{name}_P95_MS={percentile(values,0.95):.1f}')
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
if max_overlap <= 1:
    raise SystemExit('PERFORMANCE_FAIL: actual Audio worker execution was serial')
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
