#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BRANCH="feature/v3-web-e2e-ux-closeout-20260903"
TMP="/tmp/deploy-v3-web-e2e-ux-closeout.final.sh"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/deploy-v3-web-e2e-ux-closeout.sh" > "$TMP"

# Harden the final launcher before it runs. Every replacement is validated here,
# before any source worktree, rollback snapshot, or runtime mutation occurs.
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()

def once(old,new,label):
    global s
    n=s.count(old)
    if n != 1:
        raise SystemExit(f'{label}: expected one anchor, found {n}')
    s=s.replace(old,new,1)

# Legacy Saved Work: explicit prompt gender fallback for pre-metadata Face assets.
once(
 'python3 scripts/apply-v3-face-voice-gender-handoff.py\npython3 scripts/test-v3-web-e2e-ux-closeout.py\n',
 'python3 scripts/apply-v3-face-voice-gender-handoff.py\npython3 scripts/apply-v3-voice-legacy-face-gender.py\npython3 scripts/test-v3-web-e2e-ux-closeout.py\n',
 'legacy Face compatibility insertion',
)

# tts_model_resolver is a second narrow Audio file: model selection itself must
# have at least one voice for the selected Face gender.
once(
 '  services/svc-audio/app/app/services/tts_resolution_planner.py \\\n  services/svc-face/app/app/services/creator_prompt_service.py \\\n',
 '  services/svc-audio/app/app/services/tts_resolution_planner.py \\\n  services/svc-audio/app/app/services/tts_model_resolver.py \\\n  services/svc-face/app/app/services/creator_prompt_service.py \\\n',
 'Audio model resolver compile gate',
)

once(
 'for c in "$AUDIO_API" "$AUDIO_WORKER"; do snapshot_file "$c" app/services/tts_resolution_planner.py; done\n',
 'for c in "$AUDIO_API" "$AUDIO_WORKER"; do snapshot_file "$c" app/services/tts_resolution_planner.py; snapshot_file "$c" app/services/tts_model_resolver.py; done\n',
 'Audio rollback snapshot',
)
once(
 '      for c in "$AUDIO_API" "$AUDIO_WORKER"; do restore_file "$c" app/services/tts_resolution_planner.py; done\n',
 '      for c in "$AUDIO_API" "$AUDIO_WORKER"; do restore_file "$c" app/services/tts_resolution_planner.py; restore_file "$c" app/services/tts_model_resolver.py; done\n',
 'Audio rollback restore',
)
once(
 '  docker cp "$BWT/services/svc-audio/app/app/services/tts_resolution_planner.py" "$c:/app/app/services/tts_resolution_planner.py"\ndone\n',
 '  docker cp "$BWT/services/svc-audio/app/app/services/tts_resolution_planner.py" "$c:/app/app/services/tts_resolution_planner.py"\n  docker cp "$BWT/services/svc-audio/app/app/services/tts_model_resolver.py" "$c:/app/app/services/tts_model_resolver.py"\ndone\n',
 'Audio runtime deploy',
)

# Add an executable model-eligibility proof before the existing planner fallback proof.
marker='# Execute the stale-voice fallback with fake resolvers; no DB/provider/network use.\n'
proof='''# Prove model selection filters by authoritative Face gender using a fake SQL catalog.\ndocker exec -i "$AUDIO_API" python - <<'PYMODEL'\nimport asyncio\nfrom types import SimpleNamespace\nfrom app.services.tts_model_resolver import TTSModelResolver, TTSModelResolutionRequest\nclass Catalog:\n async def get_default_routing_policy(self): return SimpleNamespace(policy_code='p',require_approved_capability=False,require_approved_quality=False)\n async def list_routing_enabled_model_candidates(self, **kw):\n  base=dict(adapter_key='a',provider_model_id=None,canonical_locale='pa-IN',language_code='pa',provider_locale_code='pa-IN',provider_language_code='pa',capability_scope='locale',quality_class='premium',quality_score=1.0,max_input_chars=5000)\n  return [SimpleNamespace(provider_code='p1',model_code='female-only',**base),SimpleNamespace(provider_code='p2',model_code='male-capable',**base)]\n async def list_voice_candidates(self, **kw):\n  return [SimpleNamespace()] if kw.get('model_code')=='male-capable' and kw.get('requested_gender')=='male' else []\n async def get_masterdata_revision(self): return 1\nasync def main():\n r=await TTSModelResolver(Catalog()).resolve(TTSModelResolutionRequest(canonical_locale='pa-IN',text_length=10,requested_gender='male'))\n assert r.model_code=='male-capable', r.model_code\n print('VOICE_MODEL_GENDER_ELIGIBILITY=PASS model=male-capable')\nasyncio.run(main())\nPYMODEL\n\n'''
if marker not in s:
    raise SystemExit('Audio runtime proof insertion anchor missing')
s=s.replace(marker,proof+marker,1)

p.write_text(s)
PY

# Backend-owned closeout resources are verified in the backend repository.
git cat-file -e "origin/$BRANCH:services/svc-audio/app/app/services/tts_model_resolver.py" 2>/dev/null || {
  echo 'FAIL: backend closeout resource missing: tts_model_resolver.py' >&2
  exit 2
}

# Web-owned closeout resources must be checked in the web repository, not here.
[[ -d "$WEB_ROOT/.git" ]] || {
  echo "FAIL: web repository not found: $WEB_ROOT" >&2
  exit 2
}
git -C "$WEB_ROOT" fetch -q origin "$BRANCH"
git -C "$WEB_ROOT" cat-file -e "origin/$BRANCH:scripts/apply-v3-voice-legacy-face-gender.py" 2>/dev/null || {
  echo 'FAIL: web closeout resource missing: apply-v3-voice-legacy-face-gender.py' >&2
  exit 2
}

exec bash "$TMP"
