from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Dict, List, Optional, Tuple

from app.services.safety_service import SafetyService
from app.services.translation_service import TranslationService
from app.repos.creator_config_repo import CreatorPlatformConfigRepo


class CreatorPromptService:
    """
    DB-driven prompt engine (svc-face):

    - Deterministic: job_seed stable across retries; per-variant seed = job_seed + variant_number * PRIME_VARIANT_STRIDE

    IMPORTANT T2I vs I2I policy (matches your product intent):
      - T2I: user may omit demographic codes; gender required (default "female");
            demographic codes (age/region/skin tone) are OPTIONAL and only included if provided.
            Creative variations SHOULD apply (lighting/pose/camera/etc).
      - I2I: DO NOT inject demographic defaults; do NOT append random “Andhra/Kerala” style bases.
            The USER_PROMPT (edit instruction) must dominate.
            By default, we minimize variation modifiers that fight the edit.
            (Optionally enable extra variations via request_dict["enable_i2i_variations"]=True.)
    """

    PRIME_VARIANT_STRIDE = 9973  # large prime spacing for seed decorrelation

    def __init__(
        self,
        db_pool,
        safety: SafetyService,
        translator: TranslationService,
        config_repo: Optional[CreatorPlatformConfigRepo] = None,
    ):
        self.pool = db_pool
        self.safety = safety
        self.translator = translator
        self.config_repo = config_repo or CreatorPlatformConfigRepo(db_pool)

    # ---------------------------------------------------------------------
    # Small helpers
    # ---------------------------------------------------------------------
    @staticmethod
    def _stable_json(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))

    def _stable_seed_from(self, obj: Dict[str, Any]) -> int:
        """Deterministic job seed: stable across retries and workers."""
        s = self._stable_json(obj)
        h = hashlib.sha256(s.encode("utf-8")).hexdigest()
        return int(h[:8], 16) & 0x7FFFFFFF

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        """Works for dicts, Pydantic models, and simple objects."""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _as_text(x: Any) -> str:
        if x is None:
            return ""
        if isinstance(x, str):
            return x.strip()
        return str(x).strip()

    @classmethod
    def _join(cls, parts: List[Any]) -> str:
        cleaned = [cls._as_text(p) for p in parts]
        cleaned = [p for p in cleaned if p]
        return ", ".join(cleaned)

    @staticmethod
    def _coerce_gender(g: Any) -> str:
        """
        Important: return "" when not provided.
        Do NOT default to 'person' here; callers decide policy per mode.
        """
        if g is None:
            return ""
        if hasattr(g, "value"):
            return str(g.value or "").strip()
        if isinstance(g, dict) and "value" in g:
            return str(g.get("value") or "").strip()
        return str(g).strip()

    @staticmethod
    def _pick_one(rng: random.Random, arr: Any) -> Optional[Any]:
        if not arr:
            return None
        if isinstance(arr, (list, tuple)):
            if not arr:
                return None
            return rng.choice(list(arr))
        return None

    @staticmethod
    def _code(obj: Any) -> Optional[str]:
        if not obj:
            return None
        if isinstance(obj, dict):
            return obj.get("code") or obj.get("platform_code")
        return getattr(obj, "code", None) or getattr(obj, "platform_code", None)

    # ---------------------------------------------------------------------
    # Public: translate + validate
    # ---------------------------------------------------------------------
    async def translate_and_validate(self, user_prompt: str, language: str) -> Dict[str, Any]:
        ok, reason = await self.safety.validate_text(user_prompt)
        if not ok:
            raise ValueError(f"unsafe_prompt: {reason}")

        translated = user_prompt
        success = True
        provider = "none"

        if user_prompt and language and language != "en":
            provider = "googletranslator"
            translated, success = await self.translator.translate_to_english(user_prompt, language)

        ok2, reason2 = await self.safety.validate_text(translated)
        if not ok2:
            raise ValueError(f"unsafe_prompt_after_translation: {reason2}")

        return {
            "user_prompt_original": user_prompt,
            "user_prompt_language": language or "en",
            "user_prompt_translated_en": translated,
            "translation_provider": provider,
            "translation_success": bool(success),
        }

    async def build_variants(
        self,
        request_dict: Dict[str, Any],
        job_seed: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Returns: (variants, resolved_config)

        T2I vs I2I policy:
          - T2I: gender required (default "female"); age/region/skin_tone optional (only included if provided)
          - I2I: do NOT inject demographic defaults; user_prompt must dominate the edit
        """

        # Normalize clothing param name (some callers may send clothing_code)
        if request_dict.get("clothing_code") and not request_dict.get("clothing_style_code"):
            request_dict["clothing_style_code"] = request_dict["clothing_code"]

        # Normalize mode
        mode_raw = (request_dict.get("mode") or "text-to-image").strip().lower()
        if mode_raw in ("t2i", "text-to-image", "txt2img"):
            mode_norm = "text-to-image"
        elif mode_raw in ("i2i", "image-to-image", "img2img"):
            mode_norm = "image-to-image"
        else:
            mode_norm = "text-to-image"

        is_i2i = (mode_norm == "image-to-image")

        # -------------------------
        # Resolve configs from DB
        # -------------------------
        image_format_code = request_dict.get("image_format_code")
        use_case_code = request_dict.get("use_case_code")

        age_range_code = request_dict.get("age_range_code")
        region_code = request_dict.get("region_code")
        skin_tone_code = request_dict.get("skin_tone_code")

        # Always resolve image_format + use_case (needed for output specs + prompt base)
        image_format = await self.config_repo.get_image_format_by_code(image_format_code) if image_format_code else None
        use_case = await self.config_repo.get_use_case_by_code(use_case_code) if use_case_code else None

        # Default ONLY these two if missing
        if not image_format:
            fmts = await self.config_repo.get_image_formats()
            image_format = fmts[0] if fmts else None
            if image_format and not request_dict.get("image_format_code"):
                request_dict["image_format_code"] = self._get(image_format, "code") or ""

        if not use_case:
            ucs = await self.config_repo.get_use_cases()
            use_case = ucs[0] if ucs else None
            if use_case and not request_dict.get("use_case_code"):
                request_dict["use_case_code"] = self._get(use_case, "code") or ""

        if not image_format or not use_case:
            raise ValueError(
                f"missing_config: {[k for k, v in {'image_format': image_format, 'use_case': use_case}.items() if not v]}"
            )

        # Demographics are OPTIONAL for both modes (no defaults here)
        age_range = await self.config_repo.get_age_range_by_code(age_range_code) if age_range_code else None
        region = await self.config_repo.get_region_by_code(region_code) if region_code else None
        skin_tone = await self.config_repo.get_skin_tone_by_code(skin_tone_code) if skin_tone_code else None

        # Optional configs
        style = None
        if request_dict.get("style_code"):
            try:
                style = await self.config_repo.get_style_by_code(request_dict.get("style_code"))
            except Exception:
                style = None

        context = await self.config_repo.get_context_by_code(request_dict.get("context_code")) if request_dict.get("context_code") else None
        clothing = await self.config_repo.get_clothing_by_code(request_dict.get("clothing_style_code")) if request_dict.get("clothing_style_code") else None
        platform = await self.config_repo.get_platform_requirements_by_code(request_dict.get("platform_code")) if request_dict.get("platform_code") else None

        # Seed
        if job_seed is None:
            job_seed = self._stable_seed_from(request_dict)
        job_seed = int(job_seed) & 0x7FFFFFFF

        # Prompt (translated prompt becomes the user instruction)
        translated_prompt = (
            request_dict.get("user_prompt_translated_en")
            or request_dict.get("translated_prompt")
            or request_dict.get("user_prompt")
            or ""
        )

        # Gender policy
        gender = self._coerce_gender(request_dict.get("gender"))  # "" if not provided
        if (not is_i2i) and (not gender):
            gender = "female"  # T2I default
        # I2I: keep empty unless UI explicitly sends it

        def gender_phrase(g: str) -> Optional[str]:
            g = (g or "").strip().lower()
            if not g:
                return None
            if g in ("person", "human"):
                return "person"
            return f"{g} person"

        # -------------------------
        # Build demographic base
        # -------------------------
        demographic_parts: List[str] = []

        if not is_i2i:
            # T2I: include demographic descriptors ONLY if provided by UI
            if age_range:
                demographic_parts.append(self._as_text(self._get(age_range, "prompt_descriptor")))

            gp = gender_phrase(gender)
            if gp:
                demographic_parts.append(gp)

            if region:
                region_display = self._get(region, "display_name")
                if isinstance(region_display, dict):
                    region_name = region_display.get("en") or region_display.get("name") or self._get(region, "code")
                else:
                    region_name = region_display or self._get(region, "code")
                if region_name:
                    demographic_parts.append(f"from {region_name}")
                rb = self._as_text(self._get(region, "prompt_base"))
                if rb:
                    demographic_parts.append(rb)

            if skin_tone:
                demographic_parts.append(self._as_text(self._get(skin_tone, "prompt_descriptor")))

            # keep this for T2I quality and cultural consistency
            demographic_parts.append("authentic Indian features, natural appearance, culturally accurate")

            # add user prompt
            if translated_prompt:
                demographic_parts.append(translated_prompt)

        else:
            # I2I: do NOT inject demographic defaults.
            # Keep this minimal; identity is handled by the image itself + negative prompts.
            # Ensure the edit instruction dominates by placing it LAST later in the final prompt.
            pass

        demographic_base = self._join(demographic_parts)

        # -------------------------
        # Variations from DB
        # -------------------------
        variations_by_type = await self.config_repo.get_variations_by_use_case(
            use_case_code=self._get(use_case, "code") or "",
            professional_level_min=3,
            creativity_level_min=2,
            active_only=True,
        ) or {}

        used_codes_by_type: Dict[str, set] = {k: set() for k in variations_by_type.keys()}

        def pick_variation(var_type: str, rng_local: random.Random) -> Optional[Dict[str, Any]]:
            pool = variations_by_type.get(var_type) or []
            if not pool:
                return None
            candidates = pool[:]
            rng_local.shuffle(candidates)

            used = used_codes_by_type.setdefault(var_type, set())
            for v in candidates:
                code = v.get("code")
                if code and code not in used:
                    used.add(code)
                    return v
            return candidates[rng_local.randrange(0, len(candidates))]

        variation_types_preference = ["lighting", "expression", "pose", "camera", "background", "styling"]

        # Technical specs
        width = int(self._get(image_format, "width") or 512)
        height = int(self._get(image_format, "height") or 512)
        aspect_ratio = self._as_text(self._get(image_format, "aspect_ratio"))

        num_variants = int(request_dict.get("num_variants") or 4)
        variants: List[Dict[str, Any]] = []

        # I2I: default OFF (to avoid fighting edit instruction)
        enable_i2i_variations = bool(request_dict.get("enable_i2i_variations", False))

        for i in range(num_variants):
            variant_number = i + 1
            variant_seed = (job_seed + (variant_number * self.PRIME_VARIANT_STRIDE)) & 0x7FFFFFFF
            rng_variant = random.Random(variant_seed)

            chosen: Dict[str, Dict[str, Any]] = {}

            if (not is_i2i) or enable_i2i_variations:
                for vt in variation_types_preference:
                    vrow = pick_variation(vt, rng_variant)
                    if vrow:
                        chosen[vt] = vrow

                for vt in variations_by_type.keys():
                    if vt in chosen:
                        continue
                    vrow = pick_variation(vt, rng_variant)
                    if vrow:
                        chosen[vt] = vrow

            # Build creative parts
            creative_parts: List[str] = []

            # Use-case base:
            # - T2I: always include (helps)
            # - I2I: include ONLY if you want it (often fights edit); default omit.
            if not is_i2i:
                uc_base = self._as_text(self._get(use_case, "prompt_base"))
                if uc_base:
                    creative_parts.append(uc_base)

            # style (probably None)
            if style and (not is_i2i):
                creative_parts.append(self._as_text(self._get(style, "prompt_base")))

            # context + clothing are explicit UI controls -> include for both,
            # but background prompts can fight I2I; include bg only for T2I.
            if context:
                ctx_base = self._get(context, "prompt_base") or self._get(context, "prompt_modifiers")
                if ctx_base:
                    creative_parts.append(self._as_text(ctx_base))

                bg = self._pick_one(rng_variant, self._get(context, "background_prompts"))
                if bg and (not is_i2i):
                    creative_parts.append(self._as_text(bg))

            if clothing:
                cloth_base = self._get(clothing, "prompt_base") or self._get(clothing, "prompt_descriptor")
                if cloth_base:
                    creative_parts.append(self._as_text(cloth_base))

            # variation modifiers
            if chosen:
                for _, vrow in chosen.items():
                    pm = vrow.get("prompt_modifier")
                    if pm:
                        creative_parts.append(self._as_text(pm))

            # platform guidance (safe to include for both)
            if platform:
                guidelines = platform.get("content_guidelines") or {}
                if isinstance(guidelines, dict):
                    if guidelines.get("professionalism") == "high":
                        creative_parts.append("highly professional, polished, credible")
                    if guidelines.get("authenticity") == "high":
                        creative_parts.append("authentic, documentary realism, not stock-photo")

            # -------------------------
            # Assemble prompt
            # -------------------------
            if not is_i2i:
                # T2I: demographic_base already includes user prompt
                full_prompt = self._join([demographic_base, self._join(creative_parts)])
            else:
                # I2I: Keep prompt clean and ensure user edit dominates (place it LAST)
                i2i_base = "EDIT THE INPUT PHOTO: keep the SAME person/identity"
                i2i_parts = [i2i_base]

                # If user explicitly provided demographics via UI, include them lightly (optional)
                demo_light: List[str] = []
                if age_range:
                    demo_light.append(self._as_text(self._get(age_range, "prompt_descriptor")))
                gp = gender_phrase(gender) if gender else None
                if gp:
                    demo_light.append(gp)
                if region:
                    region_display = self._get(region, "display_name")
                    if isinstance(region_display, dict):
                        region_name = region_display.get("en") or region_display.get("name") or self._get(region, "code")
                    else:
                        region_name = region_display or self._get(region, "code")
                    if region_name:
                        demo_light.append(f"from {region_name}")
                if skin_tone:
                    demo_light.append(self._as_text(self._get(skin_tone, "prompt_descriptor")))
                if demo_light:
                    i2i_parts.append(self._join(demo_light))

                # Minimal creative parts (context/clothing/platform) ok
                if creative_parts:
                    i2i_parts.append(self._join(creative_parts))

                # USER edit instruction LAST
                if translated_prompt:
                    i2i_parts.append(translated_prompt)

                full_prompt = self._join(i2i_parts)

            full_prompt = self.safety.build_safe_prompt(full_prompt).strip()
            if len(full_prompt) > 3500:
                full_prompt = full_prompt[:3500].rstrip()

            # Negative prompts
            if is_i2i:
                negative = [
                    "different person, different face, identity drift, face morphing",
                    "face swap, wrong identity, wrong gender",
                    "low quality, blurry, distorted, deformed, watermark, text overlay",
                    self.safety.get_safety_negative_prompt(),
                ]
            else:
                negative = [
                    "different age, different skin tone, different ethnicity, different gender",
                    "demographic change, western-only features, face morphing, identity drift",
                    "clone-like, identical appearance, monotonous, generic stock photo",
                    "low quality, blurry, distorted, deformed, watermark, text overlay",
                    self.safety.get_safety_negative_prompt(),
                ]

            negative_prompt = self._join(negative)

            variants.append({
                "variant_number": variant_number,
                "seed": int(variant_seed),
                "prompt": full_prompt,
                "negative_prompt": negative_prompt,
                "technical_specs": {"width": width, "height": height, "aspect_ratio": aspect_ratio},
                "creative_variations": {
                    vt: {
                        "code": vrow.get("code"),
                        "prompt_modifier": vrow.get("prompt_modifier"),
                        "professional_level": vrow.get("professional_level"),
                        "creativity_level": vrow.get("creativity_level"),
                        "mood_impact": vrow.get("mood_impact"),
                        "use_case_compatibility": vrow.get("use_case_compatibility"),
                    }
                    for vt, vrow in chosen.items()
                },
                "prompt_used": full_prompt,
                "demographic_base": demographic_base,
            })

        resolved = {
            "mode": mode_norm,
            "image_format": image_format,
            "use_case": use_case,
            "age_range": age_range,
            "region": region,
            "skin_tone": skin_tone,
            "style": style,
            "context": context,
            "clothing": clothing,
            "platform": platform,
            "job_seed": job_seed,
            "enable_i2i_variations": enable_i2i_variations,
        }
        return variants, resolved