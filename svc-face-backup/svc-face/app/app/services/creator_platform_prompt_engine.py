from __future__ import annotations

import hashlib
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.repos.creator_config_repo import CreatorPlatformConfigRepo

logger = logging.getLogger(__name__)


DEFAULT_VARIATION_TYPES = [
    "lighting",
    "expression",
    "pose",
    "camera",
    "background",
    "styling",
]


class CreatorPlatformPromptEngine:
    """
    DB-driven prompt engine:
      - Fixed demographics (age/skin/gender/region/features)
      - Varied creative execution (variation rows by type)
      - Seeded randomness for true diversity per job + per variant
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.config_repo = CreatorPlatformConfigRepo(pool)

    # ----------------------------
    # Public API
    # ----------------------------
    async def build_variants(
        self,
        *,
        request: Any,  # CreatorPlatformRequest
        translated_prompt_en: Optional[str],
        job_seed: int,
        professional_level_min: int = 3,
        creativity_level_min: int = 2,
        variation_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Returns list of prompt payloads:
          {
            prompt, negative_prompt, variant_number,
            demographic_base, creative_variations, technical_specs, seed
          }
        """
        cfg = await self._resolve_request_config(request)

        # fixed demographic base
        demographic_base = await self._build_demographic_base(
            request=request,
            cfg=cfg,
            user_prompt_en=translated_prompt_en or request.user_prompt,
        )

        # creative variations pool
        var_types = variation_types or DEFAULT_VARIATION_TYPES
        variations_by_type = await self._load_variations_by_type(
            use_case_code=request.use_case_code,
            professional_level_min=professional_level_min,
            creativity_level_min=creativity_level_min,
            variation_types=var_types,
        )

        # seeded RNG for this job
        rng = random.Random(job_seed)

        # choose combos per variant
        plan = self._make_variation_plan(
            rng=rng,
            num_variants=request.num_variants,
            variations_by_type=variations_by_type,
        )

        # build prompts
        out: List[Dict[str, Any]] = []
        safety_neg = self._base_negative_prompt()
        for i, combo in enumerate(plan):
            variant_number = i + 1
            seed = job_seed + (variant_number * 9973)

            prompt = self._compose_prompt(
                demographic_base=demographic_base,
                cfg=cfg,
                combo=combo,
            )

            technical = {
                "width": cfg["image_format"]["width"],
                "height": cfg["image_format"]["height"],
                "aspect_ratio": cfg["image_format"]["aspect_ratio"],
                "format": cfg["image_format"]["code"],
                "safe_zones": cfg["image_format"].get("safe_zones") or {},
            }

            out.append(
                {
                    "variant_number": variant_number,
                    "prompt": prompt,
                    "negative_prompt": safety_neg,
                    "demographic_base": demographic_base,
                    "creative_variations": combo,
                    "technical_specs": technical,
                    "seed": seed,
                }
            )

        return out

    def make_job_seed(self, *, user_id: str, request_hash: str, now_ms: int) -> int:
        """
        Produces a stable-ish but unique seed per request.
        We include time so repeated same prompt still yields different faces.
        """
        raw = f"{user_id}:{request_hash}:{now_ms}".encode("utf-8")
        h = hashlib.sha256(raw).hexdigest()
        return int(h[:8], 16)  # 32-bit seed

    # ----------------------------
    # Internal helpers
    # ----------------------------
    async def _resolve_request_config(self, request: Any) -> Dict[str, Any]:
        image_format = await self.config_repo.get_image_format_by_code(request.image_format_code)
        use_case = await self.config_repo.get_use_case_by_code(request.use_case_code)
        age_range = await self.config_repo.get_age_range_by_code(request.age_range_code)
        region = await self.config_repo.get_region_by_code(request.region_code)
        skin_tone = await self.config_repo.get_skin_tone_by_code(request.skin_tone_code)

        # Optional: style/context/clothing/platform
        style = await self.config_repo.get_style_by_code(request.style_code) if getattr(request, "style_code", None) else None
        context = await self.config_repo.get_context_by_code(request.context_code) if getattr(request, "context_code", None) else None
        clothing = await self.config_repo.get_clothing_style_by_code(request.clothing_style_code) if getattr(request, "clothing_style_code", None) else None
        platform = await self.config_repo.get_platform_requirements_by_code(request.platform_code) if getattr(request, "platform_code", None) else None

        missing = []
        for k, v in [
            ("image_format", image_format),
            ("use_case", use_case),
            ("age_range", age_range),
            ("region", region),
            ("skin_tone", skin_tone),
        ]:
            if not v:
                missing.append(k)
        if missing:
            raise ValueError(f"Missing config rows for: {missing}")

        # Normalize to dicts (repo likely returns dict already; this keeps usage consistent)
        return {
            "image_format": self._as_dict(image_format),
            "use_case": self._as_dict(use_case),
            "age_range": self._as_dict(age_range),
            "region": self._as_dict(region),
            "skin_tone": self._as_dict(skin_tone),
            "style": self._as_dict(style) if style else None,
            "context": self._as_dict(context) if context else None,
            "clothing": self._as_dict(clothing) if clothing else None,
            "platform": self._as_dict(platform) if platform else None,
            "facial_features": getattr(request, "facial_features", None) or {},
        }

    async def _build_demographic_base(self, *, request: Any, cfg: Dict[str, Any], user_prompt_en: Optional[str]) -> str:
        ar = cfg["age_range"]
        reg = cfg["region"]
        st = cfg["skin_tone"]

        # Display name fallback
        reg_name = (reg.get("display_name") or {}).get("en") or reg.get("code")

        parts = [
            ar.get("prompt_descriptor") or "",
            f"{request.gender} person" if isinstance(request.gender, str) else f"{request.gender.value} person",
            f"from {reg_name}",
            st.get("prompt_descriptor") or "",
            reg.get("prompt_base") or "",
            "authentic Indian features, natural appearance, non-western beauty standards",
        ]

        # Optional facial features from DB
        if cfg["facial_features"]:
            # Expect request.facial_features like {"eye_shape":"almond","nose_type":"medium"}
            # You can map these to face_generation_features if you add feature_type/code accordingly.
            # For now: treat as direct descriptors (safe + simple)
            for k, v in cfg["facial_features"].items():
                parts.append(f"{k.replace('_',' ')} {v}")

        if user_prompt_en and str(user_prompt_en).strip():
            parts.append(str(user_prompt_en).strip())

        return ", ".join([p.strip() for p in parts if p and p.strip()])

    async def _load_variations_by_type(
        self,
        *,
        use_case_code: str,
        professional_level_min: int,
        creativity_level_min: int,
        variation_types: List[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        face_generation_variations:
          variation_type, code, prompt_modifier, use_case_compatibility[], professional_level, creativity_level, is_active
        """
        q = """
        SELECT variation_type, code, prompt_modifier, mood_impact
        FROM public.face_generation_variations
        WHERE is_active = TRUE
          AND variation_type = ANY($1::text[])
          AND professional_level >= $2
          AND creativity_level >= $3
          AND (
            use_case_compatibility IS NULL
            OR array_length(use_case_compatibility, 1) IS NULL
            OR $4 = ANY(use_case_compatibility)
          )
        ORDER BY variation_type, code
        """
        rows = await self.config_repo.execute_queries(q, variation_types, professional_level_min, creativity_level_min, use_case_code)
        rows = [self.config_repo.convert_db_row(r) for r in rows]

        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_type.setdefault(r["variation_type"], []).append(r)
        return by_type

    def _make_variation_plan(
        self,
        *,
        rng: random.Random,
        num_variants: int,
        variations_by_type: Dict[str, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """
        Build variant combos with high uniqueness.
        If some type has few options, we still rotate + shuffle.
        """
        plan: List[Dict[str, Any]] = []
        used_signatures = set()

        # Pre-shuffle each type
        pools: Dict[str, List[Dict[str, Any]]] = {}
        for t, items in variations_by_type.items():
            items2 = list(items)
            rng.shuffle(items2)
            pools[t] = items2

        for i in range(num_variants):
            combo: Dict[str, Any] = {}
            for t, items in pools.items():
                if not items:
                    continue
                pick = items[(i * 3 + rng.randint(0, 9999)) % len(items)]
                combo[t] = {
                    "code": pick["code"],
                    "prompt_modifier": pick["prompt_modifier"],
                    "mood_impact": pick.get("mood_impact"),
                }

            # avoid duplicates
            sig = "|".join(sorted([f"{t}:{v['code']}" for t, v in combo.items()]))
            if sig in used_signatures and any(pools.values()):
                # try small re-roll
                for _ in range(5):
                    for t, items in pools.items():
                        if items:
                            pick = items[rng.randint(0, len(items) - 1)]
                            combo[t] = {
                                "code": pick["code"],
                                "prompt_modifier": pick["prompt_modifier"],
                                "mood_impact": pick.get("mood_impact"),
                            }
                    sig = "|".join(sorted([f"{t}:{v['code']}" for t, v in combo.items()]))
                    if sig not in used_signatures:
                        break

            used_signatures.add(sig)
            plan.append(combo)

        return plan

    def _compose_prompt(self, *, demographic_base: str, cfg: Dict[str, Any], combo: Dict[str, Any]) -> str:
        use_case = cfg["use_case"]
        style = cfg.get("style")
        context = cfg.get("context")
        clothing = cfg.get("clothing")
        platform = cfg.get("platform")
        image_format = cfg["image_format"]

        parts = [demographic_base]

        # Use-case base prompt
        if use_case and use_case.get("prompt_base"):
            parts.append(use_case["prompt_base"])

        # Optional enrichers
        if style and style.get("prompt_base"):
            parts.append(style["prompt_base"])
        if context and context.get("prompt_modifiers"):
            parts.append(context["prompt_modifiers"])
        if clothing and clothing.get("prompt_descriptor"):
            parts.append(clothing["prompt_descriptor"])

        # Platform optimization should be guidance, not lock
        if platform and platform.get("content_guidelines"):
            cg = platform["content_guidelines"]
            if cg.get("professionalism") == "high":
                parts.append("professional, credible, authentic, not over-edited")

        # Creative variations
        for t, v in combo.items():
            if v.get("prompt_modifier"):
                parts.append(v["prompt_modifier"])
            if v.get("mood_impact"):
                parts.append(v["mood_impact"])

        # Technical guidance
        parts.append(f"{image_format.get('aspect_ratio')} aspect ratio")
        parts.append("high quality portrait photography, realistic skin texture, sharp focus, natural imperfections, no plastic skin")

        return ", ".join([p.strip() for p in parts if p and p.strip()])

    def _base_negative_prompt(self) -> str:
        # Critical: stop demographic drift + stop clones + stop low quality
        return (
            "different age, different skin tone, different ethnicity, demographic change, different gender, "
            "western celebrity look, overly retouched, plastic skin, generic stock photo, clone-like, identical appearance, monotonous, "
            "blurry, low quality, deformed, distorted, extra limbs, watermark, text overlay"
        )

    def _as_dict(self, x: Any) -> Dict[str, Any]:
        return x if isinstance(x, dict) else getattr(x, "model_dump", lambda: dict(x))()