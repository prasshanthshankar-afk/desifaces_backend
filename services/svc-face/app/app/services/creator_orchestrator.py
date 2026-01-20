from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from urllib.parse import urlparse

from ..domain.models import (
    CreatorPlatformRequest,
    JobCreatedResponse,
    JobStatusResponse,
    GeneratedVariant,
    JobStatus,
)

from ..repos.face_jobs_repo import FaceJobsRepo
from ..repos.face_profiles_repo import FaceProfilesRepo
from ..repos.media_assets_repo import MediaAssetsRepo
from ..repos.creator_config_repo import CreatorPlatformConfigRepo
from ..repos.artifacts_repo import ArtifactsRepo

from app.services.creator_prompt_service import CreatorPromptService
from app.services.azure_storage_service import AzureStorageService
from app.services.fal_client import FalClient
from app.services.safety_service import SafetyService
from app.services.translation_service import TranslationService
from app.services.idempotency_service import provider_idempotency_key

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


class CreatorOrchestrator:
    PRIME_HASH_BYTES = 16

    SEED_MODULUS = 2**31 - 1
    SEED_CONTEXT = "df:seed:v1"
    SEED_ENV_HEX = "DF_SEED_SECRET_HEX"

    ID_CONTEXT = "df:identity:v2"

    ID_FACE_SHAPES = [
        "oval",
        "round",
        "square",
        "heart-shaped",
        "diamond-shaped",
        "rectangular",
        "softly angular",
        "chubby cheeks",
        "lean face",
        "broad face",
    ]
    ID_JAWLINES = [
        "soft jawline",
        "defined jawline",
        "sharp jawline",
        "gentle jawline",
        "strong jawline",
        "narrow jawline",
        "wide jawline",
    ]
    ID_CHEEKBONES = [
        "high cheekbones",
        "soft cheekbones",
        "pronounced cheekbones",
        "subtle cheekbones",
        "full cheeks",
    ]
    ID_NOSES = [
        "straight nose",
        "button nose",
        "aquiline nose",
        "broad nose",
        "narrow nose",
        "rounded nose tip",
        "sharp nose bridge",
    ]
    ID_EYES = [
        "almond eyes",
        "round eyes",
        "hooded eyes",
        "deep-set eyes",
        "upturned eyes",
        "downturned eyes",
        "wide-set eyes",
        "close-set eyes",
    ]
    ID_EYEBROWS = [
        "arched eyebrows",
        "straight eyebrows",
        "thick eyebrows",
        "soft eyebrows",
        "defined eyebrows",
        "subtle eyebrows",
    ]
    ID_LIPS = [
        "full lips",
        "thin lips",
        "balanced lips",
        "wide smile lines",
        "narrow lips",
        "defined cupid's bow",
    ]
    ID_CHINS = [
        "rounded chin",
        "pointed chin",
        "square chin",
        "soft chin",
        "prominent chin",
        "small chin",
    ]
    ID_EYE_SPACING = ["wide-set eyes", "average eye spacing", "close-set eyes"]
    ID_FACE_PROPORTIONS = [
        "short midface",
        "long midface",
        "balanced midface",
        "short lower face",
        "long lower face",
        "balanced proportions",
    ]
    ID_EXPRESSIONS = [
        "neutral expression",
        "soft smile",
        "warm smile",
        "serious expression",
        "confident expression",
        "thoughtful expression",
        "slight smirk",
        "laughing expression",
        "angry expression",
    ]
    ID_MARKS = [
        "no visible facial marks",
        "subtle freckles",
        "a small beauty mark",
        "faint acne texture",
        "light smile lines",
    ]
    ID_NEG_DEFAULT = (
        "same person, identical face, twin, clone, repeated identity, "
        "same facial structure, same bone structure, same nose, same jawline, same cheekbones, "
        "overly generic face, stock photo face"
    )

    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    )

    def __init__(self, db_pool):
        self.jobs_repo = FaceJobsRepo(db_pool)
        self.profiles_repo = FaceProfilesRepo(db_pool)
        self.assets_repo = MediaAssetsRepo(db_pool)
        self.creator_config_repo = CreatorPlatformConfigRepo(db_pool)
        self.artifacts_repo = ArtifactsRepo(db_pool)

        self.storage_service = AzureStorageService()
        self.fal_client = FalClient()
        self.safety_service = SafetyService()
        self.translation_service = TranslationService()

        self.prompt_service = CreatorPromptService(
            db_pool=db_pool,
            safety=self.safety_service,
            translator=self.translation_service,
            config_repo=self.creator_config_repo,
        )

        self._seed_secret_cached: Optional[bytes] = None
        self._seed_secret_warned: bool = False

    # -------------------------
    # Helpers
    # -------------------------
    @staticmethod
    def _stable_json(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))

    @classmethod
    def _generate_request_hash(cls, payload: Dict[str, Any]) -> str:
        stable_payload = cls._stable_json(payload)
        return hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()[: cls.PRIME_HASH_BYTES]

    @classmethod
    def _stable_seed_from(cls, payload: Dict[str, Any]) -> int:
        stable_payload = cls._stable_json(payload)
        h = hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()
        return int(h[:8], 16) & 0x7FFFFFFF

    @staticmethod
    def _job_status_str(x: Any) -> str:
        return str(x or "").strip().lower()

    @staticmethod
    def _coerce_gender(g: Any) -> str:
        if g is None:
            return "person"
        if hasattr(g, "value"):
            return str(g.value)
        if isinstance(g, dict) and "value" in g:
            return str(g.get("value"))
        return str(g)

    @staticmethod
    def _coerce_dict(v: Any) -> Dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return {}

    @staticmethod
    def _coerce_mode(m: Any) -> str:
        s = str(m or "").strip().lower().replace("_", "-")
        if s in ("image-to-image", "i2i", "img2img"):
            return "image-to-image"
        if s in ("text-to-image", "t2i", "txt2img"):
            return "text-to-image"
        return "text-to-image"

    @staticmethod
    def _clamp_strength(v: Any, default: float = 0.25) -> float:
        try:
            f = float(v)
        except Exception:
            f = float(default)
        return max(0.10, min(0.60, f))

    def _validate_http_url(self, url: str) -> None:
        u = (url or "").strip()
        p = urlparse(u)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"invalid_url_scheme:{p.scheme or 'missing'}")
        if not p.netloc:
            raise ValueError(f"invalid_url_missing_host:{u}")

    async def _resolve_source_image_ref(self, ref: str) -> str:
        raw = (ref or "").strip()
        if not raw:
            return ""

        img_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")

        def _host_looks_invalid(netloc: str) -> bool:
            h = (netloc or "").strip().lower()
            if not h:
                return True
            if h in (".", "..", "..."):
                return True
            if any(h.endswith(ext) for ext in img_exts):
                return True
            if h == "localhost":
                return False
            if "." not in h:
                return True
            return False

        p = urlparse(raw)
        if p.scheme in ("http", "https"):
            if not p.netloc or _host_looks_invalid(p.netloc):
                candidate_key = (p.path or "").lstrip("/")
                if p.query:
                    candidate_key = f"{candidate_key}?{p.query}" if candidate_key else f"?{p.query}"

                head = candidate_key.split("?", 1)[0].split("#", 1)[0].strip()
                if head and "/" in head:
                    return await self._resolve_source_image_ref(candidate_key)

                raise RuntimeError(f"invalid_or_unusable_source_image_url:{raw}")

            return raw

        if p.scheme == "file":
            return raw

        if self._UUID_RE.match(raw):
            ma = await self.assets_repo.get_asset(raw) if hasattr(self.assets_repo, "get_asset") else None
            storage_ref = None
            if ma:
                storage_ref = ma.get("storage_ref") if isinstance(ma, dict) else getattr(ma, "storage_ref", None)
            if storage_ref:
                return await self._resolve_source_image_ref(str(storage_ref))

        head = raw.split("?", 1)[0].split("#", 1)[0].strip()
        first = head.split("/", 1)[0].strip()

        if first.lower().endswith(img_exts):
            pass
        else:
            host = first.split(":", 1)[0]
            parts = host.split(".")
            tld = parts[-1].lower() if len(parts) >= 2 else ""
            if tld in ("jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff"):
                pass
            elif len(parts) >= 2 and tld.isalpha() and 2 <= len(tld) <= 24:
                return "https://" + raw

        if "/" not in head:
            raise RuntimeError(f"unresolvable_source_image_ref:{raw}")

        ss = self.storage_service
        for fn_name in (
            "ensure_https_url",
            "to_public_url",
            "get_public_url",
            "generate_read_sas_url",
            "get_read_sas_url",
        ):
            fn = getattr(ss, fn_name, None)
            if callable(fn):
                try:
                    out = fn(raw)
                    if hasattr(out, "__await__"):
                        out = await out
                    if out:
                        out = str(out).strip()
                        pp = urlparse(out)
                        if pp.scheme in ("http", "https") and pp.netloc and not _host_looks_invalid(pp.netloc):
                            return out
                except Exception:
                    pass

        raise RuntimeError(f"unresolvable_source_image_ref:{raw}")

    # -------------------------
    # MINIMAL FIX: ensure required codes exist
    # -------------------------
    async def _ensure_required_config_codes(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        rd = request_dict

        if not (rd.get("image_format_code") or "").strip():
            use_case_code = (rd.get("use_case_code") or "").strip()
            picked: Optional[str] = None

            if use_case_code:
                try:
                    uc = await self.creator_config_repo.get_use_case_by_code(use_case_code)
                    rec = uc.get("recommended_formats") if isinstance(uc, dict) else getattr(uc, "recommended_formats", None)
                    if isinstance(rec, list) and rec:
                        picked = str(rec[0])
                except Exception:
                    picked = None

            if not picked:
                try:
                    fmts = await self.creator_config_repo.get_image_formats()
                    if fmts:
                        f0 = fmts[0]
                        picked = f0.get("code") if isinstance(f0, dict) else getattr(f0, "code", None)
                        picked = str(picked) if picked else None
                except Exception:
                    picked = None

            if picked:
                rd["image_format_code"] = picked

        if not (rd.get("age_range_code") or "").strip():
            try:
                ages = await self.creator_config_repo.get_age_ranges()
                if ages:
                    a0 = ages[0]
                    code = a0.get("code") if isinstance(a0, dict) else getattr(a0, "code", None)
                    if code:
                        rd["age_range_code"] = str(code)
            except Exception:
                pass

        if not (rd.get("skin_tone_code") or "").strip():
            try:
                tones = await self.creator_config_repo.get_skin_tones()
                picked = None
                for t in (tones or []):
                    c = t.get("code") if isinstance(t, dict) else getattr(t, "code", None)
                    if c == "medium_brown":
                        picked = "medium_brown"
                        break
                if not picked and tones:
                    t0 = tones[0]
                    c0 = t0.get("code") if isinstance(t0, dict) else getattr(t0, "code", None)
                    picked = str(c0) if c0 else None
                if picked:
                    rd["skin_tone_code"] = picked
            except Exception:
                pass

        return rd

    # -------------------------
    # Provider runs (dashboard) - minimal, best-effort, never breaks pipeline
    # -------------------------
    def _prune_provider_meta(self, meta: Any) -> Dict[str, Any]:
        m = self._coerce_dict(meta)
        if not m:
            return {}
        if "raw" in m:
            m = dict(m)
            m.pop("raw", None)
        try:
            s = json.dumps(m, default=str)
            if len(s) > 8000:
                return {"meta_truncated": True}
        except Exception:
            pass
        return m

    async def _provider_runs_upsert(
        self,
        *,
        job_id: str,
        provider: str,
        idempotency_key: str,
        provider_status: str,
        request_json: Dict[str, Any],
        response_json: Dict[str, Any],
        meta_json: Dict[str, Any],
    ) -> None:
        q = """
        INSERT INTO public.provider_runs (
          job_id, provider, idempotency_key, provider_status,
          request_json, response_json, meta_json,
          created_at, updated_at
        )
        VALUES (
          $1::uuid, $2::text, $3::text, $4::text,
          $5::jsonb, $6::jsonb, $7::jsonb,
          now(), now()
        )
        ON CONFLICT (idempotency_key)
        DO UPDATE SET
          job_id          = EXCLUDED.job_id,
          provider        = EXCLUDED.provider,
          provider_status = EXCLUDED.provider_status,
          request_json    = EXCLUDED.request_json,
          response_json   = EXCLUDED.response_json,
          meta_json       = EXCLUDED.meta_json,
          updated_at      = now()
        """
        try:
            await self.jobs_repo.execute_command(
                q,
                job_id,
                provider,
                idempotency_key,
                provider_status,
                self.jobs_repo.prepare_jsonb_param(request_json or {}),
                self.jobs_repo.prepare_jsonb_param(response_json or {}),
                self.jobs_repo.prepare_jsonb_param(meta_json or {}),
            )
        except Exception:
            return

    # -------------------------
    # Seeding
    # -------------------------
    def _get_seed_secret(self) -> Optional[bytes]:
        if self._seed_secret_cached is not None:
            return self._seed_secret_cached

        hx = (os.getenv(self.SEED_ENV_HEX) or "").strip()
        if not hx:
            if not self._seed_secret_warned:
                self._seed_secret_warned = True
                logger.warning(
                    "DF_SEED_SECRET_HEX not set; HMAC seeding disabled. Falling back to deterministic seed mixing."
                )
            self._seed_secret_cached = None
            return None

        try:
            secret = bytes.fromhex(hx)
            if len(secret) < 16:
                raise ValueError("secret too short")
            self._seed_secret_cached = secret
            return secret
        except Exception:
            if not self._seed_secret_warned:
                self._seed_secret_warned = True
                logger.warning(
                    "Invalid DF_SEED_SECRET_HEX; HMAC seeding disabled. Falling back to deterministic seed mixing."
                )
            self._seed_secret_cached = None
            return None

    @classmethod
    def _new_random_job_seed(cls, bits: int = 63) -> int:
        return secrets.randbits(bits)

    def _derive_variant_seed_hmac(
        self,
        *,
        job_seed: int,
        variant_number: int,
        purpose: str = "face:gen",
        request_hash: str = "",
    ) -> int:
        idx = max(0, int(variant_number) - 1)
        secret = self._get_seed_secret()

        if secret:
            msg = f"{self.SEED_CONTEXT}|{purpose}|job_seed={int(job_seed)}|v={idx}"
            if request_hash:
                msg += f"|rh={request_hash}"
            digest = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).digest()
            n = int.from_bytes(digest[:8], "big")
            return int(n % self.SEED_MODULUS)

        msg = f"{self.SEED_CONTEXT}|{purpose}|job_seed={int(job_seed)}|v={idx}|rh={request_hash}"
        hh = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        return int(int(hh[:8], 16) % self.SEED_MODULUS)

    def _pre_resolve_seed_mode(self, request_dict: Dict[str, Any]) -> str:
        sent_seed_mode = "seed_mode" in request_dict
        sent_seed = "seed" in request_dict

        if not sent_seed_mode and not sent_seed:
            return "deterministic"

        seed_mode = str(request_dict.get("seed_mode") or "auto").strip().lower()
        user_seed = request_dict.get("seed", None)

        if seed_mode not in ("auto", "random", "deterministic"):
            seed_mode = "auto"

        if seed_mode == "auto":
            return "deterministic" if user_seed is not None else "random"

        return seed_mode

    def _resolve_seed_mode_and_job_seed(
        self,
        *,
        request_dict: Dict[str, Any],
        request_hash_payload: Dict[str, Any],
    ) -> Tuple[str, int]:
        sent_seed_mode = "seed_mode" in request_dict
        sent_seed = "seed" in request_dict

        if not sent_seed_mode and not sent_seed:
            return "deterministic", int(self._stable_seed_from(request_hash_payload))

        seed_mode = str(request_dict.get("seed_mode") or "auto").strip().lower()
        user_seed = request_dict.get("seed", None)

        if seed_mode not in ("auto", "random", "deterministic"):
            seed_mode = "auto"

        if seed_mode == "auto":
            seed_mode = "deterministic" if user_seed is not None else "random"

        if seed_mode == "deterministic":
            if user_seed is None:
                return "deterministic", int(self._stable_seed_from(request_hash_payload))
            try:
                return "deterministic", int(user_seed)
            except Exception:
                return "deterministic", int(self._stable_seed_from(request_hash_payload))

        return "random", int(self._new_random_job_seed())

    # -------------------------
    # Identity (T2I) helpers
    # -------------------------
    def _id_digest(self, *, job_seed: int, request_hash: str, key: str) -> bytes:
        msg = f"{self.ID_CONTEXT}|{key}|job_seed={int(job_seed)}|rh={request_hash}".encode("utf-8")
        secret = self._get_seed_secret()
        return hmac.new(secret, msg, hashlib.sha256).digest() if secret else hashlib.sha256(msg).digest()

    def _id_pick(self, *, job_seed: int, request_hash: str, key: str, options: List[str]) -> str:
        if not options:
            return ""
        d = self._id_digest(job_seed=job_seed, request_hash=request_hash, key=key)
        n = int.from_bytes(d[:8], "big")
        return options[n % len(options)]

    def _id_bool(self, *, job_seed: int, request_hash: str, key: str, true_pct: int) -> bool:
        d = self._id_digest(job_seed=job_seed, request_hash=request_hash, key=f"bool:{key}")
        n = int.from_bytes(d[:4], "big") % 100
        return n < max(0, min(100, int(true_pct)))

    def _build_identity_profile(self, *, job_seed: int, request_hash: str, request_dict: Dict[str, Any]) -> Dict[str, str]:
        signature = hashlib.sha256(f"{self.ID_CONTEXT}|{job_seed}|{request_hash}".encode("utf-8")).hexdigest()[:12]

        face = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="face_shape", options=self.ID_FACE_SHAPES)
        jaw = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="jawline", options=self.ID_JAWLINES)
        nose = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="nose", options=self.ID_NOSES)
        eyes = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="eyes", options=self.ID_EYES)

        spacing = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="eye_spacing", options=self.ID_EYE_SPACING)
        proportions = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="proportions", options=self.ID_FACE_PROPORTIONS)

        cheek = (
            self._id_pick(job_seed=job_seed, request_hash=request_hash, key="cheekbones", options=self.ID_CHEEKBONES)
            if self._id_bool(job_seed=job_seed, request_hash=request_hash, key="use_cheekbones", true_pct=70)
            else ""
        )
        brows = (
            self._id_pick(job_seed=job_seed, request_hash=request_hash, key="brows", options=self.ID_EYEBROWS)
            if self._id_bool(job_seed=job_seed, request_hash=request_hash, key="use_brows", true_pct=60)
            else ""
        )
        lips = (
            self._id_pick(job_seed=job_seed, request_hash=request_hash, key="lips", options=self.ID_LIPS)
            if self._id_bool(job_seed=job_seed, request_hash=request_hash, key="use_lips", true_pct=60)
            else ""
        )
        chin = (
            self._id_pick(job_seed=job_seed, request_hash=request_hash, key="chin", options=self.ID_CHINS)
            if self._id_bool(job_seed=job_seed, request_hash=request_hash, key="use_chin", true_pct=55)
            else ""
        )

        expression = (
            self._id_pick(job_seed=job_seed, request_hash=request_hash, key="expression", options=self.ID_EXPRESSIONS)
            if self._id_bool(job_seed=job_seed, request_hash=request_hash, key="use_expression", true_pct=65)
            else "neutral expression"
        )

        mark = self._id_pick(job_seed=job_seed, request_hash=request_hash, key="marks", options=self.ID_MARKS)

        base_anchor = "different person, distinct facial identity, unique individual"
        realism = "natural facial asymmetry, realistic pores, realistic skin texture"

        parts = [
            p
            for p in [
                face and f"{face} face",
                jaw,
                cheek,
                nose,
                eyes,
                spacing,
                proportions,
                brows,
                lips,
                chin,
                expression,
                mark,
            ]
            if p
        ]
        tokens = f"{base_anchor}, {', '.join(parts)}, {realism}"

        user_prompt = str(request_dict.get("user_prompt") or "").lower()
        neg = self.ID_NEG_DEFAULT
        if "smile" in user_prompt:
            neg = neg
        return {"signature": signature, "tokens": tokens, "negative_tokens": neg}

    # -------------------------
    # Public API
    # -------------------------
    async def create_job(self, user_id: str, request: CreatorPlatformRequest) -> JobCreatedResponse:
        logger.info(
            "Creating creator platform job",
            extra={
                "user_id": user_id,
                "image_format": getattr(request, "image_format_code", None),
                "use_case": getattr(request, "use_case_code", None),
                "variants": getattr(request, "num_variants", None),
            },
        )

        request_dict: JsonDict = request.model_dump(mode="json")

        mode = self._coerce_mode(request_dict.get("mode"))
        if mode == "image-to-image":
            ref = (request_dict.get("source_image_url") or "").strip()
            logger.debug("Image-to-image mode; resolving source image URL", extra={"ref": ref})

            if not ref:
                raise ValueError("missing_required_fields: ['source_image_url'] for image-to-image mode")

            try:
                resolved = await self._resolve_source_image_ref(ref)
            except Exception as e:
                raise ValueError(f"invalid_source_image_url:{ref} err={e!s}") from e

            request_dict["source_image_url"] = resolved

        translation_meta: Dict[str, Any] = {}
        if request_dict.get("user_prompt"):
            translation_meta = await self.prompt_service.translate_and_validate(
                user_prompt=request_dict.get("user_prompt") or "",
                language=request_dict.get("language") or "en",
            )
            request_dict["translated_prompt"] = (
                translation_meta.get("user_prompt_translated_en") or request_dict.get("user_prompt")
            )
            request_dict.update(translation_meta)

        request_dict = await self._ensure_required_config_codes(request_dict)

        pre_mode = self._pre_resolve_seed_mode(request_dict)

        fields_set = getattr(request, "model_fields_set", set())
        sent_seed_mode = "seed_mode" in fields_set
        sent_seed = "seed" in fields_set
        sent_nonce = "request_nonce" in fields_set

        if pre_mode == "random":
            request_dict["request_nonce"] = (request_dict.get("request_nonce") or uuid4().hex)

        request_hash_payload = {
            "language": request_dict.get("language"),
            "user_prompt": request_dict.get("user_prompt"),
            "user_prompt_translated_en": request_dict.get("user_prompt_translated_en")
            or request_dict.get("translated_prompt"),
            "num_variants": request_dict.get("num_variants"),
            "age_range_code": request_dict.get("age_range_code"),
            "skin_tone_code": request_dict.get("skin_tone_code"),
            "region_code": request_dict.get("region_code"),
            "gender": request_dict.get("gender"),
            "image_format_code": request_dict.get("image_format_code"),
            "use_case_code": request_dict.get("use_case_code"),
            "style_code": request_dict.get("style_code"),
            "context_code": request_dict.get("context_code"),
            "clothing_style_code": request_dict.get("clothing_style_code"),
            "platform_code": request_dict.get("platform_code"),
        }

        if pre_mode == "random":
            request_hash_payload["request_nonce"] = request_dict.get("request_nonce")

        if sent_seed_mode or sent_seed or sent_nonce:
            request_hash_payload["seed_mode"] = request_dict.get("seed_mode")
            request_hash_payload["seed"] = request_dict.get("seed")
            request_hash_payload["request_nonce"] = request_dict.get("request_nonce")

        if mode == "image-to-image":
            request_hash_payload["mode"] = "image-to-image"
            request_hash_payload["source_image_url"] = request_dict.get("source_image_url")
            request_hash_payload["preservation_strength"] = request_dict.get("preservation_strength")

        request_hash = self._generate_request_hash(request_hash_payload)

        seed_mode, job_seed = self._resolve_seed_mode_and_job_seed(
            request_dict=request_dict,
            request_hash_payload=request_hash_payload,
        )

        request_dict["seed_mode"] = seed_mode
        request_dict["job_seed"] = int(job_seed)
        request_dict["mode"] = mode

        job_id = await self.jobs_repo.create_job(
            user_id=user_id,
            studio_type="face",
            request_hash=request_hash,
            payload=request_dict,
            meta={
                "request_type": "creator_platform",
                "api_version": "v2",
                "language": request_dict.get("language") or "en",
                "safety_validated": True if request_dict.get("user_prompt") else False,
                "translation_success": bool(request_dict.get("translation_success")) if translation_meta else True,
                "config_validated": True,
                "seed_mode": seed_mode,
                "job_seed": int(job_seed),
                "request_nonce": request_dict.get("request_nonce"),
                "mode": mode,
                "source_image_url": request_dict.get("source_image_url") if mode == "image-to-image" else None,
                "preservation_strength": request_dict.get("preservation_strength") if mode == "image-to-image" else None,
            },
        )

        return JobCreatedResponse(
            job_id=job_id,
            status="queued",
            message="Creator face generation started",
            estimated_completion_time="~60 seconds",
            config={
                "use_case": request_dict.get("use_case_code"),
                "image_format": request_dict.get("image_format_code"),
                "platform_optimized": True,
                "variants_requested": request_dict.get("num_variants"),
                "demographics_fixed": True,
                "creativity_varied": True,
                "mode": mode,
            },
        )

    async def process_job(self, job_id: str) -> None:
        logger.info("Processing creator platform job", extra={"job_id": job_id})

        try:
            job = await self.jobs_repo.get_job(job_id)
            if not job:
                logger.error("Job not found", extra={"job_id": job_id})
                return

            status = self._job_status_str(getattr(job, "status", None))

            if status == "queued":
                try:
                    await self.jobs_repo.update_status(job_id, "running")
                except Exception:
                    pass
                status = "running"

            if status != "running":
                logger.info("Job not processable", extra={"job_id": job_id, "status": status})
                return

            payload_json = getattr(job, "payload_json", None)
            if not isinstance(payload_json, dict):
                await self.jobs_repo.update_status(
                    job_id,
                    "failed",
                    error_code="INVALID_PAYLOAD",
                    error_message="Job payload is not a dict",
                )
                return

            payload_json = await self._ensure_required_config_codes(payload_json)

            user_id = str(getattr(job, "user_id", "") or "")

            job_seed: Optional[int] = None
            seed_mode: Optional[str] = None

            mode = self._coerce_mode(payload_json.get("mode"))
            meta_json = getattr(job, "meta_json", None)
            if isinstance(meta_json, dict):
                job_seed = meta_json.get("job_seed")
                seed_mode = meta_json.get("seed_mode")
                mode = self._coerce_mode(meta_json.get("mode") or mode)

            if job_seed is None:
                job_seed = self._stable_seed_from(payload_json)
            if not seed_mode:
                seed_mode = "deterministic"

            request_hash = str(getattr(job, "request_hash", "") or "")

            source_image_url = (payload_json.get("source_image_url") or "").strip()
            if mode == "text-to-image" and source_image_url:
                mode = "image-to-image"
            if mode == "image-to-image" and not source_image_url:
                await self.jobs_repo.update_status(
                    job_id,
                    "failed",
                    error_code="MISSING_SOURCE_IMAGE",
                    error_message="image-to-image mode requires source_image_url",
                )
                return

            variants, resolved = await self.prompt_service.build_variants(
                request_dict=payload_json,
                job_seed=int(job_seed),
            )

            for v in variants:
                vn = int(v.get("variant_number") or 1)
                v["seed_mode"] = seed_mode
                v["job_seed"] = int(job_seed)
                v["mode"] = mode
                v["request_hash"] = request_hash  # minimal: used only for provider_runs idempotency
                v["seed"] = self._derive_variant_seed_hmac(
                    job_seed=int(job_seed),
                    variant_number=vn,
                    purpose="face:gen",
                    request_hash=request_hash,
                )

            if mode == "text-to-image":
                ident = self._build_identity_profile(
                    job_seed=int(job_seed),
                    request_hash=request_hash,
                    request_dict=payload_json,
                )
                identity_signature = ident.get("signature")

                for v in variants:
                    v["identity_signature"] = identity_signature
                    p = (v.get("prompt") or "").strip()
                    n = (v.get("negative_prompt") or "").strip()
                    v["prompt"] = f"{p}, {ident['tokens']}" if p else ident["tokens"]
                    v["negative_prompt"] = f"{n}, {ident['negative_tokens']}" if n else ident["negative_tokens"]

            generated: List[GeneratedVariant] = []

            for v in variants:
                try:
                    out = await self._process_variant(
                        job_id=job_id,
                        user_id=user_id,
                        request_dict=payload_json,
                        resolved_config=resolved,
                        variant=v,
                        mode=mode,
                    )
                    generated.append(out)
                except Exception as e:
                    logger.error(
                        "Variant failed",
                        extra={"job_id": job_id, "variant": v.get("variant_number"), "error": str(e)},
                        exc_info=True,
                    )
                    continue

            if generated:
                await self.jobs_repo.update_status(job_id, "succeeded")
            else:
                await self.jobs_repo.update_status(
                    job_id,
                    "failed",
                    error_code="ALL_VARIANTS_FAILED",
                    error_message="All image generation variants failed",
                )

        except Exception as e:
            logger.exception("Job processing failed", extra={"job_id": job_id})
            await self.jobs_repo.update_status(
                job_id,
                "failed",
                error_code="PROCESSING_ERROR",
                error_message=str(e),
            )

    # ============================================================================
    # PRIVATE METHODS
    # ============================================================================
    async def _process_variant(
        self,
        job_id: str,
        user_id: str,
        request_dict: Dict[str, Any],
        resolved_config: Dict[str, Any],
        variant: Dict[str, Any],
        mode: str,
    ) -> GeneratedVariant:
        import os
        import uuid
        import httpx
        from urllib.parse import urlparse

        from app.services.providers.image_provider import ImageProviderRouter

        variant_num = int(variant.get("variant_number") or 1)
        seed = int(variant.get("seed") or 0)

        prompt = (variant.get("prompt") or "").strip()
        neg = (variant.get("negative_prompt") or "").strip()

        technical = self._coerce_dict(variant.get("technical_specs"))
        width = int(technical.get("width") or 512)
        height = int(technical.get("height") or 512)
        num_steps = int(technical.get("num_inference_steps") or 28)
        guidance = float(technical.get("guidance_scale") or 3.5)

        source_image_ref: Optional[str] = None
        source_image_url: Optional[str] = None
        strength: Optional[float] = None

        router = getattr(self, "image_provider_router", None)
        if router is None:
            router = ImageProviderRouter()
            self.image_provider_router = router

        tmp_src_path: Optional[str] = None
        tmp_out_path: Optional[str] = None

        # provider_runs: deterministic idempotency per (job, variant, mode)
        provider_name = "openai"  # your orchestrator currently forces openai
        payload_version = "face:v1"
        base_rh = str(variant.get("request_hash") or "").strip() or str(job_id)
        rh_variant = hashlib.sha256(f"{base_rh}|v={variant_num}|mode={mode}".encode("utf-8")).hexdigest()[: self.PRIME_HASH_BYTES]
        idem_key = provider_idempotency_key(provider_name, payload_version, rh_variant)

        async def _download_to_tmp(url: str, dst_path: str) -> None:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, trust_env=False) as client:
                r = await client.get(url)
                r.raise_for_status()
                with open(dst_path, "wb") as f:
                    f.write(r.content)

        # best-effort log "created"
        await self._provider_runs_upsert(
            job_id=job_id,
            provider=provider_name,
            idempotency_key=idem_key,
            provider_status="created",
            request_json={
                "studio_type": "face",
                "mode": mode,
                "variant_number": variant_num,
                "seed": seed,
                "width": width,
                "height": height,
                "num_inference_steps": num_steps,
                "guidance_scale": guidance,
                "preservation_strength": None,
            },
            response_json={},
            meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
        )

        try:
            if mode == "image-to-image":
                payload_json = request_dict

                source_image_ref = (
                    (payload_json.get("source_image_ref") or "").strip()
                    or (payload_json.get("source_image_url") or "").strip()
                )
                if not source_image_ref:
                    raise ValueError("missing_source_image_url")

                source_image_url = await self._resolve_source_image_ref(source_image_ref)
                if not source_image_url:
                    raise RuntimeError(f"unresolvable_source_image_ref:{source_image_ref}")

                self._validate_http_url(source_image_url)

                strength = self._clamp_strength(payload_json.get("preservation_strength"), 0.25)

                # update request_json with i2i specifics (still "created")
                await self._provider_runs_upsert(
                    job_id=job_id,
                    provider=provider_name,
                    idempotency_key=idem_key,
                    provider_status="created",
                    request_json={
                        "studio_type": "face",
                        "mode": mode,
                        "variant_number": variant_num,
                        "seed": seed,
                        "width": width,
                        "height": height,
                        "num_inference_steps": num_steps,
                        "guidance_scale": guidance,
                        "preservation_strength": float(strength),
                        "source_image_url": source_image_url,
                    },
                    response_json={},
                    meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
                )

                tmp_src_path = f"/tmp/df_i2i_src_{uuid.uuid4().hex}.png"

                parsed = urlparse(source_image_url)
                if parsed.scheme == "file":
                    local_path = parsed.path
                    if not local_path or not os.path.exists(local_path):
                        raise ValueError(f"source_image_file_not_found:{local_path}")
                    with open(local_path, "rb") as rf, open(tmp_src_path, "wb") as wf:
                        wf.write(rf.read())
                else:
                    await _download_to_tmp(source_image_url, tmp_src_path)

                out = await router.generate_i2i_bytes(
                    prompt=prompt,
                    image_url=source_image_url,
                    negative_prompt=neg or None,
                    seed=seed,
                    width=width,
                    height=height,
                    num_inference_steps=num_steps,
                    guidance_scale=guidance,
                    preservation_strength=float(strength),
                    src_local_path=tmp_src_path,
                    mask_local_path=None,
                    provider="openai",
                )
            else:
                out = await router.generate_t2i_bytes(
                    prompt=prompt,
                    negative_prompt=neg or None,
                    seed=seed,
                    width=width,
                    height=height,
                    num_inference_steps=num_steps,
                    guidance_scale=guidance,
                    provider="openai",
                )

            # provider_runs: succeeded
            await self._provider_runs_upsert(
                job_id=job_id,
                provider=out.provider or provider_name,
                idempotency_key=idem_key,
                provider_status="succeeded",
                request_json={
                    "studio_type": "face",
                    "mode": mode,
                    "variant_number": variant_num,
                    "seed": seed,
                    "width": width,
                    "height": height,
                    "num_inference_steps": num_steps,
                    "guidance_scale": guidance,
                    "preservation_strength": float(strength) if strength is not None else None,
                },
                response_json={
                    "provider": out.provider or provider_name,
                    "content_type": out.content_type or "image/png",
                    "provider_meta": self._prune_provider_meta(out.meta),
                },
                meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
            )

            image_bytes = out.bytes
            content_type = out.content_type or "image/png"
            file_size = len(image_bytes)

            storage_path: str
            image_url: str

            if hasattr(self.storage_service, "upload_bytes") and callable(getattr(self.storage_service, "upload_bytes")):
                storage_path, image_url = await self.storage_service.upload_bytes(
                    data=image_bytes,
                    content_type=content_type,
                    user_id=user_id,
                    job_id=job_id,
                    variant=variant_num,
                )
            elif hasattr(self.storage_service, "upload_from_file") and callable(getattr(self.storage_service, "upload_from_file")):
                ext = "png" if "png" in content_type else "jpg"
                tmp_out_path = f"/tmp/df_face_out_{uuid.uuid4().hex}.{ext}"
                with open(tmp_out_path, "wb") as f:
                    f.write(image_bytes)
                storage_path, image_url = await self.storage_service.upload_from_file(
                    path=tmp_out_path,
                    content_type=content_type,
                    user_id=user_id,
                    job_id=job_id,
                    variant=variant_num,
                )
            elif hasattr(self.storage_service, "upload_local_file") and callable(getattr(self.storage_service, "upload_local_file")):
                ext = "png" if "png" in content_type else "jpg"
                tmp_out_path = f"/tmp/df_face_out_{uuid.uuid4().hex}.{ext}"
                with open(tmp_out_path, "wb") as f:
                    f.write(image_bytes)
                storage_path, image_url = await self.storage_service.upload_local_file(
                    path=tmp_out_path,
                    content_type=content_type,
                    user_id=user_id,
                    job_id=job_id,
                    variant=variant_num,
                )
            else:
                raise RuntimeError(
                    "storage_service_missing_upload_bytes_or_upload_from_file: "
                    "OpenAI returns bytes; implement storage_service.upload_bytes(...) "
                    "or storage_service.upload_from_file(...)/upload_local_file(...)."
                )

            creative_variations = self._coerce_dict(variant.get("creative_variations"))
            identity_signature = variant.get("identity_signature")

            asset_id = await self.assets_repo.create_asset(
                user_id=user_id,
                kind="face_image",
                storage_ref=image_url,
                content_type=content_type,
                size_bytes=file_size or 150000,
                meta={
                    "job_id": job_id,
                    "variant": variant_num,
                    "seed_mode": variant.get("seed_mode"),
                    "job_seed": variant.get("job_seed"),
                    "seed": seed,
                    "mode": mode,
                    "identity_signature": identity_signature,
                    "prompt": prompt[:500],
                    "technical_specs": technical,
                    "creative_variations": creative_variations,
                    "provider": out.provider,
                    "provider_meta": out.meta,
                    "storage_path": storage_path,
                    "source_image_ref": source_image_ref,
                    "source_image_url": source_image_url,
                    "preservation_strength": float(strength) if strength is not None else None,
                },
            )

            def _code(x: Any) -> Optional[str]:
                if not x:
                    return None
                if isinstance(x, dict):
                    return x.get("code")
                return getattr(x, "code", None)

            gender = self._coerce_gender(request_dict.get("gender"))

            profile_id = await self.profiles_repo.create_profile(
                user_id=user_id,
                display_name=f"Face Variant {variant_num}",
                primary_image_asset_id=asset_id,
                attributes={
                    "region_code": request_dict.get("region_code"),
                    "gender": gender,
                    "age_range_code": request_dict.get("age_range_code"),
                    "skin_tone_code": request_dict.get("skin_tone_code"),
                    "image_format_code": request_dict.get("image_format_code"),
                    "use_case_code": request_dict.get("use_case_code"),
                    "style_code": request_dict.get("style_code"),
                    "context_code": request_dict.get("context_code"),
                    "clothing_style_code": request_dict.get("clothing_style_code"),
                    "platform_code": request_dict.get("platform_code"),
                },
                meta={
                    "job_id": job_id,
                    "variant": variant_num,
                    "seed_mode": variant.get("seed_mode"),
                    "job_seed": variant.get("job_seed"),
                    "seed": seed,
                    "mode": mode,
                    "identity_signature": identity_signature,
                    "generation_prompt": prompt[:2000],
                    "negative_prompt": neg[:2000],
                    "demographic_base": variant.get("demographic_base"),
                    "creative_variations": creative_variations,
                    "technical_specs": technical,
                    "resolved": {
                        "use_case": _code(resolved_config.get("use_case")),
                        "image_format": _code(resolved_config.get("image_format")),
                        "age_range": _code(resolved_config.get("age_range")),
                        "region": _code(resolved_config.get("region")),
                        "skin_tone": _code(resolved_config.get("skin_tone")),
                    },
                    "provider": out.provider,
                    "provider_meta": out.meta,
                    "source_image_ref": source_image_ref,
                    "source_image_url": source_image_url,
                    "preservation_strength": float(strength) if strength is not None else None,
                },
            )

            await self._upsert_face_job_output(
                job_id=job_id,
                face_profile_id=profile_id,
                output_asset_id=asset_id,
                variant_number=variant_num,
                prompt_used=(variant.get("prompt_used") or prompt)[:4000],
                negative_prompt=neg[:4000],
                technical_specs=technical,
                creative_variations=creative_variations,
            )

            await self.artifacts_repo.create_artifact(
                job_id=job_id,
                kind="face_image",
                url=image_url,
                content_type=content_type,
                bytes_size=file_size,
                meta={
                    "engine": "creator",
                    "variant_number": variant_num,
                    "seed_mode": variant.get("seed_mode"),
                    "job_seed": variant.get("job_seed"),
                    "seed": seed,
                    "mode": mode,
                    "identity_signature": identity_signature,
                    "output_asset_id": asset_id,
                    "face_profile_id": profile_id,
                    "storage_path": storage_path,
                    "prompt_used": prompt[:2000],
                    "negative_prompt": neg[:2000],
                    "technical_specs": technical,
                    "creative_variations": creative_variations,
                    "provider": out.provider,
                    "provider_meta": out.meta,
                    "source_image_ref": source_image_ref,
                    "source_image_url": source_image_url,
                    "preservation_strength": float(strength) if strength is not None else None,
                },
            )

            return GeneratedVariant(
                variant_number=variant_num,
                face_profile_id=profile_id,
                media_asset_id=asset_id,
                image_url=image_url,
                prompt_used=prompt,
                technical_specs=technical,
                creative_variations=creative_variations,
            )

        except Exception as e:
            await self._provider_runs_upsert(
                job_id=job_id,
                provider=provider_name,
                idempotency_key=idem_key,
                provider_status="failed",
                request_json={
                    "studio_type": "face",
                    "mode": mode,
                    "variant_number": variant_num,
                    "seed": seed,
                    "width": width,
                    "height": height,
                    "num_inference_steps": num_steps,
                    "guidance_scale": guidance,
                    "preservation_strength": float(strength) if strength is not None else None,
                },
                response_json={"error": str(e)[:500]},
                meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
            )
            raise
        finally:
            for p in (tmp_src_path, tmp_out_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    async def _upsert_face_job_output(
        self,
        job_id: str,
        face_profile_id: str,
        output_asset_id: Optional[str],
        variant_number: int,
        prompt_used: Optional[str],
        negative_prompt: Optional[str],
        technical_specs: Dict[str, Any],
        creative_variations: Dict[str, Any],
    ) -> None:
        q = """
        INSERT INTO face_job_outputs (
          job_id, face_profile_id, output_asset_id, variant_number,
          prompt_used, negative_prompt, technical_specs, creative_variations
        )
        VALUES (
          $1::uuid, $2::uuid, $3::uuid, $4,
          $5, $6, $7::jsonb, $8::jsonb
        )
        ON CONFLICT (job_id, variant_number)
        DO UPDATE SET
          face_profile_id = EXCLUDED.face_profile_id,
          output_asset_id = EXCLUDED.output_asset_id,
          prompt_used = EXCLUDED.prompt_used,
          negative_prompt = EXCLUDED.negative_prompt,
          technical_specs = EXCLUDED.technical_specs,
          creative_variations = EXCLUDED.creative_variations
        """
        await self.jobs_repo.execute_command(
            q,
            job_id,
            face_profile_id,
            output_asset_id,
            int(variant_number),
            prompt_used,
            negative_prompt,
            self.jobs_repo.prepare_jsonb_param(technical_specs or {}),
            self.jobs_repo.prepare_jsonb_param(creative_variations or {}),
        )

    async def get_job_status(self, job_id: str) -> JobStatusResponse:
        job = await self.jobs_repo.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        variants: List[GeneratedVariant] = []

        q = """
        SELECT
          fjo.variant_number,
          fjo.face_profile_id::text as face_profile_id,
          fjo.output_asset_id::text as media_asset_id,
          coalesce(a.url, ma.storage_ref) as image_url,
          fjo.prompt_used,
          fjo.technical_specs,
          fjo.creative_variations
        FROM face_job_outputs fjo
        LEFT JOIN media_assets ma ON ma.id = fjo.output_asset_id
        LEFT JOIN LATERAL (
          SELECT url
          FROM artifacts
          WHERE job_id = fjo.job_id
            AND kind = 'face_image'
            AND (meta_json->>'variant_number')::int = fjo.variant_number
          ORDER BY created_at DESC
          LIMIT 1
        ) a ON true
        WHERE fjo.job_id = $1::uuid
        ORDER BY fjo.variant_number ASC
        """
        rows = await self.jobs_repo.execute_queries(q, job_id)

        for row in rows:
            r = self.jobs_repo.convert_db_row(row)
            tech = self._coerce_dict(r.get("technical_specs"))
            crea = self._coerce_dict(r.get("creative_variations"))

            variants.append(
                GeneratedVariant(
                    variant_number=int(r.get("variant_number") or 0),
                    face_profile_id=str(r.get("face_profile_id") or ""),
                    media_asset_id=str(r.get("media_asset_id") or ""),
                    image_url=str(r.get("image_url") or ""),
                    prompt_used=str(r.get("prompt_used") or ""),
                    technical_specs=tech,
                    creative_variations=crea,
                )
            )

        requested: Optional[int] = None
        try:
            if isinstance(job.payload_json, dict) and job.payload_json.get("num_variants") is not None:
                requested = int(job.payload_json.get("num_variants"))
        except Exception:
            requested = None

        raw_status = self._job_status_str(getattr(job, "status", "queued") or "queued")
        try:
            status_enum = JobStatus(raw_status)
        except Exception:
            status_enum = JobStatus.QUEUED

        return JobStatusResponse(
            job_id=job_id,
            status=status_enum,
            message=self._get_status_message(status_enum),
            progress=self._get_progress_info(status_enum, len(variants), requested),
            variants=variants if variants else None,
            error=getattr(job, "error_message", None),
            created_at=getattr(job, "created_at", None),
            updated_at=getattr(job, "updated_at", None),
        )

    def _get_status_message(self, status: JobStatus) -> str:
        messages = {
            JobStatus.QUEUED: "Job queued for processing",
            JobStatus.RUNNING: "Generating diverse face variants",
            JobStatus.SUCCEEDED: "Face generation completed successfully",
            JobStatus.FAILED: "Face generation failed",
            JobStatus.CANCELLED: "Job was cancelled",
        }
        return messages.get(status, "Unknown status")

    def _get_progress_info(self, status: JobStatus, variants_count: int, requested: Optional[int]) -> Optional[Dict[str, Any]]:
        if status == JobStatus.RUNNING:
            base: Dict[str, Any] = {
                "message": "Generating creator platform variants...",
                "current_step": "Image generation",
                "variants_completed": variants_count,
            }
            if requested is not None:
                base["variants_requested"] = requested
            return base

        if status == JobStatus.SUCCEEDED:
            base = {"message": f"Generated {variants_count} variants successfully", "variants_completed": variants_count}
            if requested is not None:
                base["variants_requested"] = requested
            return base

        return None