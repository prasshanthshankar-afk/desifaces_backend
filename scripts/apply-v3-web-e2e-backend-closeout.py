#!/usr/bin/env python3
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


# -----------------------------------------------------------------------------
# Face: persist the same resolved gender presentation used by T2I prompting.
# No new UI question is required. Explicit request wins; otherwise infer only
# unambiguous gender words from the user's translated prompt, then preserve the
# existing T2I default as the final fallback.
# -----------------------------------------------------------------------------
prompt_path = Path("services/svc-face/app/app/services/creator_prompt_service.py")
prompt = prompt_path.read_text()
if "FACE_VARIANT_GENDER_METADATA_V1" not in prompt:
    prompt = replace_once(
        prompt,
        "import random\nfrom typing import Any, Dict, List, Optional, Tuple\n",
        "import random\nimport re\nfrom typing import Any, Dict, List, Optional, Tuple\n",
        "Face prompt regex import",
    )

    helper_anchor = '''    @staticmethod\n    def _pick_one(rng: random.Random, arr: Any) -> Optional[Any]:\n'''
    helper = '''    @staticmethod\n    def _infer_gender_from_prompt(text: Any) -> str:\n        \"\"\"Infer only an explicit binary presentation term from user text.\n\n        This is deliberately conservative: ambiguous prompts return empty and\n        fall through to the existing product default. It does not infer gender\n        from geography, name, attire, occupation, or other stereotypes.\n        \"\"\"\n        value = str(text or \"\").strip().lower()\n        if not value:\n            return \"\"\n        female = bool(re.search(r\"\\b(female|woman|women|girl|lady|ladies)\\b\", value))\n        male = bool(re.search(r\"\\b(male|man|men|boy|gentleman|gentlemen)\\b\", value))\n        if female == male:\n            return \"\"\n        return \"female\" if female else \"male\"\n\n'''
    if helper_anchor not in prompt:
        raise SystemExit("Face prompt gender helper anchor missing")
    prompt = prompt.replace(helper_anchor, helper + helper_anchor, 1)

    old_gender = '''        # Gender policy\n        gender = explicit_gender\n        if (not is_i2i) and (not gender):\n            gender = \"female\"  # T2I default\n        # I2I: keep empty unless UI explicitly sends it\n'''
    new_gender = '''        # FACE_VARIANT_GENDER_METADATA_V1: use one resolved gender value for\n        # both generation prompting and persisted Face metadata.\n        gender = explicit_gender\n        if (not is_i2i) and (not gender):\n            gender = self._infer_gender_from_prompt(translated_prompt)\n        if (not is_i2i) and (not gender):\n            gender = \"female\"  # preserve existing T2I product default\n        if (not is_i2i) and gender in {\"male\", \"female\"}:\n            request_dict[\"gender\"] = gender\n        # I2I: keep empty unless the source/request already supplies it.\n'''
    prompt = replace_once(prompt, old_gender, new_gender, "Face resolved gender persistence")

prompt_path.write_text(prompt)


# Make the resolved gender available immediately in GeneratedVariant technical
# metadata as well as the saved Face profile/asset, so Face -> Voice handoff does
# not need to wait for a Dashboard refresh.
orch_path = Path("services/svc-face/app/app/services/creator_orchestrator.py")
orch = orch_path.read_text()
if "FACE_VARIANT_TECHNICAL_GENDER_V1" not in orch:
    old = '''            creative_variations = self._coerce_dict(variant.get("creative_variations"))\n            identity_signature = variant.get("identity_signature")\n\n            asset_id = await self.assets_repo.create_asset(\n'''
    new = '''            creative_variations = self._coerce_dict(variant.get("creative_variations"))\n            identity_signature = variant.get("identity_signature")\n            # FACE_VARIANT_TECHNICAL_GENDER_V1: this is the same resolved value\n            # that the prompt service used for T2I generation.\n            gender = self._coerce_gender(request_dict.get("gender"))\n            if gender:\n                technical["gender"] = gender\n                technical["gender_presentation"] = gender\n\n            asset_id = await self.assets_repo.create_asset(\n'''
    orch = replace_once(orch, old, new, "Face technical gender metadata")
    orch = replace_once(
        orch,
        '''            gender = self._coerce_gender(request_dict.get("gender"))\n\n            profile_id = await self.profiles_repo.create_profile(\n''',
        '''            profile_id = await self.profiles_repo.create_profile(\n''',
        "Face duplicate gender assignment",
    )
orch_path.write_text(orch)


# -----------------------------------------------------------------------------
# Fusion Extension: normalize optional Video direction into existing tags and
# provider_options, while deliberately leaving current provider selection and
# pricing/reservation behavior untouched.
# -----------------------------------------------------------------------------
route_path = Path("services/svc-fusion-extension/app/app/api/routes/longform.py")
route = route_path.read_text()
if "VIDEO_DIRECTION_CONTRACT_V1" not in route:
    import_anchor = "from app.services.sas_service import AzureBlobService\n"
    route = replace_once(
        route,
        import_anchor,
        import_anchor + "from app.services.video_direction_contract import apply_video_direction\n",
        "Video direction import",
    )

    # Ensure provider_options exists for both talking and cinematic paths.
    anchor = '''    provider_hint = (_safe_str(body.get('provider_hint')) or _safe_str(tags.get('provider_hint')) or '').lower()\n\n    if resolved_profile == 'talking_video':\n'''
    replacement = '''    provider_hint = (_safe_str(body.get('provider_hint')) or _safe_str(tags.get('provider_hint')) or '').lower()\n    provider_options = _as_dict_loose(body.get('provider_options'))\n\n    if resolved_profile == 'talking_video':\n'''
    route = replace_once(route, anchor, replacement, "Video direction provider options scope")

    # Remove the narrower redefinition inside the talking-video branch.
    route = replace_once(
        route,
        '''        provider_options = _as_dict_loose(body.get('provider_options'))\n\n        if is_talking_economy:\n''',
        '''        if is_talking_economy:\n''',
        "Video direction duplicate provider options",
    )

    # Apply after existing provider selection. The helper cannot alter provider
    # hint/routing and therefore cannot affect pricing/provider authority.
    before_defaults = '''    if not _safe_str(body.get('background_mode')):\n        body['background_mode'] = 'movement_based' if resolved_profile == 'cinematic_video_direction' else 'fixed'\n'''
    direction_block = '''    # VIDEO_DIRECTION_CONTRACT_V1: provider-neutral customer direction.\n    # Current provider routing above remains authoritative and unchanged.\n    body, tags, provider_options = apply_video_direction(body, tags, provider_options)\n    if _safe_str(body.get('background_mode')):\n        provider_options['background_mode'] = body.get('background_mode')\n    body['provider_options'] = provider_options\n\n    if not _safe_str(body.get('background_mode')):\n        body['background_mode'] = 'movement_based' if resolved_profile == 'cinematic_video_direction' else 'fixed'\n'''
    route = replace_once(route, before_defaults, direction_block, "Video direction application")

route_path.write_text(route)
print("V3_WEB_E2E_BACKEND_CLOSEOUT_PATCH=PASS")
