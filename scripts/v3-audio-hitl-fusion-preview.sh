#!/usr/bin/env bash
set -euo pipefail

# V3 benchmark gate: Audio HITL approval -> workflow advance -> Fusion parent pricing preview.
#
# Safety invariants:
# - no Audio regeneration
# - no Fusion reservation
# - no Fusion dispatch/provider generation
# - explicit operator approval required before mutating the 28 review decisions
# - Fusion remains one billable parent / zero billable children

WORKFLOW_ID="${WORKFLOW_ID:-06c5d43e-7bbc-4cb4-aef3-9df36886da3b}"
DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
AUDIO_URL="${AUDIO_URL:-http://127.0.0.1:18004}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-audio-hitl-fusion-preview-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.tsv "$RUN_DIR"/*.html 2>/dev/null || true

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
echo " V3 AUDIO HITL -> FUSION PARENT PRICE PREVIEW"
echo " workflow: $WORKFLOW_ID"
echo " Fusion reserve: DISABLED"
echo " Fusion dispatch: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
curl -fsS "$AUDIO_URL/api/health" >/dev/null || fail "svc-audio unhealthy"
curl -fsS "$DIRECTOR_URL/api/health" >/dev/null || fail "svc-director unhealthy"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
current_stage="$(psql_scalar "select current_stage from public.v3_studio_workflows where workflow_id='${WORKFLOW_ID}'::uuid;")"
audio_awaiting="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='awaiting_review';")"
audio_approved="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
audio_attempt_succeeded="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and a.state='succeeded';")"
pending_reviews="$(psql_scalar "
select count(*)
from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='pending';")"
active_outputs="$(psql_scalar "
select count(*)
from public.v3_studio_stage_outputs o
join public.v3_studio_stage_runs s on s.stage_run_id=o.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and o.is_active=true;")"
fusion_pending="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='fusion' and state='pending';")"
fusion_attempts="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='fusion';")"

printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_jobs"
printf 'WORKFLOW_CURRENT_STAGE=%s\n' "$current_stage"
printf 'AUDIO_AWAITING_REVIEW=%s\n' "$audio_awaiting"
printf 'AUDIO_APPROVED=%s\n' "$audio_approved"
printf 'AUDIO_SUCCEEDED_ATTEMPTS=%s\n' "$audio_attempt_succeeded"
printf 'AUDIO_PENDING_REVIEWS=%s\n' "$pending_reviews"
printf 'AUDIO_ACTIVE_OUTPUTS=%s\n' "$active_outputs"
printf 'FUSION_PENDING=%s\n' "$fusion_pending"
printf 'FUSION_ATTEMPTS=%s\n' "$fusion_attempts"

[[ "$active_jobs" == "0" ]] || fail "active work exists"
[[ "$current_stage" == "audio" ]] || fail "workflow is not at Audio"
[[ "$audio_awaiting" == "28" && "$audio_approved" == "0" ]] || fail "Audio review state is not 28 awaiting / 0 approved"
[[ "$audio_attempt_succeeded" == "28" ]] || fail "expected 28 succeeded Audio attempts"
[[ "$pending_reviews" == "28" && "$active_outputs" == "28" ]] || fail "Audio review/output cohort is not exactly 28"
[[ "$fusion_pending" == "1" && "$fusion_attempts" == "0" ]] || fail "Fusion is not clean and untouched"
echo "PRE_APPROVAL_GATE=PASS"

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
export DF_BEARER_TOKEN

echo "AUTH_FRESH=PASS"

# Build the operator review manifest from canonical active outputs.
psql_tsv "
select
  dt.sequence_no,
  p.display_name,
  replace(replace(dt.text_value,E'\\t',' '),E'\\n',' '),
  s.stage_run_id::text,
  r.review_item_id::text,
  o.media_id::text
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
join public.v3_studio_stage_outputs o on o.stage_run_id=s.stage_run_id and o.is_active=true
join public.v3_studio_review_items r on r.stage_run_id=s.stage_run_id and r.media_id=o.media_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
  and s.stage_type='audio'
  and s.state='awaiting_review'
  and r.decision='pending'
order by dt.sequence_no, s.stage_run_id;" > "$RUN_DIR/review-base.tsv"

[[ "$(wc -l < "$RUN_DIR/review-base.tsv" | tr -d ' ')" == "28" ]] || fail "review manifest base is not 28 rows"

export AUDIO_URL RUN_DIR
python3 - "$RUN_DIR/review-base.tsv" "$RUN_DIR/audio-review-manifest.tsv" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import urllib.request

src,dst=sys.argv[1:]
token=os.environ['DF_BEARER_TOKEN']
base=os.environ['AUDIO_URL'].rstrip('/')
rows=[]
for line in open(src,encoding='utf-8'):
    seq,name,text,stage,review,media=line.rstrip('\n').split('\t')
    rows.append((seq,name,text,stage,review,media))

def read_url(row):
    media=row[5]
    req=urllib.request.Request(
        f'{base}/api/audio/assets/{media}/read-url',
        headers={'Authorization':f'Bearer {token}','Accept':'application/json'},
    )
    with urllib.request.urlopen(req,timeout=30) as resp:
        payload=json.loads(resp.read().decode('utf-8'))
    url=str(payload.get('read_url') or '')
    if not url:
        raise RuntimeError(f'missing read_url for {media}')
    return row+(url,)

resolved=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
    for item in ex.map(read_url,rows): resolved.append(item)
resolved.sort(key=lambda x:(int(x[0]),x[3]))
with open(dst,'w',encoding='utf-8') as fh:
    fh.write('sequence\tdisplay_name\ttext\tstage_run_id\treview_item_id\tmedia_id\tread_url\n')
    for row in resolved: fh.write('\t'.join(row)+'\n')
print(f'AUDIO_REVIEW_MANIFEST={dst}')
print(f'AUDIO_REVIEW_ITEMS={len(resolved)}')
PY

echo
echo "Audio HITL manifest is ready:"
echo "  $RUN_DIR/audio-review-manifest.tsv"
echo "It contains sequence, speaker, dialogue text, review/media IDs, and fresh read URLs."
echo

if [[ -z "${AUDIO_APPROVAL_CONFIRMATION:-}" ]]; then
  read -r -p "After reviewing the benchmark outputs, type exactly 'APPROVE 28 AUDIO': " AUDIO_APPROVAL_CONFIRMATION
fi
[[ "$AUDIO_APPROVAL_CONFIRMATION" == "APPROVE 28 AUDIO" ]] || {
  echo "AUDIO_HITL_NOT_APPROVED=STOP"
  echo "Fusion pricing preview was NOT called."
  exit 0
}
echo "AUDIO_HITL_CONFIRMATION=ACCEPTED"

# Submit 28 supported Director review decisions concurrently.
export DIRECTOR_URL WORKFLOW_ID
if ! python3 - "$RUN_DIR/review-base.tsv" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import os
import sys
import urllib.error
import urllib.request

src=sys.argv[1]
token=os.environ['DF_BEARER_TOKEN']
base=os.environ['DIRECTOR_URL'].rstrip('/')
rows=[]
for line in open(src,encoding='utf-8'):
    seq,name,text,stage,review,media=line.rstrip('\n').split('\t')
    rows.append((seq,name,stage,review,media))

def approve(row):
    seq,name,stage,review,media=row
    url=f'{base}/api/director/studio-reviews/{review}'
    body=json.dumps({
        'decision':'approved',
        'feedback':'V3 measured Audio performance benchmark operator HITL approval.'
    }).encode('utf-8')
    req=urllib.request.Request(
        url,data=body,method='POST',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json','Accept':'application/json'},
    )
    try:
        with urllib.request.urlopen(req,timeout=60) as resp:
            payload=resp.read().decode('utf-8')
            return row+(resp.status,payload)
    except urllib.error.HTTPError as exc:
        return row+(exc.code,exc.read().decode('utf-8','replace'))
    except Exception as exc:
        return row+(0,str(exc))

results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=28) as ex:
    futures=[ex.submit(approve,row) for row in rows]
    for fut in concurrent.futures.as_completed(futures): results.append(fut.result())

bad=[r for r in results if r[-2] != 200]
if bad:
    for r in sorted(bad,key=lambda x:int(x[0])):
        print(f'REVIEW_APPROVAL_FAILURE seq={r[0]} name={r[1]} review={r[3]} HTTP={r[-2]} body={r[-1][:1200]}',file=sys.stderr)
    raise SystemExit(30)
print('AUDIO_REVIEW_APPROVAL_CALLS=28/28')
PY
then
  approved_now="$(psql_scalar "
select count(*) from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
  pending_now="$(psql_scalar "
select count(*) from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='pending';")"
  echo "HOLD_PARTIAL_REVIEW_AUDIO_APPROVED=$approved_now" >&2
  echo "HOLD_PARTIAL_REVIEW_PENDING=$pending_now" >&2
  echo "DO_NOT_RETRY_BLINDLY=YES" >&2
  exit 30
fi

audio_approved_after="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
review_approved_after="$(psql_scalar "
select count(*) from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='approved';")"
[[ "$audio_approved_after" == "28" && "$review_approved_after" == "28" ]] || fail "Audio HITL cohort did not become 28/28 approved"
echo "AUDIO_HITL_APPROVED=28/28"

# Advance only after the complete Audio cohort is approved.
advance_http="$(curl -sS -o "$RUN_DIR/advance.json" -w '%{http_code}' \
  -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/advance")"
[[ "$advance_http" == "200" ]] || {
  cat "$RUN_DIR/advance.json" >&2
  fail "workflow advance HTTP $advance_http"
}
workflow_stage_after="$(psql_scalar "select current_stage from public.v3_studio_workflows where workflow_id='${WORKFLOW_ID}'::uuid;")"
[[ "$workflow_stage_after" == "fusion" ]] || fail "workflow did not advance to Fusion: $workflow_stage_after"
echo "WORKFLOW_ADVANCE_AUDIO_TO_FUSION=PASS"

fusion_stage_id="$(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='fusion'
order by created_at,stage_run_id limit 1;")"
[[ -n "$fusion_stage_id" ]] || fail "Fusion stage id missing"
fusion_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${fusion_stage_id}'::uuid;")"
fusion_attempts_before_preview="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${fusion_stage_id}'::uuid;")"
active_before_preview="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
[[ "$fusion_state" == "pending" && "$fusion_attempts_before_preview" == "0" && "$active_before_preview" == "0" ]] || fail "Fusion pre-preview safety gate failed"
printf 'FUSION_STAGE_ID=%s\n' "$fusion_stage_id"
echo "FUSION_PREVIEW_SAFETY_GATE=PASS"

# One non-reserving parent pricing preview. External provider consent remains false;
# this call prices only and verifies that all child pricing is suppressed.
fusion_http="$(curl -sS -o "$RUN_DIR/fusion-preview.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"external_provider_ok":false}' \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$fusion_stage_id/pricing-preview")"
[[ "$fusion_http" == "200" ]] || {
  cat "$RUN_DIR/fusion-preview.json" >&2
  fail "Fusion pricing preview HTTP $fusion_http"
}

python3 - "$RUN_DIR/fusion-preview.json" <<'PY'
from __future__ import annotations
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))

if int(p.get('turn_count') or 0) != 28:
    raise SystemExit(f'expected 28 Fusion turns, got {p.get("turn_count")}')
if int(p.get('billable_parent_quote_count') or 0) != 1:
    raise SystemExit(f'expected one billable parent quote: {p}')
if int(p.get('billable_child_quote_count') or 0) != 0:
    raise SystemExit(f'child billable quote count must be zero: {p}')
if int(p.get('child_pricing_suppressed') or 0) != 28:
    raise SystemExit(f'expected 28 suppressed children: {p.get("child_pricing_suppressed")}')
if int(p.get('required_child_count') or 0) != 28:
    raise SystemExit(f'expected 28 required children: {p.get("required_child_count")}')
if int(p.get('preserved_child_count') or 0) != 0:
    raise SystemExit(f'fresh benchmark must have 0 preserved children: {p.get("preserved_child_count")}')

parent=p.get('parent_quote') or {}
pricing=parent.get('pricing') or {}
quote=str(pricing.get('quote_id') or '')
fp=str(pricing.get('preview_fingerprint') or '')
if not quote or not fp:
    raise SystemExit(f'parent confirmation contract missing: {parent}')
if str(pricing.get('unit_type') or '').lower() != 'minute':
    raise SystemExit(f'parent unit_type is not minute: {pricing}')
minutes=int(parent.get('billable_minutes') or pricing.get('estimated_units') or 0)
if minutes <= 0:
    raise SystemExit(f'invalid billable minutes: {parent}')
if int(pricing.get('estimated_units') or 0) != minutes:
    raise SystemExit(f'parent pricing units do not match billable minutes: {parent}')

print('FUSION_PARENT_QUOTE_COUNT=1')
print('FUSION_CHILD_BILLABLE_QUOTES=0')
print('FUSION_CHILD_PRICING_SUPPRESSED=28')
print(f'FUSION_TOTAL_AUDIO_DURATION_SEC={parent.get("total_audio_duration_sec")}')
print(f'FUSION_BILLABLE_MINUTES={minutes}')
print(f'FUSION_PARENT_QUOTE_ID={quote}')
print(f'FUSION_PARENT_PREVIEW_FINGERPRINT={fp}')
print(f'FUSION_PARENT_ESTIMATED_AMOUNT={pricing.get("estimated_amount")}')
print(f'FUSION_PARENT_CURRENCY={pricing.get("currency")}')
print(f'FUSION_PARENT_BILLING_MODE={pricing.get("billing_mode")}')
print(f'FUSION_PARENT_SETTLEMENT_MODE={pricing.get("settlement_mode")}')
print(f'FUSION_PARENT_SKU={pricing.get("sku_code")}')
print(f'FUSION_PARENT_LEAF_SKU={pricing.get("leaf_sku_code")}')
print(f'FUSION_PARENT_BEFORE_CREDITS={pricing.get("before_credits")}')
print(f'FUSION_PARENT_AFTER_ESTIMATED_CREDITS={pricing.get("after_estimated_credits")}')
PY

fusion_attempts_after_preview="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${fusion_stage_id}'::uuid;")"
active_after_preview="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
[[ "$fusion_attempts_after_preview" == "0" && "$active_after_preview" == "0" ]] || fail "Fusion preview unexpectedly created execution work"

echo
echo "============================================================"
echo " V3 AUDIO HITL + FUSION PREVIEW = PASS"
echo " Audio HITL approvals               = 28/28"
echo " workflow current stage             = fusion"
echo " Fusion parent quotes               = 1"
echo " Fusion billable child quotes       = 0"
echo " Fusion child pricing suppressed    = 28/28"
echo " Fusion attempts                    = 0"
echo " active generation jobs             = 0"
echo " Fusion reservation                 = NOT CALLED"
echo " Fusion dispatch                    = NOT CALLED"
echo " external provider consent          = NOT GRANTED"
echo "============================================================"
echo "STOP: review the exact Fusion parent exposure above before paid Fusion dispatch."
