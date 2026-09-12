#!/usr/bin/env bash
set -euo pipefail

# Resume V3 Audio benchmark after catalog deployment/certification.
# Fresh-auth only. No rebuild/restart. Stops after 28 Audio pricing previews.
# No reserve, no dispatch, no provider generation, no credit commit.

WORKFLOW_ID="${WORKFLOW_ID:-06c5d43e-7bbc-4cb4-aef3-9df36886da3b}"
BENCHMARK_TARGET_LOCALE="${BENCHMARK_TARGET_LOCALE:-ur-PK}"
DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
AUDIO_URL="${AUDIO_URL:-http://127.0.0.1:18004}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-audio-benchmark-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/resume-*.json "$RUN_DIR"/price-*.json "$RUN_DIR"/price-*.http "$RUN_DIR"/pricing-http.tsv "$RUN_DIR"/audio-pricing-review-summary.tsv 2>/dev/null || true

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

echo "============================================================"
echo " V3 AUDIO BENCHMARK RESUME — FRESH AUTH + PRICING ONLY"
echo " workflow: $WORKFLOW_ID"
echo " target speech locale: $BENCHMARK_TARGET_LOCALE"
echo " rebuild/restart: NO"
echo " paid generation: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || { echo "ERROR: run from ~/workspace/desifaces-v3" >&2; exit 2; }
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || {
  echo "ERROR: wrong branch" >&2; exit 3;
}

curl -fsS "$AUDIO_URL/api/health" >/dev/null
curl -fsS "$DIRECTOR_URL/api/health" >/dev/null
echo "SERVICE_HEALTH=PASS"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
attempts="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
pending="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='pending';")"
authored="$(psql_scalar "
select coalesce(string_agg(distinct dt.locale, ',' order by dt.locale),'')
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"

echo "ACTIVE_GENERATION_JOBS=$active_jobs"
echo "AUDIO_ATTEMPTS=$attempts"
echo "AUDIO_PENDING=$pending"
echo "AUTHORED_AUDIO_LOCALES=$authored"
[[ "$active_jobs" == "0" && "$attempts" == "0" && "$pending" == "28" && "$authored" == "en-PK" ]] || {
  echo "ERROR: clean benchmark safety gate failed" >&2; exit 10;
}
echo "FIXTURE_GATE=PASS"

# ALWAYS replace any inherited/stale token.
unset DF_BEARER_TOKEN DF_X_USER_ID || true
export DF_EMAIL CORE_URL
read -rsp "Enter test-account password: " DF_PASSWORD
echo
export DF_PASSWORD
LOGIN_EXPORTS="$(python3 scripts/df_login_exports.py)"
LOGIN_RC=$?
unset DF_PASSWORD
[[ "$LOGIN_RC" -eq 0 ]] || { echo "ERROR: authentication failed" >&2; exit 20; }
eval "$LOGIN_EXPORTS"
unset LOGIN_EXPORTS
[[ -n "${DF_BEARER_TOKEN:-}" ]] || { echo "ERROR: fresh bearer token missing" >&2; exit 21; }
export DF_BEARER_TOKEN

echo "AUTH_FRESH=PASS"

# Reconfirm only the critical catalog invariants; no deploy work here.
curl -fsS -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$AUDIO_URL/api/audio/catalog/voices?locale=en-PK" > "$RUN_DIR/resume-en-pk.json"
curl -fsS -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$AUDIO_URL/api/audio/catalog/voices?locale=${BENCHMARK_TARGET_LOCALE}" > "$RUN_DIR/resume-target-voices.json"

jq -e '.items | length == 0' "$RUN_DIR/resume-en-pk.json" >/dev/null
jq -e '.items | any(((.gender // "")|ascii_downcase) == "male")' "$RUN_DIR/resume-target-voices.json" >/dev/null
jq -e '.items | any(((.gender // "")|ascii_downcase) == "female")' "$RUN_DIR/resume-target-voices.json" >/dev/null
echo "CATALOG_GATE=PASS"

participant_state="$(psql_scalar "
select
  count(distinct p.participant_id) filter (where coalesce(nullif(btrim(p.voice_profile_ref),''),'')='') || '|' ||
  count(distinct p.participant_id) filter (where coalesce(nullif(btrim(p.voice_locale),''),'')='') || '|' ||
  count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
IFS='|' read -r missing_voice missing_locale participant_count <<<"$participant_state"
[[ "$participant_count" == "2" ]] || { echo "ERROR: expected 2 speaking participants" >&2; exit 30; }

if [[ "$missing_voice" == "2" && "$missing_locale" == "2" ]]; then
  http="$(curl -sS -o "$RUN_DIR/resume-autoconfig.json" -w '%{http_code}' \
    -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-autoconfigure")"
  [[ "$http" == "200" ]] || {
    echo "ERROR: audio-autoconfigure HTTP $http" >&2
    cat "$RUN_DIR/resume-autoconfig.json" >&2
    exit 31
  }
  jq -e '.ready == false and (.characters|length)==2 and all(.characters[]; .status=="needs_user_choice")' \
    "$RUN_DIR/resume-autoconfig.json" >/dev/null
  mutated="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio'
  and (coalesce(nullif(btrim(p.voice_profile_ref),''),'')<>'' or coalesce(nullif(btrim(p.voice_locale),''),'')<>'');")"
  [[ "$mutated" == "0" ]] || { echo "ERROR: fail-closed autoconfigure mutated voice state" >&2; exit 32; }
  echo "DIRECTOR_FAIL_CLOSED=PASS"
elif [[ "$missing_voice" == "0" && "$missing_locale" == "0" ]]; then
  non_target="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio'
  and p.voice_locale <> '${BENCHMARK_TARGET_LOCALE}';")"
  [[ "$non_target" == "0" ]] || { echo "ERROR: existing voice locale is not benchmark target" >&2; exit 33; }
  echo "DIRECTOR_FAIL_CLOSED=SKIP_ALREADY_PREPARED"
else
  echo "ERROR: partial participant voice configuration; refusing repair" >&2
  exit 34
fi

# Discover the two explicit participant genders and assign only proven executable voices.
psql_scalar "
select distinct on (p.participant_id)
  p.participant_id::text || E'\\t' || replace(p.display_name,E'\\t',' ') || E'\\t' ||
  lower(coalesce(
    nullif(p.metadata_json #>> '{explicit_face_constraints,gender}',''),
    nullif(p.metadata_json #>> '{explicit_face_constraints,gender_presentation}',''),
    nullif(p.persona_json->>'gender',''),
    nullif(p.persona_json->>'gender_presentation',''),''))
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio'
order by p.participant_id,p.display_name;" > "$RUN_DIR/resume-participants.tsv"

[[ "$(wc -l < "$RUN_DIR/resume-participants.tsv" | tr -d ' ')" == "2" ]] || { echo "ERROR: participant discovery != 2" >&2; exit 40; }

while IFS=$'\t' read -r participant_id display_name gender_raw; do
  case "$gender_raw" in
    male|man|m) gender="male" ;;
    female|woman|f) gender="female" ;;
    *) echo "ERROR: explicit gender missing for $display_name" >&2; exit 41 ;;
  esac

  voice_id="$(jq -r --arg g "$gender" '
    [ .items[] | select((((.gender // "")|ascii_downcase) == $g)) ]
    | (map(select(.is_default == true)) + .)
    | .[0].voice_name // empty
  ' "$RUN_DIR/resume-target-voices.json")"
  [[ -n "$voice_id" ]] || { echo "ERROR: no executable $gender voice" >&2; exit 42; }

  payload="$(jq -nc --arg voice "$voice_id" --arg locale "$BENCHMARK_TARGET_LOCALE" '{voice_id:$voice,voice_locale:$locale}')"
  http="$(curl -sS -o "$RUN_DIR/resume-voice-${participant_id}.json" -w '%{http_code}' \
    -X PUT -H "Authorization: Bearer $DF_BEARER_TOKEN" -H 'Content-Type: application/json' \
    -d "$payload" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/participants/$participant_id/voice-profile")"
  [[ "$http" == "200" ]] || {
    echo "ERROR: voice profile HTTP $http for $display_name" >&2
    cat "$RUN_DIR/resume-voice-${participant_id}.json" >&2
    exit 43
  }
  echo "$display_name -> $voice_id ($BENCHMARK_TARGET_LOCALE)"
done < "$RUN_DIR/resume-participants.tsv"

configured="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio'
  and p.voice_locale='${BENCHMARK_TARGET_LOCALE}'
  and coalesce(nullif(btrim(p.voice_profile_ref),''),'')<>'';")"
[[ "$configured" == "2" ]] || { echo "ERROR: voice configuration incomplete" >&2; exit 44; }

attempts_after_voice="$(psql_scalar "
select count(*) from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
active_after_voice="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
[[ "$attempts_after_voice" == "0" && "$active_after_voice" == "0" ]] || { echo "ERROR: voice configuration created execution work" >&2; exit 45; }
echo "VOICE_CONFIGURATION_NON_BILLABLE=PASS"

mapfile -t stage_ids < <(psql_scalar "
select stage_run_id::text from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='pending'
order by created_at,stage_run_id;")
[[ "${#stage_ids[@]}" -eq 28 ]] || { echo "ERROR: pending Audio stages != 28" >&2; exit 50; }

export WORKFLOW_ID DIRECTOR_URL RUN_DIR DF_BEARER_TOKEN
printf '%s\n' "${stage_ids[@]}" | xargs -P 28 -I{} bash -c '
  stage="$1"
  code="$(curl -sS -o "$RUN_DIR/price-${stage}.json" -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-stages/$stage/pricing-preview")"
  printf "%s\\t%s\\n" "$stage" "$code" > "$RUN_DIR/price-${stage}.http"
' _ {}

cat "$RUN_DIR"/price-*.http | sort > "$RUN_DIR/pricing-http.tsv"
non_200="$(awk -F'\t' '$2 != 200 {n++} END {print n+0}' "$RUN_DIR/pricing-http.tsv")"
if [[ "$non_200" != "0" ]]; then
  echo "ERROR: $non_200 Audio pricing previews failed" >&2
  cat "$RUN_DIR/pricing-http.tsv" >&2
  for f in "$RUN_DIR"/price-*.http; do
    if [[ "$(cut -f2 "$f")" != "200" ]]; then
      stage="$(cut -f1 "$f")"
      echo "--- $stage ---" >&2
      cat "$RUN_DIR/price-${stage}.json" >&2
    fi
  done
  exit 51
fi

python3 - "$RUN_DIR" <<'PY'
from __future__ import annotations
import glob, json, os, sys
from decimal import Decimal, InvalidOperation
run_dir = sys.argv[1]
rows=[]; total=Decimal('0'); currencies=set()
for path in sorted(glob.glob(os.path.join(run_dir,'price-*.json'))):
    stage_id=os.path.basename(path)[len('price-'):-len('.json')]
    with open(path,encoding='utf-8') as fh: payload=json.load(fh)
    envelope=payload.get('pricing') or {}
    canonical=envelope.get('pricing') or {}
    amount_raw=canonical.get('estimated_amount')
    if amount_raw is None:
        raise SystemExit(f'missing pricing.pricing.estimated_amount for {stage_id}: {payload}')
    try: amount=Decimal(str(amount_raw))
    except InvalidOperation as exc: raise SystemExit(f'invalid amount {stage_id}: {amount_raw}') from exc
    quote_id=str(envelope.get('quote_id') or canonical.get('quote_id') or '')
    fingerprint=str(envelope.get('preview_fingerprint') or canonical.get('preview_fingerprint') or '')
    currency=str(canonical.get('currency') or '')
    if not quote_id: raise SystemExit(f'missing quote_id for {stage_id}')
    total += amount
    if currency: currencies.add(currency)
    rows.append((stage_id,quote_id,fingerprint,str(amount),currency))
if len(rows)!=28: raise SystemExit(f'expected 28 quotes, got {len(rows)}')
if len(currencies)>1: raise SystemExit(f'mixed currencies: {sorted(currencies)}')
summary=os.path.join(run_dir,'audio-pricing-review-summary.tsv')
with open(summary,'w',encoding='utf-8') as fh:
    fh.write('stage_run_id\tquote_id\tpreview_fingerprint\testimated_amount\tcurrency\n')
    for row in rows: fh.write('\t'.join(row)+'\n')
print(f'AUDIO_QUOTES={len(rows)}')
print(f'AUDIO_TOTAL_ESTIMATED_AMOUNT={total}')
print(f'AUDIO_CURRENCY={next(iter(currencies),"")}')
print(f'AUDIO_PRICING_SUMMARY={summary}')
PY

attempts_final="$(psql_scalar "
select count(*) from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
[[ "$attempts_final" == "0" && "$active_final" == "0" ]] || { echo "ERROR: preview created execution work" >&2; exit 60; }

echo "============================================================"
echo " V3 AUDIO BENCHMARK RESUME = PASS"
echo " fresh auth                         = PASS"
echo " catalog gate                       = PASS"
echo " authored dialogue locale           = en-PK (unchanged)"
echo " benchmark target speech locale     = $BENCHMARK_TARGET_LOCALE"
echo " Audio pricing previews             = 28/28"
echo " Audio stage attempts               = 0"
echo " active generation jobs             = 0"
echo " pricing reserve                    = NOT CALLED"
echo " Audio dispatch                     = NOT CALLED"
echo " provider generation                = NOT CALLED"
echo " credit commit                      = NOT CALLED"
echo "============================================================"
echo "STOP: review AUDIO_TOTAL_ESTIMATED_AMOUNT before paid dispatch."
