#!/usr/bin/env bash
set -euo pipefail

# V3 Audio catalog hardening + benchmark preparation.
#
# This script intentionally STOPS after pricing preview.
# It does NOT reserve credits, dispatch Audio jobs, call a TTS provider for
# generation, or commit/release pricing.
#
# Benchmark fixture: clean 28-turn Pakistan Story selected for V3 performance
# certification. The benchmark-only speech target is ur-PK because that is the
# proven executable Pakistan locale in the live capability graph. The authored
# dialogue locale remains en-PK and is never rewritten by this script.

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
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.http "$RUN_DIR"/*.tsv 2>/dev/null || true

compose() {
  bash scripts/v3-compose.sh "$@"
}

psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

echo "============================================================"
echo " V3 AUDIO CATALOG HARDENING + BENCHMARK PREPARE"
echo " workflow: $WORKFLOW_ID"
echo " benchmark target locale: $BENCHMARK_TARGET_LOCALE"
echo " PAID GENERATION: DISABLED"
echo "============================================================"

if [[ ! -f scripts/v3-compose.sh ]]; then
  echo "ERROR: run from ~/workspace/desifaces-v3" >&2
  exit 2
fi

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "feature/v3-multiperson-core-20260818" ]]; then
  echo "ERROR: expected feature/v3-multiperson-core-20260818, got $BRANCH" >&2
  exit 3
fi

HEAD_SHA="$(git rev-parse HEAD)"
echo "HEAD=$HEAD_SHA"

required_commits=(
  42dbf6f32d3cb982de53a2a50ca48170292a3c31
  9f527a109bf601f066c81fdc229981b7ab4bcaa2
  6d9b5b3946bd04216eb2b4a9bd65910a77d0e6fa
  5746bf7e62657e2e970ccf5b733543e613704d1c
)
for sha in "${required_commits[@]}"; do
  git merge-base --is-ancestor "$sha" HEAD || {
    echo "ERROR: required source commit missing: $sha" >&2
    exit 4
  }
done
echo "SOURCE_GATE=PASS"

python3 -m py_compile \
  services/svc-audio/app/app/api/routes/catalog.py \
  services/svc-director/app/app/audio_autoconfigure_routes.py \
  services/svc-audio/tests/test_global_audio_catalog.py \
  services/svc-director/tests/test_audio_autoconfigure_gender.py

echo "PY_COMPILE=PASS"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
echo "ACTIVE_GENERATION_JOBS=$active_jobs"
[[ "$active_jobs" == "0" ]] || {
  echo "HOLD: active generation jobs exist; refusing API restart." >&2
  exit 10
}

fixture_row="$(psql_scalar "
select
  w.current_stage || '|' ||
  count(*) filter (where s.stage_type='audio') || '|' ||
  count(*) filter (where s.stage_type='audio' and s.state='pending') || '|' ||
  count(*) filter (where s.stage_type='fusion' and s.state='pending')
from public.v3_studio_workflows w
join public.v3_studio_stage_runs s on s.workflow_id=w.workflow_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
group by w.current_stage;")"

authored_locales="$(psql_scalar "
select coalesce(string_agg(distinct dt.locale, ',' order by dt.locale),'')
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"

echo "FIXTURE=$fixture_row"
echo "AUTHORED_AUDIO_LOCALES=$authored_locales"
IFS='|' read -r current_stage audio_total audio_pending fusion_pending <<<"$fixture_row"
[[ "$current_stage" == "audio" && "$audio_total" == "28" && "$audio_pending" == "28" && "$fusion_pending" == "1" ]] || {
  echo "ERROR: benchmark fixture is no longer clean/current Audio." >&2
  exit 11
}
[[ "$authored_locales" == "en-PK" ]] || {
  echo "ERROR: expected authored Audio locale en-PK, got '$authored_locales'" >&2
  exit 12
}

attempts_before="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
[[ "$attempts_before" == "0" ]] || {
  echo "ERROR: benchmark Audio fixture already has attempts=$attempts_before" >&2
  exit 13
}

echo "FIXTURE_GATE=PASS"

echo
echo "===== BUILD AFFECTED APIS ====="
compose build svc-audio svc-director

echo
echo "===== RECREATE AFFECTED APIS ====="
compose up -d --no-deps --force-recreate svc-audio svc-director

wait_health() {
  local url="$1"
  local name="$2"
  local i
  for i in $(seq 1 60); do
    if curl -fsS "$url/api/health" >/dev/null 2>&1; then
      echo "$name=HEALTHY"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: $name health timeout" >&2
  return 1
}

wait_health "$AUDIO_URL" "svc-audio"
wait_health "$DIRECTOR_URL" "svc-director"

if [[ -z "${DF_BEARER_TOKEN:-}" ]]; then
  export DF_EMAIL CORE_URL
  read -rsp "Enter test-account password: " DF_PASSWORD
  echo
  export DF_PASSWORD
  LOGIN_EXPORTS="$(python3 scripts/df_login_exports.py)"
  LOGIN_RC=$?
  unset DF_PASSWORD
  [[ "$LOGIN_RC" -eq 0 ]] || {
    echo "ERROR: authentication failed" >&2
    exit 20
  }
  eval "$LOGIN_EXPORTS"
  unset LOGIN_EXPORTS
fi
[[ -n "${DF_BEARER_TOKEN:-}" ]] || {
  echo "ERROR: DF_BEARER_TOKEN missing" >&2
  exit 21
}
export DF_BEARER_TOKEN

echo "AUTH=PASS"

echo
echo "===== CATALOG CERTIFICATION ====="
curl -fsS -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$AUDIO_URL/api/audio/catalog/target-languages?country_code=PK" \
  > "$RUN_DIR/pk-targets.json"

curl -fsS -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$AUDIO_URL/api/audio/catalog/voices?locale=${BENCHMARK_TARGET_LOCALE}" \
  > "$RUN_DIR/target-voices.json"

curl -fsS -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$AUDIO_URL/api/audio/catalog/voices?locale=en-PK" \
  > "$RUN_DIR/en-pk-voices.json"

jq -e --arg locale "$BENCHMARK_TARGET_LOCALE" '.items | any(.locale == $locale)' \
  "$RUN_DIR/pk-targets.json" >/dev/null
jq -e '.items | length == 0' "$RUN_DIR/en-pk-voices.json" >/dev/null
jq -e '.items | any(((.gender // "")|ascii_downcase) == "male")' \
  "$RUN_DIR/target-voices.json" >/dev/null
jq -e '.items | any(((.gender // "")|ascii_downcase) == "female")' \
  "$RUN_DIR/target-voices.json" >/dev/null

echo "PK_TARGET_${BENCHMARK_TARGET_LOCALE}=PASS"
echo "EN_PK_UNSUPPORTED_FAIL_CLOSED=PASS"
echo "TARGET_LOCALE_MALE_FEMALE_VOICES=PASS"

mapfile -t exposed_voices < <(jq -r '.items[].voice_name' "$RUN_DIR/target-voices.json")
[[ "${#exposed_voices[@]}" -gt 0 ]] || {
  echo "ERROR: no voices returned for $BENCHMARK_TARGET_LOCALE" >&2
  exit 29
}

for voice in "${exposed_voices[@]}"; do
  [[ "$voice" =~ ^[A-Za-z0-9._:-]+$ ]] || {
    echo "ERROR: unexpected voice identifier syntax: $voice" >&2
    exit 30
  }
  graph_count="$(psql_scalar "
select count(*)
from public.tts_voices v
join public.tts_voice_locale_capabilities vl
  on vl.voice_id=v.id
 and vl.locale='${BENCHMARK_TARGET_LOCALE}'
 and vl.is_enabled=true
 and vl.is_approved=true
join public.tts_voice_model_capabilities vm
  on vm.voice_id=v.id
 and vm.provider_code=v.provider
 and vm.is_enabled=true
 and vm.is_approved=true
join public.tts_provider_models m
  on m.provider_code=vm.provider_code
 and m.model_code=vm.model_code
 and m.is_enabled=true
 and m.routing_enabled=true
join public.tts_providers p
  on p.provider_code=vm.provider_code
 and p.is_enabled=true
 and p.routing_enabled=true
join public.tts_locales l on l.locale='${BENCHMARK_TARGET_LOCALE}'
where v.voice_name='${voice}'
  and (
    exists (
      select 1 from public.tts_model_locale_capabilities mlc
      where mlc.provider_code=vm.provider_code
        and mlc.model_code=vm.model_code
        and mlc.locale=l.locale
        and mlc.is_enabled=true
        and mlc.is_approved=true
    )
    or exists (
      select 1 from public.tts_model_language_capabilities mlng
      where mlng.provider_code=vm.provider_code
        and mlng.model_code=vm.model_code
        and mlng.language_code=l.language_code
        and mlng.is_enabled=true
        and mlng.is_approved=true
    )
  );")"
  [[ "$graph_count" -gt 0 ]] || {
    echo "ERROR: catalog exposed non-executable voice: $voice" >&2
    exit 31
  }
done

echo "EXECUTABLE_CAPABILITY_GRAPH=PASS"

participant_state="$(psql_scalar "
select
  count(distinct p.participant_id) filter (
    where coalesce(nullif(btrim(p.voice_profile_ref),''),'')=''
  ) || '|' ||
  count(distinct p.participant_id) filter (
    where coalesce(nullif(btrim(p.voice_locale),''),'')=''
  ) || '|' ||
  count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
IFS='|' read -r missing_voice missing_locale participant_count <<<"$participant_state"
[[ "$participant_count" == "2" ]] || {
  echo "ERROR: expected two speaking participants, got $participant_count" >&2
  exit 32
}

if [[ "$missing_voice" == "2" && "$missing_locale" == "2" ]]; then
  autoconfig_http="$(curl -sS -o "$RUN_DIR/autoconfig-before-explicit.json" -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/audio-autoconfigure")"
  [[ "$autoconfig_http" == "200" ]] || {
    echo "ERROR: audio-autoconfigure returned HTTP $autoconfig_http" >&2
    cat "$RUN_DIR/autoconfig-before-explicit.json" >&2
    exit 33
  }
  jq -e '.ready == false' "$RUN_DIR/autoconfig-before-explicit.json" >/dev/null
  jq -e '(.characters | length) == 2 and all(.characters[]; .status == "needs_user_choice")' \
    "$RUN_DIR/autoconfig-before-explicit.json" >/dev/null

  after_auto="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
  and s.stage_type='audio'
  and (coalesce(nullif(btrim(p.voice_profile_ref),''),'') <> ''
       or coalesce(nullif(btrim(p.voice_locale),''),'') <> '');")"
  [[ "$after_auto" == "0" ]] || {
    echo "ERROR: fail-closed autoconfigure mutated participant voice profile" >&2
    exit 34
  }
  echo "DIRECTOR_UNSUPPORTED_LOCALE_FAIL_CLOSED=PASS"
elif [[ "$missing_voice" == "0" && "$missing_locale" == "0" ]]; then
  existing_non_target="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
  and s.stage_type='audio'
  and p.voice_locale <> '${BENCHMARK_TARGET_LOCALE}';")"
  [[ "$existing_non_target" == "0" ]] || {
    echo "ERROR: fixture already has explicit non-${BENCHMARK_TARGET_LOCALE} voice selection; refusing overwrite" >&2
    exit 35
  }
  echo "DIRECTOR_UNSUPPORTED_LOCALE_FAIL_CLOSED=SKIP_ALREADY_PREPARED"
else
  echo "ERROR: partial participant voice configuration; refusing automatic repair" >&2
  exit 36
fi

echo
echo "===== BENCHMARK-ONLY EXPLICIT VOICE CONFIGURATION ====="
psql_scalar "
select distinct on (p.participant_id)
  p.participant_id::text || E'\\t' ||
  replace(p.display_name, E'\\t', ' ') || E'\\t' ||
  lower(coalesce(
    nullif(p.metadata_json #>> '{explicit_face_constraints,gender}',''),
    nullif(p.metadata_json #>> '{explicit_face_constraints,gender_presentation}',''),
    nullif(p.persona_json->>'gender',''),
    nullif(p.persona_json->>'gender_presentation',''),
    ''
  ))
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio'
order by p.participant_id, p.display_name;" > "$RUN_DIR/participants.tsv"

[[ "$(wc -l < "$RUN_DIR/participants.tsv" | tr -d ' ')" == "2" ]] || {
  echo "ERROR: participant discovery did not produce exactly two rows" >&2
  cat "$RUN_DIR/participants.tsv" >&2
  exit 40
}

while IFS=$'\t' read -r participant_id display_name gender_raw; do
  case "$gender_raw" in
    male|man|m) gender="male" ;;
    female|woman|f) gender="female" ;;
    *) echo "ERROR: explicit participant gender missing/unsupported for $display_name: $gender_raw" >&2; exit 41 ;;
  esac

  voice_id="$(jq -r --arg g "$gender" '
    [ .items[] | select((((.gender // "")|ascii_downcase) == $g)) ]
    | (map(select(.is_default == true)) + .)
    | .[0].voice_name // empty
  ' "$RUN_DIR/target-voices.json")"
  [[ -n "$voice_id" ]] || {
    echo "ERROR: no executable $gender voice for $BENCHMARK_TARGET_LOCALE" >&2
    exit 42
  }

  payload="$(jq -nc --arg voice "$voice_id" --arg locale "$BENCHMARK_TARGET_LOCALE" \
    '{voice_id:$voice,voice_locale:$locale}')"

  http="$(curl -sS -o "$RUN_DIR/voice-${participant_id}.json" -w '%{http_code}' \
    -X PUT \
    -H "Authorization: Bearer $DF_BEARER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/participants/$participant_id/voice-profile")"

  [[ "$http" == "200" ]] || {
    echo "ERROR: voice-profile HTTP $http for $display_name" >&2
    cat "$RUN_DIR/voice-${participant_id}.json" >&2
    exit 43
  }
  echo "$display_name -> $voice_id ($BENCHMARK_TARGET_LOCALE)"
done < "$RUN_DIR/participants.tsv"

configured_count="$(psql_scalar "
select count(distinct p.participant_id)
from public.v3_studio_stage_runs s
join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
join public.v3_participants p on p.participant_id=dt.speaker_participant_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
  and s.stage_type='audio'
  and p.voice_locale='${BENCHMARK_TARGET_LOCALE}'
  and coalesce(nullif(btrim(p.voice_profile_ref),''),'') <> '';")"
[[ "$configured_count" == "2" ]] || {
  echo "ERROR: benchmark voice configuration incomplete" >&2
  exit 44
}

attempts_after_voice="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
active_after_voice="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
[[ "$attempts_after_voice" == "0" && "$active_after_voice" == "0" ]] || {
  echo "ERROR: voice configuration unexpectedly created execution work" >&2
  exit 45
}
echo "VOICE_CONFIGURATION_NON_BILLABLE=PASS"

mapfile -t audio_stage_ids < <(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid
  and stage_type='audio'
  and state='pending'
order by created_at,stage_run_id;")
[[ "${#audio_stage_ids[@]}" -eq 28 ]] || {
  echo "ERROR: expected 28 pending Audio stages, got ${#audio_stage_ids[@]}" >&2
  exit 50
}

export WORKFLOW_ID DIRECTOR_URL RUN_DIR DF_BEARER_TOKEN
printf '%s\n' "${audio_stage_ids[@]}" | xargs -P 28 -I{} bash -c '
  stage="$1"
  code="$(curl -sS -o "$RUN_DIR/price-${stage}.json" -w "%{http_code}" \
    -X POST \
    -H "Authorization: Bearer $DF_BEARER_TOKEN" \
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

import glob
import json
import os
import sys
from decimal import Decimal, InvalidOperation

run_dir = sys.argv[1]
rows = []
total = Decimal("0")
currencies = set()

for path in sorted(glob.glob(os.path.join(run_dir, "price-*.json"))):
    stage_id = os.path.basename(path)[len("price-"):-len(".json")]
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    # Director response contract:
    #   payload.pricing = AudioPricingPreviewResponse
    #   payload.pricing.pricing = canonical pricing payload
    envelope = payload.get("pricing") or {}
    canonical = envelope.get("pricing") or {}

    amount_raw = canonical.get("estimated_amount")
    if amount_raw is None:
        raise SystemExit(
            f"missing pricing.pricing.estimated_amount for {stage_id}: {payload}"
        )
    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation as exc:
        raise SystemExit(f"invalid estimated_amount for {stage_id}: {amount_raw}") from exc

    quote_id = str(envelope.get("quote_id") or canonical.get("quote_id") or "")
    fingerprint = str(
        envelope.get("preview_fingerprint")
        or canonical.get("preview_fingerprint")
        or ""
    )
    currency = str(canonical.get("currency") or "")

    if not quote_id:
        raise SystemExit(f"missing quote_id for {stage_id}: {payload}")

    total += amount
    if currency:
        currencies.add(currency)
    rows.append((stage_id, quote_id, fingerprint, str(amount), currency))

if len(rows) != 28:
    raise SystemExit(f"expected 28 quote payloads, got {len(rows)}")
if len(currencies) > 1:
    raise SystemExit(f"mixed pricing currencies: {sorted(currencies)}")

summary_path = os.path.join(run_dir, "audio-pricing-review-summary.tsv")
with open(summary_path, "w", encoding="utf-8") as fh:
    fh.write("stage_run_id\tquote_id\tpreview_fingerprint\testimated_amount\tcurrency\n")
    for row in rows:
        fh.write("\t".join(row) + "\n")

print(f"AUDIO_QUOTES={len(rows)}")
print(f"AUDIO_TOTAL_ESTIMATED_AMOUNT={total}")
print(f"AUDIO_CURRENCY={next(iter(currencies), '')}")
print(f"AUDIO_PRICING_SUMMARY={summary_path}")
PY

attempts_final="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio';")"
active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing');")"
[[ "$attempts_final" == "0" && "$active_final" == "0" ]] || {
  echo "ERROR: pricing preview unexpectedly created execution work" >&2
  exit 60
}

echo
echo "============================================================"
echo " V3 AUDIO BENCHMARK PREPARE = PASS"
echo " catalog hardening deployed         = YES"
echo " en-PK silent substitution          = BLOCKED"
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
echo "STOP: review AUDIO_TOTAL_ESTIMATED_AMOUNT above before paid dispatch."
