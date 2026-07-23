from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import inspect
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import (
        PricingCommitRequest,
        PricingPreviewRequest,
        PricingReleaseRequest,
        PricingReserveRequest,
    )
except Exception:
    class PricingClientError(Exception):
        pass

    @dataclass
    class PricingPreviewRequest:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        external_ref_type: str
        external_ref_id: Optional[str]
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingReserveRequest:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        quote_id: Optional[str]
        preview_fingerprint: Optional[str]
        meta: Dict[str, Any]

    @dataclass
    class PricingCommitRequest:
        user_id: str
        reservation_id: str
        actual_units: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingReleaseRequest:
        user_id: str
        reservation_id: str
        reason: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    class SvcPricingClient:
        enabled = False

        @classmethod
        def from_env(cls, service_name: str) -> "SvcPricingClient":
            return cls()

        async def preview(self, req: PricingPreviewRequest):
            raise PricingClientError("pricing client unavailable")

        async def reserve(self, req: PricingReserveRequest):
            raise PricingClientError("pricing client unavailable")

        async def commit(self, req: PricingCommitRequest):
            raise PricingClientError("pricing client unavailable")

        async def release(self, req: PricingReleaseRequest):
            raise PricingClientError("pricing client unavailable")


from ..domain.models import (
    CreatorPlatformRequest,
    GeneratedVariant,
    JobCreatedResponse,
    JobStatus,
    JobStatusResponse,
    PricingConfirmationModel,
    PricingPreviewResponseModel,
    PricingStateView,
    PricingSummaryView,
)

from ..repos.artifacts_repo import ArtifactsRepo
from ..repos.creator_config_repo import CreatorPlatformConfigRepo
from ..repos.face_jobs_repo import FaceJobsRepo
from ..repos.face_profiles_repo import FaceProfilesRepo
from ..repos.media_assets_repo import MediaAssetsRepo

from app.services.azure_storage_service import AzureStorageService
from app.services.creator_prompt_service import CreatorPromptService
from app.services.fal_client import FalClient
from app.services.idempotency_service import provider_idempotency_key
from app.services.safety_service import SafetyService
from app.services.translation_service import TranslationService

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


def _is_i2i_mode_value(value: Any) -> bool:
    return str(value or "").strip().lower().replace("_", "-") in {
        "image-to-image",
        "i2i",
        "img2img",
    }


def _merge_csv_terms(*values: Any) -> str:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        for part in raw.split(","):
            term = re.sub(r"\s+", " ", part).strip()
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return ", ".join(out)


def _build_strict_edit_face_identity_contract(request_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    DesiFaces Edit Face contract.

    Edit Face is not a face transformation feature. The source image owns identity.
    Prompt enhancement and user text may change requested non-identity areas only.
    """
    rd = dict(request_dict or {})
    if not _is_i2i_mode_value(rd.get("mode") or rd.get("generation_mode")):
        return rd

    locked_identity_features = [
        "identity",
        "same_real_person",
        "face",
        "facial_geometry",
        "facial_proportions",
        "forehead",
        "hairline",
        "visible_hair",
        "eyes",
        "eye_shape",
        "eye_spacing",
        "eyelids",
        "eyebrows",
        "nose",
        "nose_width",
        "nose_bridge",
        "lips",
        "mouth_shape",
        "cheeks",
        "cheek_shape",
        "cheek_volume",
        "cheek_fullness",
        "lower_face_width",
        "jawline",
        "jawline_definition",
        "chin",
        "chin_size",
        "chin_shape",
        "facial_fullness",
        "skin_tone",
        "natural_complexion",
        "skin_texture",
        "age_appearance",
        "gender_presentation",
        "facial_hair",
        "glasses",
        "eyewear",
        "natural_imperfections",
    ]

    allowed_i2i_changes = [
        "clothing",
        "outfit",
        "attire",
        "jewelry",
        "background",
        "environment",
        "scene",
        "lighting",
        "camera_angle",
        "framing",
        "composition",
        "color_grade",
        "style",
    ]

    forbidden_i2i_changes = [
        "different_person",
        "new_face",
        "lookalike",
        "beautified_face",
        "idealized_portrait",
        "model_like_face",
        "celebrity_like_face",
        "ai_generated_face",
        "synthetic_face",
        "waxy_face",
        "plastic_skin",
        "airbrushed_skin",
        "overly_smoothed_skin",
        "beauty_filter",
        "face",
        "face_shape",
        "facial_geometry",
        "facial_proportions",
        "forehead",
        "hairline",
        "visible_hair",
        "eyes",
        "eye_shape",
        "eye_spacing",
        "eyebrows",
        "nose",
        "lips",
        "jawline",
        "jawline_definition",
        "chin",
        "chin_size",
        "chin_shape",
        "cheeks",
        "cheek_shape",
        "cheek_volume",
        "cheek_fullness",
        "swollen_cheeks",
        "puffy_cheeks",
        "enlarged_cheeks",
        "rounded_cheeks",
        "bloated_face",
        "lower_face_width",
        "widened_lower_face",
        "skin_tone",
        "complexion",
        "skin_texture",
        "age",
        "age_group",
        "gender",
        "gender_presentation",
        "facial_hair",
        "glasses",
        "eyewear",
        "expression",
    ]

    identity_instruction = (
        "DESIFACES EDIT FACE STRICT IDENTITY LOCK: edit the input photo while preserving the exact same real person. "
        "Do not create a new portrait, lookalike, beautified version, or AI-polished face. "
        "Preserve the original face geometry and natural appearance: forehead, hairline, visible hair, eyes, eye spacing, eyelids, eyebrows, nose width and bridge, lips, mouth shape, cheeks, cheek shape, cheek volume, cheek fullness, lower-face width, jawline, jawline definition, chin size, chin shape, facial fullness, skin tone, natural complexion, skin texture, age appearance, gender presentation, facial hair, glasses/eyewear if present, and natural imperfections. "
        "The user request may change only explicitly requested non-identity areas such as outfit, clothing, jewelry, background, scene, lighting, framing, camera angle, composition, color grade, or style. "
        "If the prompt conflicts with identity preservation, ignore only the conflicting identity-change portion and preserve source identity."
    )

    negative_prompt = (
        "different person, new face, lookalike, changed identity, changed face, changed facial geometry, changed facial proportions, "
        "changed forehead, changed hairline, changed eyes, changed eye shape, changed eye spacing, changed eyebrows, changed nose, changed lips, changed mouth, "
        "changed cheeks, changed cheek shape, changed cheek volume, changed cheek fullness, swollen cheeks, puffy cheeks, enlarged cheeks, fuller cheeks, rounded cheeks, bloated face, "
        "changed lower face, widened lower face, altered lower face, changed jawline, softened jawline, changed chin, larger chin, smaller chin, rounded chin, "
        "changed skin tone, lighter complexion, darker complexion, changed complexion, changed skin texture, airbrushed skin, overly smoothed skin, plastic skin, waxy skin, "
        "beautified face, idealized portrait, model-like face, celebrity-like face, AI-generated face, synthetic face, beauty filter, glamour retouch, studio retouch, "
        "removed glasses, changed glasses, missing eyewear, changed facial hair, changed age, younger face, older face, changed gender presentation"
    )

    rd["identity_lock"] = True
    rd["identity_lock_level"] = "strict"
    rd["preserve_source_identity"] = True
    rd["preserve_source_gender"] = True
    rd["gender_lock_mode"] = "preserve_from_source"
    rd["locked_identity_features"] = locked_identity_features
    rd["allowed_i2i_changes"] = allowed_i2i_changes
    rd["request_only_editable_attributes"] = allowed_i2i_changes
    rd["forbidden_i2i_changes"] = forbidden_i2i_changes
    rd["identity_lock_instructions"] = identity_instruction
    rd["strict_identity_instruction"] = identity_instruction
    rd["strict_i2i_edit_instruction"] = (
        "REQUEST-ONLY EDITING RULE: modify only the non-identity attributes the user explicitly asks to change. "
        "Do not infer changes to cheeks, chin, jawline, skin tone, glasses, facial hair, age, gender, or facial structure."
    )
    rd["negative_prompt"] = _merge_csv_terms(
        rd.get("negative_prompt"),
        rd.get("negativePrompt"),
        negative_prompt,
    )
    rd["system_prompt"] = " ".join(
        part for part in [
            identity_instruction,
            str(rd.get("system_prompt") or "").strip(),
        ] if part
    )
    return rd



def _notifications_base_url() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_URL")
        or os.getenv("DF_CORE_URL")
        or os.getenv("SVC_CORE_URL")
        or ""
    ).strip().rstrip("/")


def _notifications_internal_events_url() -> str:
    base = _notifications_base_url()
    if not base:
        return ""
    if base.endswith("/api/internal/notifications/events"):
        return base
    if base.endswith("/api"):
        return f"{base}/internal/notifications/events"
    return f"{base}/api/internal/notifications/events"


def _notifications_bearer() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or os.getenv("DF_PRICING_INTERNAL_BEARER")
        or ""
    ).strip()


async def _emit_notification_best_effort(payload: Dict[str, Any], *, context: Dict[str, Any]) -> None:
    url = _notifications_internal_events_url()
    token = _notifications_bearer()
    if not url or not token:
        return

    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def _send() -> None:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    try:
        await asyncio.to_thread(_send)
    except Exception:
        logger.exception("face_notification_emit_failed", extra=context)


def _patch_pricing_client_service_headers(client: Any, *, service_name: str = "svc-face"):
    if client is None:
        return client

    if getattr(client, "_df_service_header_patched", False):
        return client

    headers_fn = getattr(client, "_headers", None)
    if not callable(headers_fn):
        setattr(client, "_df_service_header_patched", True)
        return client

    def _headers_with_service_name(*args, **kwargs):
        headers = headers_fn(*args, **kwargs)
        if not isinstance(headers, dict):
            headers = dict(headers or {})
        else:
            headers = dict(headers)
        headers["X-Service-Name"] = service_name
        return headers

    setattr(client, "_headers", _headers_with_service_name)
    setattr(client, "_df_service_header_patched", True)
    return client


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
        self.pricing_client = _patch_pricing_client_service_headers(
            SvcPricingClient.from_env(service_name="svc-face"),
            service_name="svc-face",
        )

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
            return ""
        if hasattr(g, "value"):
            return str(g.value or "").strip()
        if isinstance(g, dict) and "value" in g:
            return str(g.get("value") or "").strip()
        return str(g).strip()

    @staticmethod
    def _coerce_dict(v: Any) -> Dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                vv = json.loads(v)
                return vv if isinstance(vv, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _coerce_int(v: Any, default: int = 0) -> int:
        try:
            return int(float(v))
        except Exception:
            return default

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

    @staticmethod
    def _normalize_aspect_ratio(v: Any) -> str:
        s = str(v or "").strip().lower()
        if s in {"16:9", "landscape", "horizontal"}:
            return "16:9"
        if s in {"1:1", "square"}:
            return "1:1"
        return "9:16"

    @classmethod
    def _default_image_size_hint_for_ratio(cls, aspect_ratio: Any) -> str:
        ar = cls._normalize_aspect_ratio(aspect_ratio)
        if ar == "1:1":
            return "1024x1024"
        if ar == "16:9":
            return "1536x1024"
        return "1024x1536"

    @classmethod
    def _normalize_image_size_hint(cls, aspect_ratio: Any, size_hint: Any) -> str:
        s = str(size_hint or "").strip().lower()
        normalized = {
            "1024x1024": "1024x1024",
            "square": "1024x1024",
            "1024": "1024x1024",
            "1024x1536": "1024x1536",
            "portrait": "1024x1536",
            "vertical": "1024x1536",
            "1536x1024": "1536x1024",
            "landscape": "1536x1024",
            "horizontal": "1536x1024",
            "auto": cls._default_image_size_hint_for_ratio(aspect_ratio),
        }
        return normalized.get(s, cls._default_image_size_hint_for_ratio(aspect_ratio))

    @classmethod
    def _size_hint_to_dimensions(cls, aspect_ratio: Any, size_hint: Any) -> Tuple[int, int, str]:
        ar = cls._normalize_aspect_ratio(aspect_ratio)
        hint = cls._normalize_image_size_hint(ar, size_hint)

        compatibility = {
            "1:1": {"1024x1024"},
            "16:9": {"1536x1024"},
            "9:16": {"1024x1536"},
        }
        if hint not in compatibility.get(ar, {"1024x1536"}):
            hint = cls._default_image_size_hint_for_ratio(ar)

        dims = {
            "1024x1024": (1024, 1024),
            "1024x1536": (1024, 1536),
            "1536x1024": (1536, 1024),
        }
        width, height = dims[hint]
        return width, height, hint

    @classmethod
    def _normalize_request_framing(cls, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        rd = dict(request_dict or {})

        if not (rd.get("use_case_code") or "").strip() and (rd.get("use_case") or "").strip():
            rd["use_case_code"] = str(rd.get("use_case") or "").strip()

        if not (rd.get("shot_type_code") or "").strip() and (rd.get("shot_type") or "").strip():
            rd["shot_type_code"] = str(rd.get("shot_type") or "").strip()

        aspect_ratio = cls._normalize_aspect_ratio(rd.get("aspect_ratio"))
        width, height, image_size_hint = cls._size_hint_to_dimensions(
            aspect_ratio,
            rd.get("image_size_hint") or rd.get("size"),
        )

        rd["aspect_ratio"] = aspect_ratio
        rd["image_size_hint"] = image_size_hint
        rd["size"] = image_size_hint
        rd["width"] = int(width)
        rd["height"] = int(height)

        return rd

    @staticmethod
    def _row_get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        if hasattr(obj, key):
            return getattr(obj, key, default)
        try:
            return obj[key]
        except Exception:
            return default

    @staticmethod
    def _string_or_none(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @staticmethod
    def _pricing_resp_get(resp: Any, key: str, default: Any = None) -> Any:
        if resp is None:
            return default
        if isinstance(resp, dict):
            value = resp.get(key, default)
        else:
            value = getattr(resp, key, default)
        if hasattr(value, "value"):
            try:
                return value.value
            except Exception:
                return default if value is None else value
        return value

    @staticmethod
    def _normalize_settlement_mode(v: Any) -> str:
        s = str(v or "").strip().lower()
        if s in {"postpaid", "invoice", "bill", "billed"}:
            return "postpaid"
        if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
            return "prepaid"
        if s in {"hybrid", "mixed"}:
            return "hybrid"
        return s

    def _canonicalize_pricing_entitlement(
        self,
        pricing: Optional[Dict[str, Any]],
        *,
        resp: Any = None,
    ) -> Dict[str, Any]:
        out = dict(pricing or {})

        billing_account_id = self._string_or_none(
            self._pricing_resp_get(resp, "billing_account_id") if resp is not None else None
        ) or self._string_or_none(out.get("billing_account_id"))
        settlement_mode = self._normalize_settlement_mode(
            self._pricing_resp_get(resp, "settlement_mode") if resp is not None else out.get("settlement_mode")
        ) or self._normalize_settlement_mode(out.get("settlement_mode"))
        billing_mode = self._string_or_none(
            self._pricing_resp_get(resp, "billing_mode") if resp is not None else None
        ) or self._string_or_none(out.get("billing_mode"))
        pricing_mode = self._string_or_none(
            self._pricing_resp_get(resp, "pricing_mode") if resp is not None else None
        ) or self._string_or_none(out.get("pricing_mode"))

        explicit_tier = self._clean_text(
            self._pricing_resp_get(resp, "tier_code") if resp is not None else out.get("tier_code")
        )
        explicit_source = self._clean_text(
            self._pricing_resp_get(resp, "entitlement_source") if resp is not None else out.get("entitlement_source")
        )
        explicit_reason = self._clean_text(
            self._pricing_resp_get(resp, "entitlement_reason") if resp is not None else out.get("entitlement_reason")
        )

        weak_tier = bool(billing_account_id and explicit_tier.lower() == "free")
        weak_source = bool(billing_account_id and explicit_source.lower() == "module_gate_fallback")

        if billing_account_id:
            out["billing_account_id"] = billing_account_id
        if settlement_mode:
            out["settlement_mode"] = settlement_mode
        if billing_mode:
            out["billing_mode"] = billing_mode
        if pricing_mode:
            out["pricing_mode"] = pricing_mode

        if explicit_tier and not weak_tier:
            out["tier_code"] = explicit_tier
        elif billing_account_id and settlement_mode == "postpaid":
            out["tier_code"] = "enterprise"
        elif billing_account_id and settlement_mode == "hybrid":
            out["tier_code"] = "business"
        elif explicit_tier:
            out["tier_code"] = explicit_tier

        if explicit_source and not weak_source:
            out["entitlement_source"] = explicit_source
        elif billing_account_id and settlement_mode == "postpaid":
            out["entitlement_source"] = "credit_account"
        elif billing_account_id:
            out["entitlement_source"] = "billing_account"
        elif explicit_source:
            out["entitlement_source"] = explicit_source

        if explicit_reason:
            out["entitlement_reason"] = explicit_reason
        elif billing_account_id and (weak_tier or weak_source):
            out["entitlement_reason"] = "billing_account_context_override"
        elif billing_account_id and not self._clean_text(out.get("entitlement_reason")):
            out["entitlement_reason"] = "billing_account_context_fallback"

        return out

    @classmethod
    def _stable_source_url_for_hash(cls, url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        try:
            p = urlparse(u)
            if p.scheme in ("http", "https") and p.netloc:
                qs = parse_qs(p.query or "")
                sas_keys = {"sig", "se", "sp", "sv", "sr", "st"}
                if ("blob.core.windows.net" in (p.netloc or "")) and (sas_keys & set(qs.keys())):
                    return f"{p.scheme}://{p.netloc}{p.path}"
        except Exception:
            pass
        return u

    @staticmethod
    def _decimal_or_zero(v: Any) -> Decimal:
        try:
            if v is None or str(v).strip() == "":
                return Decimal("0")
            return Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    @classmethod
    def _money_str(cls, v: Any, default: str = "0.00") -> str:
        try:
            d = cls._decimal_or_zero(v).quantize(Decimal("0.01"))
            return format(d, "f")
        except Exception:
            return default

    @classmethod
    def _units_str(cls, v: Any, default: str = "0") -> str:
        try:
            d = cls._decimal_or_zero(v)
            if d == d.to_integral():
                return str(int(d))
            return format(d.normalize(), "f")
        except Exception:
            return default

    @staticmethod
    def _clean_text(v: Any) -> str:
        return str(v or "").strip()

    async def _get_media_asset_row(self, asset_id: str) -> Optional[Dict[str, Any]]:
        try:
            q = """
            SELECT id::text as id, storage_ref, meta_json
            FROM public.media_assets
            WHERE id = $1::uuid
            LIMIT 1
            """
            rows = await self.jobs_repo.execute_queries(q, asset_id)
            if not rows:
                return None
            r0 = self.jobs_repo.convert_db_row(rows[0])
            return {
                "id": str(r0.get("id") or ""),
                "storage_ref": str(r0.get("storage_ref") or ""),
                "meta_json": self._coerce_dict(r0.get("meta_json")),
            }
        except Exception:
            return None

    async def _refresh_read_sas_best_effort(self, storage_ref: str, meta_json: Dict[str, Any]) -> str:
        try:
            fn = getattr(self.storage_service, "get_readonly_sas_url", None)
            if callable(fn):
                refreshed = await fn(
                    storage_ref=storage_ref or None,
                    meta_json=meta_json if meta_json else None,
                    hours=24,
                    refresh_if_within_minutes=60,
                )
                if refreshed:
                    return str(refreshed).strip()
        except Exception:
            pass
        return storage_ref

    def _validate_remote_http_url(self, url: str) -> None:
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
            storage_ref = None
            meta_json: Dict[str, Any] = {}

            if hasattr(self.assets_repo, "get_asset") and callable(getattr(self.assets_repo, "get_asset")):
                try:
                    ma = await self.assets_repo.get_asset(raw)  # type: ignore[attr-defined]
                    if ma:
                        storage_ref = ma.get("storage_ref") if isinstance(ma, dict) else getattr(ma, "storage_ref", None)
                        mj = ma.get("meta_json") if isinstance(ma, dict) else getattr(ma, "meta_json", None)
                        meta_json = self._coerce_dict(mj)
                except Exception:
                    storage_ref = None

            if not storage_ref:
                row = await self._get_media_asset_row(raw)
                if row:
                    storage_ref = row.get("storage_ref") or None
                    meta_json = self._coerce_dict(row.get("meta_json"))

            if storage_ref:
                storage_ref = await self._refresh_read_sas_best_effort(str(storage_ref), meta_json)
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
    # Pricing helpers
    # -------------------------
    def _build_initial_pricing_block(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        mode = self._coerce_mode(request_dict.get("mode"))
        requested_variants = max(
            1,
            self._coerce_int(request_dict.get("num_variants"), 0),
            self._coerce_int(request_dict.get("variant_count"), 0),
            self._coerce_int(request_dict.get("num_outputs"), 0),
        )

        aspect_ratio = self._normalize_aspect_ratio(request_dict.get("aspect_ratio"))
        _, _, image_size_hint = self._size_hint_to_dimensions(
            aspect_ratio,
            request_dict.get("image_size_hint") or request_dict.get("size"),
        )

        is_i2i = mode == "image-to-image"
        sku_code = (
            os.getenv("DF_PRICING_SKU_FACE_I2I", "face.creator.generate.i2i")
            if is_i2i
            else os.getenv("DF_PRICING_SKU_FACE_T2I", "face.creator.generate.t2i")
        )

        state = "disabled"
        if self.pricing_client.enabled:
            state = "pending_reservation"

        return {
            "enabled": bool(self.pricing_client.enabled),
            "state": state,
            "service_name": "svc-face",
            "service_action": f"face.creator.generate.{'i2i' if is_i2i else 't2i'}",
            "sku_code": sku_code,
            "variant_code": "FACE_I2I" if is_i2i else "FACE_T2I",
            "estimated_units": str(requested_variants),
            "unit_type": "image",
            "mode": mode,
            "quote_id": None,
            "quote_expires_at": None,
            "preview_fingerprint": None,
            "reservation_id": None,
            "reservation_status": None,
            "quote_idempotency_key": None,
            "quote_breakdown": {},
            "summary": {},
            "reserved_units": None,
            "actual_units": None,
            "billed_units": None,
            "released_units": None,
            "estimated_amount": None,
            "final_amount": None,
            "amount": None,
            "currency": None,
            "ledger_entry_id": None,
            "billing_mode": None,
            "billing_account_id": None,
            "settlement_mode": None,
            "pricing_mode": None,
            "entitlement_source": None,
            "entitlement_reason": None,
            "tier_code": None,
            "client_presented_amount": None,
            "client_presented_currency": None,
            "user_confirmed": None,
            "meta": {
                "requested_variants": requested_variants,
                "mode": mode,
                "image_format_code": request_dict.get("image_format_code"),
                "use_case_code": request_dict.get("use_case_code"),
                "shot_type_code": request_dict.get("shot_type_code"),
                "aspect_ratio": aspect_ratio,
                "image_size_hint": image_size_hint,
                "width": request_dict.get("width"),
                "height": request_dict.get("height"),
                "platform_code": request_dict.get("platform_code"),
                "provider_hint": "openai",
            },
        }

    @staticmethod
    def _merge_pricing_block(current: Optional[Dict[str, Any]], **updates: Any) -> Dict[str, Any]:
        out = dict(current or {})
        for key, value in updates.items():
            if value is not None:
                out[key] = value
        return out

    def _pricing_from_job(self, job: Any, payload_json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = (
            payload_json
            if isinstance(payload_json, dict)
            else self._coerce_dict(self._row_get(job, "payload_json", None))
        )
        meta = self._coerce_dict(self._row_get(job, "meta_json", None))

        pricing = self._coerce_dict(payload.get("pricing"))
        if pricing:
            return self._canonicalize_pricing_entitlement(pricing)

        pricing = self._coerce_dict(meta.get("pricing"))
        if pricing:
            return self._canonicalize_pricing_entitlement(pricing)

        return {}

    async def _persist_pricing_block(self, job_id: str, pricing: Dict[str, Any]) -> None:
        pricing = self._canonicalize_pricing_entitlement(pricing)
        q = """
        UPDATE public.studio_jobs
        SET
          payload_json = COALESCE(payload_json, '{}'::jsonb) || jsonb_build_object('pricing', $2::jsonb),
          meta_json = COALESCE(meta_json, '{}'::jsonb)
                      || jsonb_build_object(
                           'pricing', $2::jsonb,
                           'pricing_state', COALESCE($3::text, ''),
                           'pricing_enabled', $4::bool,
                           'pricing_billing_mode', NULLIF($5::text, ''),
                           'pricing_settlement_mode', NULLIF($6::text, ''),
                           'pricing_billing_account_id', NULLIF($7::text, '')
                         ),
          updated_at = now()
        WHERE id = $1::uuid
        """
        try:
            await self.jobs_repo.execute_command(
                q,
                job_id,
                self.jobs_repo.prepare_jsonb_param(pricing or {}),
                str(pricing.get("state") or ""),
                bool(pricing.get("enabled", False)),
                str(pricing.get("billing_mode") or ""),
                str(pricing.get("settlement_mode") or ""),
                str(pricing.get("billing_account_id") or ""),
            )
        except Exception:
            logger.exception("Failed to persist pricing block", extra={"job_id": job_id})

    async def _load_latest_job_and_pricing(
        self,
        job_id: str,
    ) -> Tuple[Optional[Any], Dict[str, Any], Dict[str, Any]]:
        latest_job = await self.jobs_repo.get_job(job_id)
        if not latest_job:
            return None, {}, {}
        latest_payload = self._coerce_dict(self._row_get(latest_job, "payload_json", None))
        latest_pricing = self._pricing_from_job(latest_job, latest_payload)
        return latest_job, latest_payload, latest_pricing

    async def _await_reserved_pricing(
        self,
        job_id: str,
        *,
        max_wait_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> Dict[str, Any]:
        if not self.pricing_client.enabled:
            return {}

        deadline = asyncio.get_running_loop().time() + max_wait_s
        last_pricing: Dict[str, Any] = {}

        while True:
            _, _, pricing = await self._load_latest_job_and_pricing(job_id)
            last_pricing = pricing or {}

            if not last_pricing.get("enabled"):
                return last_pricing

            state = str(last_pricing.get("state") or "").strip().lower()
            reservation_id = str(last_pricing.get("reservation_id") or "").strip()

            if state in {"reserved", "committed", "released", "reservation_failed", "commit_failed", "release_failed"}:
                return last_pricing

            if reservation_id and state in {"pending_reservation", ""}:
                out = dict(last_pricing)
                out["state"] = "reserved"
                out["reservation_status"] = out.get("reservation_status") or "reserved"
                return out

            if asyncio.get_running_loop().time() >= deadline:
                return last_pricing

            await asyncio.sleep(poll_s)

    async def _reserve_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not pricing.get("enabled"):
            return pricing

        quote_id = self._string_or_none(pricing.get("quote_id"))
        if self._is_local_preview_quote_id(quote_id):
            quote_id = None

        preview_fingerprint = self._string_or_none(pricing.get("preview_fingerprint"))
        if self._is_local_preview_fingerprint(preview_fingerprint):
            preview_fingerprint = None

        req = PricingReserveRequest(
            user_id=str(user_id),
            service_name="svc-face",
            service_action=str(pricing.get("service_action") or "face.creator.generate.t2i"),
            sku_code=str(pricing.get("sku_code") or "face.creator.generate.t2i"),
            units=str(pricing.get("estimated_units") or "1"),
            external_ref_type="studio_job",
            external_ref_id=str(job_id),
            idempotency_key=f"svc-face:job:{job_id}:reserve",
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
            meta=self._coerce_dict(pricing.get("meta")),
        )

        try:
            resp = await self._call_pricing_client(
                "reserve",
                req,
                timeout_s=self._pricing_rpc_timeout_s(),
            )
            reserve_status = str(self._pricing_resp_get(resp, "status", "reserved") or "reserved")

            reserve_amount = self._pricing_resp_get(resp, "amount")
            quote_id = self._pricing_resp_get(resp, "quote_id") or pricing.get("quote_id")
            preview_fingerprint = self._pricing_resp_get(resp, "preview_fingerprint") or pricing.get(
                "preview_fingerprint"
            )
            billing_mode = self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode")
            resp_reserved_units = self._pricing_resp_get(resp, "reserved_units")

            normalized_reserved_units = resp_reserved_units
            if str(billing_mode or "").strip().lower() == "bill" and str(resp_reserved_units or "").strip() in {"", "0"}:
                normalized_reserved_units = None

            pricing = self._merge_pricing_block(
                pricing,
                enabled=True,
                state="reserved",
                reservation_id=self._pricing_resp_get(resp, "reservation_id"),
                quote_id=quote_id,
                preview_fingerprint=preview_fingerprint,
                variant_code=self._pricing_resp_get(resp, "variant_code") or pricing.get("variant_code"),
                reserved_units=normalized_reserved_units,
                reservation_status=reserve_status,
                estimated_amount=reserve_amount or pricing.get("estimated_amount"),
                amount=reserve_amount or pricing.get("amount"),
                currency=self._pricing_resp_get(resp, "currency") or pricing.get("currency"),
                billing_mode=billing_mode,
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode"),
                pricing_mode=self._pricing_resp_get(resp, "pricing_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source"),
                entitlement_reason=self._pricing_resp_get(resp, "entitlement_reason"),
                tier_code=self._pricing_resp_get(resp, "tier_code"),
            )
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)
            await self._persist_pricing_block(job_id, pricing)
            return pricing
        except Exception as e:
            logger.exception(
                "Pricing reserve failed",
                extra={"job_id": job_id, "user_id": user_id},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="reservation_failed",
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            if isinstance(e, PricingClientError):
                raise
            raise PricingClientError(str(e)) from e

    async def _commit_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        actual_units: int,
    ) -> Dict[str, Any]:
        if not pricing.get("enabled"):
            return pricing

        _, _, latest_pricing = await self._load_latest_job_and_pricing(job_id)
        if latest_pricing:
            pricing = latest_pricing

        reservation_id = str(pricing.get("reservation_id") or "").strip()
        state = str(pricing.get("state") or "").strip().lower()

        if (not reservation_id) or (state not in {"reserved", "commit_failed"}):
            awaited = await self._await_reserved_pricing(job_id)
            if awaited:
                pricing = awaited
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                state = str(pricing.get("state") or "").strip().lower()

        if not reservation_id:
            pricing = self._merge_pricing_block(
                pricing,
                state="commit_failed",
                actual_units=str(max(1, int(actual_units))),
                error="missing_reservation_id_at_commit",
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

        if state not in {"reserved", "commit_failed"}:
            return pricing

        try:
            resp = await self._call_pricing_client(
                "commit",
                PricingCommitRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    actual_units=str(max(1, int(actual_units))),
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-face:job:{job_id}:commit",
                    meta={
                        "sku_code": pricing.get("sku_code"),
                        "service_action": pricing.get("service_action"),
                        "requested_units": pricing.get("estimated_units"),
                    },
                ),
                timeout_s=self._pricing_rpc_timeout_s(),
            )
            commit_status = str(self._pricing_resp_get(resp, "status", "committed") or "committed")
            committed_amount = self._pricing_resp_get(resp, "amount")
            pricing = self._merge_pricing_block(
                pricing,
                enabled=True,
                state="committed",
                quote_id=self._pricing_resp_get(resp, "quote_id") or pricing.get("quote_id"),
                variant_code=self._pricing_resp_get(resp, "variant_code") or pricing.get("variant_code"),
                actual_units=str(max(1, int(actual_units))),
                commit_status=commit_status,
                reservation_status=commit_status,
                ledger_entry_id=self._pricing_resp_get(resp, "ledger_entry_id"),
                billed_units=self._pricing_resp_get(resp, "billed_units") or str(max(1, int(actual_units))),
                final_amount=committed_amount or pricing.get("final_amount") or pricing.get("amount"),
                amount=committed_amount or pricing.get("amount"),
                currency=self._pricing_resp_get(resp, "currency") or pricing.get("currency"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
                pricing_mode=self._pricing_resp_get(resp, "pricing_mode") or pricing.get("pricing_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
                entitlement_reason=self._pricing_resp_get(resp, "entitlement_reason") or pricing.get("entitlement_reason"),
                tier_code=self._pricing_resp_get(resp, "tier_code") or pricing.get("tier_code"),
            )
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)
            await self._persist_pricing_block(job_id, pricing)
            return pricing
        except Exception as e:
            logger.exception(
                "Pricing commit failed",
                extra={"job_id": job_id, "reservation_id": reservation_id, "user_id": user_id},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="commit_failed",
                actual_units=str(max(1, int(actual_units))),
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

    async def _release_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        if not pricing.get("enabled"):
            return pricing

        _, _, latest_pricing = await self._load_latest_job_and_pricing(job_id)
        if latest_pricing:
            pricing = latest_pricing

        reservation_id = str(pricing.get("reservation_id") or "").strip()
        state = str(pricing.get("state") or "").strip().lower()

        if (not reservation_id) or (state not in {"reserved", "release_failed"}):
            awaited = await self._await_reserved_pricing(job_id)
            if awaited:
                pricing = awaited
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                state = str(pricing.get("state") or "").strip().lower()

        if not reservation_id:
            return pricing

        if state not in {"reserved", "release_failed"}:
            return pricing

        try:
            resp = await self._call_pricing_client(
                "release",
                PricingReleaseRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    reason=reason,
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-face:job:{job_id}:release",
                    meta={
                        "sku_code": pricing.get("sku_code"),
                        "service_action": pricing.get("service_action"),
                    },
                ),
                timeout_s=self._pricing_rpc_timeout_s(),
            )
            release_status = str(self._pricing_resp_get(resp, "status", "released") or "released")
            pricing = self._merge_pricing_block(
                pricing,
                enabled=bool(pricing.get("enabled", True)),
                state="released",
                quote_id=self._pricing_resp_get(resp, "quote_id") or pricing.get("quote_id"),
                variant_code=self._pricing_resp_get(resp, "variant_code") or pricing.get("variant_code"),
                release_status=release_status,
                reservation_status=release_status,
                released_units=self._pricing_resp_get(resp, "released_units"),
                final_amount=pricing.get("final_amount") or "0.00",
                billing_mode=self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
                pricing_mode=self._pricing_resp_get(resp, "pricing_mode") or pricing.get("pricing_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
                entitlement_reason=self._pricing_resp_get(resp, "entitlement_reason") or pricing.get("entitlement_reason"),
                tier_code=self._pricing_resp_get(resp, "tier_code") or pricing.get("tier_code"),
            )
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)
            await self._persist_pricing_block(job_id, pricing)
            return pricing
        except Exception as e:
            logger.exception(
                "Pricing release failed",
                extra={"job_id": job_id, "user_id": user_id, "reason": reason},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="release_failed",
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

    async def _fail_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        error_code: str,
        error_message: str,
        release_reason: str,
    ) -> None:
        try:
            await self._release_pricing_for_job(
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
                reason=release_reason,
            )
        finally:
            await self.jobs_repo.update_status(
                job_id,
                "failed",
                error_code=error_code,
                error_message=error_message,
            )
            await _emit_notification_best_effort(
                {
                    "event_type": "FACE_FAILED",
                    "category": "jobs",
                    "priority": "important",
                    "source_service": "svc-face",
                    "source_ref_type": "job",
                    "source_ref_id": str(job_id),
                    "actor_user_id": None,
                    "title": "Your Face job needs attention",
                    "body": error_message or "Your desifaces.ai Face generation failed.",
                    "action_route": "/notifications",
                    "action_label": "Review issue",
                    "image_url": None,
                    "payload_json": {"job_id": str(job_id), "error_code": error_code},
                    "metadata_json": {"job_id": str(job_id), "error_code": error_code},
                    "dedupe_key": f"face-failed:{job_id}:{error_code}",
                    "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": True, "email": True}}],
                },
                context={"job_id": str(job_id), "user_id": str(user_id), "event_type": "FACE_FAILED", "error_code": error_code},
            )

    # -------------------------
    # Config normalization
    # -------------------------
    async def _ensure_required_config_codes(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        rd = request_dict

        if not (rd.get("image_format_code") or "").strip():
            use_case_code = (rd.get("use_case_code") or "").strip()
            picked: Optional[str] = None

            if use_case_code:
                try:
                    uc = await self.creator_config_repo.get_use_case_by_code(use_case_code)
                    rec = (
                        uc.get("recommended_formats")
                        if isinstance(uc, dict)
                        else getattr(uc, "recommended_formats", None)
                    )
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

        # Intentionally DO NOT auto-default age_range_code or skin_tone_code.
        return rd

    # -------------------------
    # Provider runs
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
            job_id,
            provider,
            idempotency_key,
            provider_status,
            request_json,
            response_json,
            meta_json,
            created_at,
            updated_at
        )
        VALUES (
            $1::uuid,
            $2::text,
            $3::text,
            $4::text,
            $5::jsonb,
            $6::jsonb,
            $7::jsonb,
            now(),
            now()
        )
        ON CONFLICT (idempotency_key)
        DO UPDATE SET
            job_id = EXCLUDED.job_id,
            provider = EXCLUDED.provider,
            provider_status = EXCLUDED.provider_status,
            request_json = EXCLUDED.request_json,
            response_json = EXCLUDED.response_json,
            meta_json = EXCLUDED.meta_json,
            updated_at = now()
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
            logger.exception(
                "provider_runs upsert failed",
                extra={
                    "job_id": job_id,
                    "provider": provider,
                    "idempotency_key": idempotency_key,
                    "provider_status": provider_status,
                },
            )
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

    def _build_identity_profile(
        self,
        *,
        job_seed: int,
        request_hash: str,
        request_dict: Dict[str, Any],
        variant_number: int = 1,
        variant_seed: Optional[int] = None,
    ) -> Dict[str, str]:
        variant_scope = f"{request_hash}|v={int(variant_number)}|vs={int(variant_seed or 0)}"

        signature = hashlib.sha256(
            f"{self.ID_CONTEXT}|{job_seed}|{variant_scope}".encode("utf-8")
        ).hexdigest()[:12]

        face = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="face_shape",
            options=self.ID_FACE_SHAPES,
        )
        jaw = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="jawline",
            options=self.ID_JAWLINES,
        )
        nose = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="nose",
            options=self.ID_NOSES,
        )
        eyes = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="eyes",
            options=self.ID_EYES,
        )
        spacing = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="eye_spacing",
            options=self.ID_EYE_SPACING,
        )
        proportions = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="proportions",
            options=self.ID_FACE_PROPORTIONS,
        )

        cheek = (
            self._id_pick(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="cheekbones",
                options=self.ID_CHEEKBONES,
            )
            if self._id_bool(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="use_cheekbones",
                true_pct=70,
            )
            else ""
        )
        brows = (
            self._id_pick(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="brows",
                options=self.ID_EYEBROWS,
            )
            if self._id_bool(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="use_brows",
                true_pct=60,
            )
            else ""
        )
        lips = (
            self._id_pick(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="lips",
                options=self.ID_LIPS,
            )
            if self._id_bool(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="use_lips",
                true_pct=60,
            )
            else ""
        )
        chin = (
            self._id_pick(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="chin",
                options=self.ID_CHINS,
            )
            if self._id_bool(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="use_chin",
                true_pct=55,
            )
            else ""
        )

        expression = (
            self._id_pick(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="expression",
                options=self.ID_EXPRESSIONS,
            )
            if self._id_bool(
                job_seed=job_seed,
                request_hash=variant_scope,
                key="use_expression",
                true_pct=65,
            )
            else "neutral expression"
        )

        mark = self._id_pick(
            job_seed=job_seed,
            request_hash=variant_scope,
            key="marks",
            options=self.ID_MARKS,
        )

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

        return {
            "signature": signature,
            "tokens": tokens,
            "negative_tokens": self.ID_NEG_DEFAULT,
        }

    # -------------------------
    # Pricing preview / summary helpers
    # -------------------------
    def _pricing_to_view(self, pricing: Dict[str, Any]) -> Optional[PricingStateView]:
        if not pricing:
            return None

        pricing = self._canonicalize_pricing_entitlement(pricing)

        final_amount = pricing.get("final_amount")
        if final_amount in (None, "") and str(pricing.get("state") or "").lower() == "committed":
            final_amount = pricing.get("amount")

        estimated_amount = pricing.get("estimated_amount")
        if estimated_amount in (None, "") and str(pricing.get("state") or "").lower() in {"reserved", "committed"}:
            estimated_amount = pricing.get("amount")

        reserved_units = pricing.get("reserved_units")
        if str(pricing.get("billing_mode") or "").strip().lower() == "bill" and str(reserved_units or "").strip() == "0":
            reserved_units = None

        return PricingStateView(
            state=str(pricing.get("state") or ""),
            enabled=bool(pricing.get("enabled", False)),
            quote_id=self._string_or_none(pricing.get("quote_id")),
            quote_expires_at=self._string_or_none(pricing.get("quote_expires_at")),
            preview_fingerprint=self._string_or_none(pricing.get("preview_fingerprint")),
            reservation_id=self._string_or_none(pricing.get("reservation_id")),
            variant_code=self._string_or_none(pricing.get("variant_code")),
            service_name=self._string_or_none(pricing.get("service_name")),
            service_action=self._string_or_none(pricing.get("service_action")),
            sku_code=self._string_or_none(pricing.get("sku_code")),
            unit_type=self._string_or_none(pricing.get("unit_type")),
            estimated_units=self._string_or_none(pricing.get("estimated_units")),
            reserved_units=self._string_or_none(reserved_units),
            actual_units=self._string_or_none(pricing.get("actual_units")),
            billed_units=self._string_or_none(pricing.get("billed_units")),
            released_units=self._string_or_none(pricing.get("released_units")),
            estimated_amount=self._string_or_none(estimated_amount),
            final_amount=self._string_or_none(final_amount),
            amount=self._string_or_none(pricing.get("amount")),
            currency=self._string_or_none(pricing.get("currency")),
            ledger_entry_id=self._string_or_none(pricing.get("ledger_entry_id")),
            billing_mode=self._string_or_none(pricing.get("billing_mode")),
            billing_account_id=self._string_or_none(pricing.get("billing_account_id")),
            settlement_mode=self._string_or_none(pricing.get("settlement_mode")),
            pricing_mode=self._string_or_none(pricing.get("pricing_mode")),
            entitlement_source=self._string_or_none(pricing.get("entitlement_source")),
            entitlement_reason=self._string_or_none(pricing.get("entitlement_reason")),
            tier_code=self._string_or_none(pricing.get("tier_code")),
            source=self._string_or_none(pricing.get("source")),
            reason=self._string_or_none(pricing.get("reason")),
            summary=self._coerce_dict(pricing.get("summary")),
            meta=self._coerce_dict(pricing.get("meta")),
        )

    def _pricing_summary_view(self, pricing: Dict[str, Any]) -> Optional[PricingSummaryView]:
        if not pricing:
            return None

        state = self._clean_text(pricing.get("state")).lower()
        currency = self._clean_text(pricing.get("currency"))

        estimated_amount = self._money_str(
            pricing.get("estimated_amount")
            if pricing.get("estimated_amount") not in (None, "")
            else pricing.get("amount")
        )

        def _fmt(amount_text: Optional[str]) -> Optional[str]:
            if amount_text in (None, ""):
                return None
            return f"{currency} {amount_text}" if currency else amount_text

        if state == "committed":
            final_amount = self._money_str(
                pricing.get("final_amount")
                if pricing.get("final_amount") not in (None, "")
                else pricing.get("amount")
            )
            est_dec = self._decimal_or_zero(estimated_amount)
            fin_dec = self._decimal_or_zero(final_amount)
            delta = fin_dec - est_dec
            delta_text = format(delta.quantize(Decimal("0.01")), "f")

            return PricingSummaryView(
                display_estimate=_fmt(estimated_amount),
                display_final=_fmt(final_amount),
                display_delta=_fmt(delta_text),
                display_note="Final charge recorded after execution.",
            )

        if state == "released":
            final_amount = "0.00"
            est_dec = self._decimal_or_zero(estimated_amount)
            fin_dec = self._decimal_or_zero(final_amount)
            delta = fin_dec - est_dec
            delta_text = format(delta.quantize(Decimal("0.01")), "f")

            return PricingSummaryView(
                display_estimate=_fmt(estimated_amount),
                display_final=_fmt(final_amount),
                display_delta=_fmt(delta_text),
                display_note="No charge because the reservation was released.",
            )

        if state == "quoted":
            return PricingSummaryView(
                display_estimate=_fmt(estimated_amount),
                display_final=_fmt(estimated_amount),
                display_delta=_fmt("0.00"),
                display_note="Estimated price before execution.",
            )

        if state in {"reserved", "pending_reservation"}:
            return PricingSummaryView(
                display_estimate=_fmt(estimated_amount),
                display_final=_fmt(estimated_amount),
                display_delta=_fmt("0.00"),
                display_note="Estimated charge reserved before execution.",
            )

        if state == "disabled":
            return PricingSummaryView(
                display_estimate=_fmt(estimated_amount),
                display_final=None,
                display_delta=None,
                display_note="Pricing is disabled for this request.",
            )

        return PricingSummaryView(
            display_estimate=_fmt(estimated_amount),
            display_final=None,
            display_delta=None,
            display_note="Pricing is being processed.",
        )

    async def _prepare_creator_request_dict(
        self,
        request: CreatorPlatformRequest,
    ) -> Tuple[JsonDict, Dict[str, Any], str]:
        request_dict: JsonDict = request.model_dump(mode="json")
        request_dict = self._normalize_request_framing(request_dict)

        if not (request_dict.get("user_prompt") or "").strip():
            p = str(request_dict.get("prompt") or "").strip()
            if p:
                request_dict["user_prompt"] = p

        mode = self._coerce_mode(request_dict.get("mode"))
        request_dict["mode"] = mode

        if mode == "image-to-image":
            asset_ref = (request_dict.get("source_image_asset_id") or "").strip()
            url_ref = (request_dict.get("source_image_url") or "").strip()
            ref = asset_ref or url_ref
            if not ref:
                raise ValueError("missing_required_fields: ['source_image_url'] for image-to-image mode")

            resolved = await self._resolve_source_image_ref(ref)
            request_dict["source_image_ref"] = ref
            request_dict["source_image_url"] = resolved

            if asset_ref:
                request_dict["source_image_asset_id"] = asset_ref
            elif self._UUID_RE.match(ref) and not request_dict.get("source_image_asset_id"):
                request_dict["source_image_asset_id"] = ref

            request_dict = _build_strict_edit_face_identity_contract(request_dict)

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
        request_dict = self._normalize_request_framing(request_dict)
        return request_dict, translation_meta, mode

    async def _prepare_pricing_preview_request_dict(
        self,
        request: CreatorPlatformRequest,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Prepare only the deterministic fields required to calculate a Face price.

        Pricing does not depend on prompt safety classification or translated
        prompt text. Full safety validation and translation remain enforced by
        process_job() in the Face worker before variant generation.
        """
        if hasattr(request, "model_dump"):
            request_dict = request.model_dump(
                mode="json",
                exclude_none=True,
            )
        elif isinstance(request, dict):
            request_dict = dict(request)
        else:
            request_dict = dict(getattr(request, "__dict__", {}) or {})

        mode = self._coerce_mode(request_dict.get("mode"))
        request_dict["mode"] = mode

        user_prompt = self._clean_text(
            request_dict.get("user_prompt")
            or request_dict.get("prompt")
        )
        if user_prompt:
            request_dict["user_prompt"] = user_prompt
            request_dict["prompt"] = user_prompt

        # Pricing only needs to know that I2I was requested. Do not resolve or
        # download the source image while calculating a quote.
        if mode == "image-to-image":
            asset_ref = self._clean_text(
                request_dict.get("source_image_asset_id")
                or request_dict.get("source_asset_id")
            )
            url_ref = self._clean_text(
                request_dict.get("source_image_url")
            )
            source_ref = asset_ref or url_ref

            if not source_ref:
                raise ValueError(
                    "missing_required_fields: ['source_image_url'] "
                    "for image-to-image mode"
                )

            request_dict["source_image_ref"] = source_ref

            if asset_ref:
                request_dict["source_image_asset_id"] = asset_ref
            else:
                request_dict["source_image_url"] = url_ref

        request_dict = await self._ensure_required_config_codes(
            request_dict
        )
        request_dict = self._normalize_request_framing(
            request_dict
        )

        return request_dict, mode

    async def _prepare_creator_submission_request_dict(
        self,
        request: CreatorPlatformRequest,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Prepare the deterministic submission payload only.

        This intentionally reuses the proven pricing-preview preparation path so
        POST /creator/generate can create, reserve, and queue the job without
        running remote prompt safety or translation work in the API process.
        Full prompt preparation remains enforced by process_job() before variant
        generation.
        """
        return await self._prepare_pricing_preview_request_dict(request)


    def _pricing_rpc_timeout_s(self) -> float:
        try:
            return max(3.0, min(60.0, float(os.getenv("DF_FACE_PRICING_RPC_TIMEOUT_S", "15"))))
        except Exception:
            return 15.0

    async def _call_pricing_client(self, method_name: str, req: Any, *, timeout_s: float) -> Any:
        method = getattr(self.pricing_client, method_name, None)
        if not callable(method):
            raise PricingClientError(f"pricing_client_missing_method:{method_name}")

        def _invoke():
            result = method(req)
            if inspect.isawaitable(result):
                return asyncio.run(result)
            return result

        return await asyncio.wait_for(
            asyncio.to_thread(_invoke),
            timeout=timeout_s,
        )

    def _pricing_preview_timeout_s(self) -> float:
        try:
            return max(2.0, min(30.0, float(os.getenv("DF_FACE_PRICING_PREVIEW_TIMEOUT_S", "8"))))
        except Exception:
            return 8.0

    @staticmethod
    def _is_local_preview_quote_id(value: Any) -> bool:
        return str(value or "").strip().startswith("qt_local_")

    @staticmethod
    def _is_local_preview_fingerprint(value: Any) -> bool:
        return str(value or "").strip().startswith("local_preview_")

    def _build_local_preview_response(
        self,
        *,
        pricing: Dict[str, Any],
        preview_ref: str,
        mode: str,
        reason: str,
    ) -> PricingPreviewResponseModel:
        quote_id = f"qt_local_{preview_ref[:24]}"
        preview_fingerprint = "local_preview_" + hashlib.sha256(
            f"{preview_ref}|{mode}|{reason}".encode("utf-8")
        ).hexdigest()[:40]

        summary = {
            "display_estimate": None,
            "display_final": None,
            "display_delta": None,
            "display_note": (
                "Live pricing preview is temporarily unavailable. "
                "You can continue, and the final reservation will be calculated during Create Face."
            ),
        }

        pricing = self._merge_pricing_block(
            pricing,
            enabled=True,
            state="quoted",
            quote_id=quote_id,
            quote_expires_at=None,
            preview_fingerprint=preview_fingerprint,
            estimated_amount=None,
            amount=None,
            currency=None,
            source="local_fallback",
            reason=reason,
            summary=summary,
        )
        pricing = self._canonicalize_pricing_entitlement(pricing)

        pricing_view = self._pricing_to_view(pricing) or PricingStateView(
            state="quoted",
            enabled=True,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
            source="local_fallback",
            reason=reason,
        )

        return PricingPreviewResponseModel(
            studio="face",
            action="generate",
            quote_id=quote_id,
            quote_expires_at=None,
            preview_fingerprint=preview_fingerprint,
            pricing=pricing_view,
            balance={
                "before_credits": None,
                "after_estimated_credits": None,
                "before_money": None,
                "after_estimated_money": None,
            },
            summary=summary,
        )

    async def preview_pricing(
        self,
        *,
        user_id: str,
        request: CreatorPlatformRequest,
        client_context: Optional[Dict[str, Any]] = None,
    ) -> PricingPreviewResponseModel:
        request_dict, mode = await self._prepare_pricing_preview_request_dict(request)

        pricing = self._build_initial_pricing_block(request_dict)
        if not pricing.get("enabled"):
            preview_payload = {
                "service_name": pricing.get("service_name"),
                "service_action": pricing.get("service_action"),
                "sku_code": pricing.get("sku_code"),
                "estimated_units": pricing.get("estimated_units"),
                "mode": mode,
                "request": {
                    "image_format_code": request_dict.get("image_format_code"),
                    "use_case_code": request_dict.get("use_case_code"),
                    "platform_code": request_dict.get("platform_code"),
                },
            }
            preview_fingerprint = hashlib.sha256(self._stable_json(preview_payload).encode("utf-8")).hexdigest()
            quote_id = f"qt_{preview_fingerprint[:24]}"
            pricing = self._merge_pricing_block(
                pricing,
                enabled=False,
                state="quoted",
                quote_id=quote_id,
                preview_fingerprint=preview_fingerprint,
                quote_expires_at=None,
                estimated_amount="0.00",
                currency="USD",
                summary={
                    "display_estimate": "USD 0.00",
                    "display_final": "USD 0.00",
                    "display_delta": "USD 0.00",
                    "display_note": "Pricing is currently disabled.",
                },
            )
            return PricingPreviewResponseModel(
                studio="face",
                action="generate",
                quote_id=quote_id,
                quote_expires_at=None,
                preview_fingerprint=preview_fingerprint,
                pricing=self._pricing_to_view(pricing) or PricingStateView(state="quoted"),
                balance={
                    "before_credits": None,
                    "after_estimated_credits": None,
                    "before_money": None,
                    "after_estimated_money": None,
                },
                summary=self._coerce_dict(pricing.get("summary")),
            )

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
            "shot_type_code": request_dict.get("shot_type_code"),
            "aspect_ratio": request_dict.get("aspect_ratio"),
            "image_size_hint": request_dict.get("image_size_hint"),
            "width": request_dict.get("width"),
            "height": request_dict.get("height"),
            "style_code": request_dict.get("style_code"),
            "context_code": request_dict.get("context_code"),
            "clothing_style_code": request_dict.get("clothing_style_code"),
            "platform_code": request_dict.get("platform_code"),
            "mode": mode,
        }

        if mode == "image-to-image":
            if (request_dict.get("source_image_asset_id") or "").strip():
                request_hash_payload["source_image_asset_id"] = request_dict.get("source_image_asset_id")
            else:
                request_hash_payload["source_image_url"] = self._stable_source_url_for_hash(
                    str(request_dict.get("source_image_url") or "")
                )
            request_hash_payload["preservation_strength"] = request_dict.get("preservation_strength")

        preview_ref = self._generate_request_hash(request_hash_payload)

        resolved_client_context = client_context or {}
        resolved_channel = self._clean_text(resolved_client_context.get("channel")) or "mobile"
        resolved_country_code = self._clean_text(resolved_client_context.get("country_code"))
        resolved_currency = self._clean_text(resolved_client_context.get("currency")) or "USD"

        preview_meta = {
            **self._coerce_dict(pricing.get("meta")),
            "channel": resolved_channel,
            "country_code": resolved_country_code,
            "currency": resolved_currency,
            "client_context": resolved_client_context,
            "request_hash": preview_ref,
            "mode": mode,
            "shot_type_code": request_dict.get("shot_type_code"),
            "aspect_ratio": request_dict.get("aspect_ratio"),
            "image_size_hint": request_dict.get("image_size_hint"),
            "width": request_dict.get("width"),
            "height": request_dict.get("height"),
        }

        try:
            resp = await self._call_pricing_client(
                "preview",
                PricingPreviewRequest(
                    user_id=str(user_id),
                    service_name=str(pricing.get("service_name") or "svc-face"),
                    service_action=str(pricing.get("service_action") or ""),
                    sku_code=str(pricing.get("sku_code") or ""),
                    units=str(pricing.get("estimated_units") or "1"),
                    external_ref_type="studio_job_preview",
                    external_ref_id=preview_ref,
                    idempotency_key=f"svc-face:preview:{preview_ref}",
                    meta=preview_meta,
                ),
                timeout_s=self._pricing_preview_timeout_s(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "face_preview_pricing_timeout user_id=%s preview_ref=%s mode=%s",
                user_id,
                preview_ref,
                mode,
            )
            return self._build_local_preview_response(
                pricing=pricing,
                preview_ref=preview_ref,
                mode=mode,
                reason="pricing_preview_timeout",
            )
        except PricingClientError as e:
            logger.warning(
                "face_preview_pricing_client_error user_id=%s preview_ref=%s mode=%s err=%s",
                user_id,
                preview_ref,
                mode,
                str(e),
            )
            return self._build_local_preview_response(
                pricing=pricing,
                preview_ref=preview_ref,
                mode=mode,
                reason="pricing_preview_unavailable",
            )
        except Exception:
            logger.exception(
                "face_preview_pricing_unexpected_error user_id=%s preview_ref=%s mode=%s",
                user_id,
                preview_ref,
                mode,
            )
            return self._build_local_preview_response(
                pricing=pricing,
                preview_ref=preview_ref,
                mode=mode,
                reason="pricing_preview_error",
            )

        quote_id = self._clean_text(self._pricing_resp_get(resp, "quote_id")) or f"qt_{preview_ref}"
        preview_fingerprint = self._clean_text(self._pricing_resp_get(resp, "preview_fingerprint")) or hashlib.sha256(
            f"{quote_id}|{preview_ref}".encode("utf-8")
        ).hexdigest()

        quote_breakdown = self._coerce_dict(self._pricing_resp_get(resp, "quote_breakdown"))
        summary = self._coerce_dict(self._pricing_resp_get(resp, "summary"))

        pricing = self._merge_pricing_block(
            pricing,
            enabled=True,
            state=str(self._pricing_resp_get(resp, "status", "quoted") or "quoted"),
            quote_id=quote_id,
            quote_expires_at=self._pricing_resp_get(resp, "quote_expires_at"),
            preview_fingerprint=preview_fingerprint,
            estimated_amount=self._pricing_resp_get(resp, "estimated_amount"),
            amount=self._pricing_resp_get(resp, "estimated_amount"),
            currency=self._pricing_resp_get(resp, "currency"),
            billing_mode=self._pricing_resp_get(resp, "billing_mode"),
            billing_account_id=self._pricing_resp_get(resp, "billing_account_id"),
            settlement_mode=self._pricing_resp_get(resp, "settlement_mode"),
            pricing_mode=self._pricing_resp_get(resp, "pricing_mode"),
            entitlement_source=self._pricing_resp_get(resp, "entitlement_source"),
            entitlement_reason=self._pricing_resp_get(resp, "entitlement_reason"),
            tier_code=self._pricing_resp_get(resp, "tier_code"),
            quote_breakdown=quote_breakdown,
            summary=summary,
        )
        pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)

        if not summary:
            summary_view = self._pricing_summary_view(pricing)
            if summary_view is not None:
                summary = summary_view.model_dump(exclude_none=True)
                pricing["summary"] = summary

        pricing_view = self._pricing_to_view(pricing) or PricingStateView(
            state="quoted",
            enabled=bool(pricing.get("enabled", False)),
        )

        return PricingPreviewResponseModel(
            studio="face",
            action="generate",
            quote_id=quote_id,
            quote_expires_at=self._string_or_none(self._pricing_resp_get(resp, "quote_expires_at")),
            preview_fingerprint=preview_fingerprint,
            pricing=pricing_view,
            balance={
                "before_credits": self._pricing_resp_get(resp, "before_credits"),
                "after_estimated_credits": self._pricing_resp_get(resp, "after_estimated_credits"),
                "before_money": self._pricing_resp_get(resp, "before_money"),
                "after_estimated_money": self._pricing_resp_get(resp, "after_estimated_money"),
            },
            summary=summary,
        )

    def _face_variant_concurrency(self) -> int:
        try:
            return max(1, min(8, int(os.getenv("DF_FACE_VARIANT_CONCURRENCY", "3"))))
        except Exception:
            return 3

    async def _prepare_shared_variant_inputs(self, request_dict: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
        if mode != "image-to-image":
            return {}

        source_image_ref = (
            (request_dict.get("source_image_ref") or "").strip()
            or (request_dict.get("source_image_asset_id") or "").strip()
            or (request_dict.get("source_image_url") or "").strip()
        )
        if not source_image_ref:
            return {}

        source_image_url = await self._resolve_source_image_ref(source_image_ref)
        preservation_strength = self._clamp_strength(request_dict.get("preservation_strength"), 0.25)

        out: Dict[str, Any] = {
            "source_image_ref": source_image_ref,
            "source_image_url": source_image_url,
            "preservation_strength": preservation_strength,
        }

        parsed = urlparse(source_image_url)
        if parsed.scheme in ("http", "https"):
            self._validate_remote_http_url(source_image_url)
        elif parsed.scheme != "file":
            raise ValueError(f"unsupported_source_image_scheme:{parsed.scheme or 'missing'}")

        tmp_src_path = f"/tmp/df_face_shared_src_{uuid4().hex}.png"
        if parsed.scheme == "file":
            local_path = parsed.path
            if not local_path or not os.path.exists(local_path):
                raise ValueError(f"source_image_file_not_found:{local_path}")
            with open(local_path, "rb") as rf, open(tmp_src_path, "wb") as wf:
                wf.write(rf.read())
        else:
            import httpx
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, trust_env=False) as client:
                r = await client.get(source_image_url)
                r.raise_for_status()
                with open(tmp_src_path, "wb") as f:
                    f.write(r.content)

        out["tmp_src_path"] = tmp_src_path
        return out

    async def _mark_variant_running(self, job_id: str, variant_number: int, seed: int, *, variants_requested: int) -> None:
        await self.jobs_repo.update_variant_state(
            job_id,
            variant_number,
            {
                "status": "running",
                "seed": int(seed),
                "started_at": asyncio.get_running_loop().time(),
            },
            variants_requested=variants_requested,
        )

    async def _mark_variant_succeeded(
        self,
        job_id: str,
        variant_number: int,
        *,
        image_url: str,
        media_asset_id: str,
        face_profile_id: str,
        variants_requested: int,
    ) -> None:
        await self.jobs_repo.update_variant_state(
            job_id,
            variant_number,
            {
                "status": "succeeded",
                "image_url": image_url,
                "media_asset_id": media_asset_id,
                "face_profile_id": face_profile_id,
            },
            variants_requested=variants_requested,
        )

    async def _mark_variant_failed(
        self,
        job_id: str,
        variant_number: int,
        error_message: str,
        *,
        variants_requested: int,
    ) -> None:
        await self.jobs_repo.update_variant_state(
            job_id,
            variant_number,
            {
                "status": "failed",
                "error_message": str(error_message or "variant_failed")[:500],
            },
            variants_requested=variants_requested,
        )

    async def _count_completed_variants(self, job_id: str) -> int:
        q = "SELECT COUNT(*)::int AS n FROM face_job_outputs WHERE job_id = $1::uuid"
        rows = await self.jobs_repo.execute_queries(q, job_id)
        if not rows:
            return 0
        row = self.jobs_repo.convert_db_row(rows[0])
        return int(row.get("n") or 0)

    async def _load_variants_state(self, job_id: str) -> Dict[str, Any]:
        job = await self.jobs_repo.get_job(job_id)
        if not job:
            return {}
        meta = self._coerce_dict(self._row_get(job, "meta_json", None))
        variants_state = self._coerce_dict(meta.get("variants_state"))
        return variants_state if isinstance(variants_state, dict) else {}

    async def _run_variant_task(
        self,
        *,
        sem: asyncio.Semaphore,
        job_id: str,
        user_id: str,
        request_dict: Dict[str, Any],
        resolved_config: Dict[str, Any],
        variant: Dict[str, Any],
        mode: str,
        shared_inputs: Dict[str, Any],
        variants_requested: int,
    ) -> Optional[GeneratedVariant]:
        variant_number = int(variant.get("variant_number") or 1)
        seed = int(variant.get("seed") or 0)
        async with sem:
            await self._mark_variant_running(
                job_id,
                variant_number,
                seed,
                variants_requested=variants_requested,
            )
            try:
                return await self._process_variant(
                    job_id=job_id,
                    user_id=user_id,
                    request_dict=request_dict,
                    resolved_config=resolved_config,
                    variant=variant,
                    mode=mode,
                    shared_inputs=shared_inputs,
                    variants_requested=variants_requested,
                )
            except Exception as e:
                await self._mark_variant_failed(
                    job_id,
                    variant_number,
                    str(e),
                    variants_requested=variants_requested,
                )
                logger.exception(
                    "Variant failed",
                    extra={"job_id": job_id, "variant": variant_number, "error": str(e)},
                )
                return None

    async def get_job_status_light(self, job_id: str) -> Dict[str, Any]:
        job = await self.jobs_repo.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        meta = self._coerce_dict(self._row_get(job, "meta_json", None))
        payload = self._coerce_dict(self._row_get(job, "payload_json", None))
        pricing = self._pricing_from_job(job, payload)
        variants_state = self._coerce_dict(meta.get("variants_state"))

        pricing_view = self._pricing_to_view(pricing)
        return {
            "job_id": job_id,
            "status": self._job_status_str(self._row_get(job, "status", "queued")),
            "variants_requested": int(meta.get("variants_requested") or payload.get("num_variants") or 1),
            "variants_completed": int(meta.get("variants_completed") or 0),
            "variants_failed": int(meta.get("variants_failed") or 0),
            "variants_running": int(meta.get("variants_running") or 0),
            "variants": [
                {
                    "variant_number": int(k),
                    "status": str(self._coerce_dict(v).get("status") or "queued"),
                    "image_url": self._string_or_none(self._coerce_dict(v).get("image_url")),
                    "media_asset_id": self._string_or_none(self._coerce_dict(v).get("media_asset_id")),
                    "face_profile_id": self._string_or_none(self._coerce_dict(v).get("face_profile_id")),
                    "error_message": self._string_or_none(self._coerce_dict(v).get("error_message")),
                }
                for k, v in sorted(variants_state.items(), key=lambda item: int(item[0]))
            ],
            "updated_at": self._row_get(job, "updated_at", None),
            "error_code": self._row_get(job, "error_code", None),
            "error_message": self._row_get(job, "error_message", None),
            "pricing": pricing_view.model_dump(exclude_none=True) if pricing_view else None,
        }

    async def recover_job(self, job_id: str) -> None:
        job = await self.jobs_repo.get_job(job_id)
        if not job:
            return
        status = self._job_status_str(self._row_get(job, "status", None))
        if status in {"succeeded", "failed", "cancelled"}:
            return
        await self.jobs_repo.update_status(
            job_id,
            "running",
            meta_patch={"recovered_at": str(asyncio.get_running_loop().time())},
        )
        await self.process_job(job_id)

    async def recover_stale_running_jobs_once(self, *, limit: int = 5, stale_seconds: int = 120) -> int:
        job_ids = await self.jobs_repo.claim_stale_running_jobs(
            studio_type="face",
            limit=limit,
            stale_seconds=stale_seconds,
        )
        if not job_ids:
            return 0
        await asyncio.gather(*(self.recover_job(job_id) for job_id in job_ids), return_exceptions=True)
        return len(job_ids)

    # -------------------------
    # Public API
    # -------------------------
    async def create_job(
        self,
        user_id: str,
        request: CreatorPlatformRequest,
        pricing_confirmation: Optional[PricingConfirmationModel] = None,
    ) -> JobCreatedResponse:
        logger.info(
            "Creating creator platform job",
            extra={
                "user_id": user_id,
                "image_format": getattr(request, "image_format_code", None),
                "use_case": getattr(request, "use_case_code", None),
                "variants": getattr(request, "num_variants", None),
            },
        )

        request_dict, mode = await self._prepare_creator_submission_request_dict(request)

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
            "shot_type_code": request_dict.get("shot_type_code"),
            "aspect_ratio": request_dict.get("aspect_ratio"),
            "image_size_hint": request_dict.get("image_size_hint"),
            "width": request_dict.get("width"),
            "height": request_dict.get("height"),
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
            if (request_dict.get("source_image_asset_id") or "").strip():
                request_hash_payload["source_image_asset_id"] = request_dict.get("source_image_asset_id")
            else:
                request_hash_payload["source_image_url"] = self._stable_source_url_for_hash(
                    str(request_dict.get("source_image_url") or "")
                )
            request_hash_payload["preservation_strength"] = request_dict.get("preservation_strength")

        request_hash = self._generate_request_hash(request_hash_payload)

        seed_mode, job_seed = self._resolve_seed_mode_and_job_seed(
            request_dict=request_dict,
            request_hash_payload=request_hash_payload,
        )

        request_dict["seed_mode"] = seed_mode
        request_dict["job_seed"] = int(job_seed)
        request_dict["mode"] = mode

        pricing = self._build_initial_pricing_block(request_dict)

        if pricing.get("enabled"):
            if not pricing_confirmation:
                raise ValueError("missing_pricing_confirmation")
            if not bool(pricing_confirmation.user_confirmed):
                raise ValueError("pricing_confirmation_not_confirmed")
            if not self._clean_text(pricing_confirmation.quote_id):
                raise ValueError("missing_quote_id")

            pricing = self._merge_pricing_block(
                pricing,
                quote_id=self._clean_text(pricing_confirmation.quote_id),
                preview_fingerprint=self._clean_text(pricing_confirmation.preview_fingerprint),
                client_presented_amount=self._clean_text(pricing_confirmation.client_presented_amount),
                client_presented_currency=self._clean_text(pricing_confirmation.client_presented_currency),
                user_confirmed=bool(pricing_confirmation.user_confirmed),
            )

        request_dict["pricing"] = pricing

        explicit_demographics = bool(
            (request_dict.get("age_range_code") or "").strip()
            or (request_dict.get("region_code") or "").strip()
            or (request_dict.get("skin_tone_code") or "").strip()
            or self._coerce_gender(request_dict.get("gender"))
        )

        job_id = await self.jobs_repo.create_job(
            user_id=user_id,
            studio_type="face",
            request_hash=request_hash,
            payload=request_dict,
            meta={
                "request_type": "creator_platform",
                "api_version": "v2",
                "language": request_dict.get("language") or "en",
                "safety_validated": False,
                "translation_success": True if not request_dict.get("user_prompt") else None,
                "config_validated": True,
                "seed_mode": seed_mode,
                "job_seed": int(job_seed),
                "request_nonce": request_dict.get("request_nonce"),
                "mode": mode,
                "shot_type_code": request_dict.get("shot_type_code"),
                "aspect_ratio": request_dict.get("aspect_ratio"),
                "image_size_hint": request_dict.get("image_size_hint"),
                "width": request_dict.get("width"),
                "height": request_dict.get("height"),
                "source_image_ref": request_dict.get("source_image_ref") if mode == "image-to-image" else None,
                "source_image_asset_id": request_dict.get("source_image_asset_id") if mode == "image-to-image" else None,
                "source_image_url": request_dict.get("source_image_url") if mode == "image-to-image" else None,
                "preservation_strength": request_dict.get("preservation_strength") if mode == "image-to-image" else None,
                "pricing": pricing,
                "pricing_state": pricing.get("state"),
                "pricing_enabled": bool(pricing.get("enabled")),
                "pricing_billing_mode": pricing.get("billing_mode"),
                "pricing_settlement_mode": pricing.get("settlement_mode"),
                "pricing_billing_account_id": pricing.get("billing_account_id"),
                "demographics_fixed": explicit_demographics,
            },
        )

        await self._persist_pricing_block(job_id, pricing)

        if self.pricing_client.enabled:
            try:
                pricing = await self._reserve_pricing_for_job(
                    job_id=job_id,
                    user_id=str(user_id),
                    pricing=pricing,
                )
            except PricingClientError as e:
                await self.jobs_repo.update_status(
                    job_id,
                    "failed",
                    error_code="PRICING_RESERVATION_FAILED",
                    error_message=str(e),
                )
                raise

        await _emit_notification_best_effort(
            {
                "event_type": "FACE_JOB_SUBMITTED",
                "category": "jobs",
                "priority": "info",
                "source_service": "svc-face",
                "source_ref_type": "job",
                "source_ref_id": str(job_id),
                "actor_user_id": None,
                "title": "Face generation started",
                "body": "Your desifaces.ai Face job has been queued.",
                "action_route": "/notifications",
                "action_label": "View job",
                "image_url": None,
                "payload_json": {"job_id": str(job_id), "mode": mode},
                "metadata_json": {"job_id": str(job_id), "mode": mode},
                "dedupe_key": f"face-submitted:{job_id}",
                "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": False, "email": False}}],
            },
            context={"job_id": str(job_id), "user_id": str(user_id), "event_type": "FACE_JOB_SUBMITTED"},
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
                "demographics_fixed": explicit_demographics,
                "creativity_varied": True,
                "mode": mode,
                "shot_type_code": request_dict.get("shot_type_code"),
                "aspect_ratio": request_dict.get("aspect_ratio"),
                "image_size_hint": request_dict.get("image_size_hint"),
                "pricing_state": pricing.get("state"),
                "pricing_enabled": bool(pricing.get("enabled")),
            },
            pricing=self._pricing_to_view(pricing),
        )

    async def process_job(self, job_id: str) -> None:
        logger.info("Processing creator platform job", extra={"job_id": job_id})

        user_id = ""
        pricing: Dict[str, Any] = {}
        shared_inputs: Dict[str, Any] = {}

        try:
            job = await self.jobs_repo.get_job(job_id)
            if not job:
                logger.error("Job not found", extra={"job_id": job_id})
                return

            status = self._job_status_str(self._row_get(job, "status", None))

            if status == "queued":
                try:
                    await self.jobs_repo.update_status(job_id, "running")
                except Exception:
                    pass
                status = "running"

            if status != "running":
                logger.info("Job not processable", extra={"job_id": job_id, "status": status})
                return

            payload_json = self._coerce_dict(self._row_get(job, "payload_json", None))
            if not isinstance(payload_json, dict) or not payload_json:
                await self.jobs_repo.update_status(
                    job_id,
                    "failed",
                    error_code="INVALID_PAYLOAD",
                    error_message="Job payload is not a dict",
                )
                return

            payload_json = await self._ensure_required_config_codes(payload_json)
            payload_json = self._normalize_request_framing(payload_json)

            user_id = str(self._row_get(job, "user_id", "") or "")
            pricing = self._pricing_from_job(job, payload_json)

            if self.pricing_client.enabled:
                latest_pricing = await self._await_reserved_pricing(job_id)
                if latest_pricing:
                    pricing = latest_pricing

                pricing_state = str(pricing.get("state") or "").strip().lower()
                reservation_id = str(pricing.get("reservation_id") or "").strip()

                if pricing.get("enabled") and pricing_state == "reservation_failed":
                    await self.jobs_repo.update_status(
                        job_id,
                        "failed",
                        error_code="PRICING_RESERVATION_FAILED",
                        error_message=str(pricing.get("error") or "Pricing reservation failed"),
                    )
                    return

                if pricing.get("enabled") and not reservation_id:
                    await self.jobs_repo.update_status(
                        job_id,
                        "failed",
                        error_code="PRICING_NOT_RESERVED",
                        error_message="Pricing reservation did not complete before job execution",
                    )
                    return

            job_seed: Optional[int] = None
            seed_mode: Optional[str] = None

            mode = self._coerce_mode(payload_json.get("mode"))
            meta_json = self._coerce_dict(self._row_get(job, "meta_json", None))
            if meta_json:
                job_seed = meta_json.get("job_seed")
                seed_mode = meta_json.get("seed_mode")
                mode = self._coerce_mode(meta_json.get("mode") or mode)

            if job_seed is None:
                job_seed = self._stable_seed_from(payload_json)
            if not seed_mode:
                seed_mode = "deterministic"

            request_hash = str(self._row_get(job, "request_hash", "") or "")

            user_prompt = self._clean_text(
                payload_json.get("user_prompt")
                or payload_json.get("prompt")
            )
            prompt_already_prepared = bool(
                self._clean_text(payload_json.get("user_prompt_translated_en"))
                and bool(meta_json.get("safety_validated"))
            )

            if user_prompt and not prompt_already_prepared:
                try:
                    translation_meta = await self.prompt_service.translate_and_validate(
                        user_prompt=user_prompt,
                        language=payload_json.get("language") or "en",
                    )
                except ValueError as e:
                    error_text = str(e)
                    unsafe_prompt = error_text.startswith("unsafe_prompt")
                    await self._fail_job(
                        job_id=job_id,
                        user_id=user_id,
                        pricing=pricing,
                        error_code="UNSAFE_PROMPT" if unsafe_prompt else "PROMPT_PREPARATION_FAILED",
                        error_message=error_text,
                        release_reason="unsafe_prompt" if unsafe_prompt else "prompt_preparation_failed",
                    )
                    return

                payload_json["translated_prompt"] = (
                    translation_meta.get("user_prompt_translated_en")
                    or user_prompt
                )
                payload_json.update(translation_meta)
                await self.jobs_repo.patch_job_meta(
                    job_id,
                    {
                        "safety_validated": True,
                        "translation_success": bool(
                            translation_meta.get("translation_success", True)
                        ),
                    },
                )

            source_image_url = (payload_json.get("source_image_url") or "").strip()
            if mode == "text-to-image" and source_image_url:
                mode = "image-to-image"

            if mode == "image-to-image" and not source_image_url:
                ref = (
                    (payload_json.get("source_image_ref") or "").strip()
                    or (payload_json.get("source_image_asset_id") or "").strip()
                )
                if ref:
                    try:
                        resolved = await self._resolve_source_image_ref(ref)
                        payload_json["source_image_url"] = resolved
                        payload_json["source_image_ref"] = ref
                        if self._UUID_RE.match(ref) and not payload_json.get("source_image_asset_id"):
                            payload_json["source_image_asset_id"] = ref
                    except Exception as e:
                        await self._fail_job(
                            job_id=job_id,
                            user_id=user_id,
                            pricing=pricing,
                            error_code="INVALID_SOURCE_IMAGE",
                            error_message=f"invalid_source_image_ref:{ref} err={e!s}",
                            release_reason="invalid_source_image",
                        )
                        return
                else:
                    await self._fail_job(
                        job_id=job_id,
                        user_id=user_id,
                        pricing=pricing,
                        error_code="MISSING_SOURCE_IMAGE",
                        error_message="image-to-image mode requires source_image_url or source_image_asset_id",
                        release_reason="missing_source_image",
                    )
                    return

            if mode == "image-to-image":
                payload_json = _build_strict_edit_face_identity_contract(payload_json)

            variants, resolved = await self.prompt_service.build_variants(
                request_dict=payload_json,
                job_seed=int(job_seed),
            )
            variants_requested = max(1, len(variants))
            await self.jobs_repo.patch_job_meta(job_id, {"variants_requested": variants_requested})

            aspect_ratio = self._normalize_aspect_ratio(payload_json.get("aspect_ratio"))
            width, height, image_size_hint = self._size_hint_to_dimensions(
                aspect_ratio,
                payload_json.get("image_size_hint") or payload_json.get("size"),
            )
            payload_json["aspect_ratio"] = aspect_ratio
            payload_json["image_size_hint"] = image_size_hint
            payload_json["size"] = image_size_hint
            payload_json["width"] = int(width)
            payload_json["height"] = int(height)

            for v in variants:
                vn = int(v.get("variant_number") or 1)
                v["seed_mode"] = seed_mode
                v["job_seed"] = int(job_seed)
                v["mode"] = mode
                v["request_hash"] = request_hash
                v["shot_type_code"] = payload_json.get("shot_type_code")
                v["aspect_ratio"] = aspect_ratio
                v["image_size_hint"] = image_size_hint
                tech = self._coerce_dict(v.get("technical_specs"))
                tech["width"] = int(width)
                tech["height"] = int(height)
                tech["aspect_ratio"] = aspect_ratio
                tech["image_size_hint"] = image_size_hint
                v["technical_specs"] = tech
                v["seed"] = self._derive_variant_seed_hmac(
                    job_seed=int(job_seed),
                    variant_number=vn,
                    purpose="face:gen",
                    request_hash=request_hash,
                )

            if mode == "text-to-image":
                for v in variants:
                    vn = int(v.get("variant_number") or 1)
                    vseed = int(v.get("seed") or 0)

                    ident = self._build_identity_profile(
                        job_seed=int(job_seed),
                        request_hash=request_hash,
                        request_dict=payload_json,
                        variant_number=vn,
                        variant_seed=vseed,
                    )

                    v["identity_signature"] = ident.get("signature")

                    p = (v.get("prompt") or "").strip()
                    n = (v.get("negative_prompt") or "").strip()
                    v["prompt"] = f"{p}, {ident['tokens']}" if p else ident["tokens"]
                    v["negative_prompt"] = (
                        f"{n}, {ident['negative_tokens']}" if n else ident["negative_tokens"]
                    )

            shared_inputs = await self._prepare_shared_variant_inputs(payload_json, mode=mode)

            variants_state = await self._load_variants_state(job_id)
            completed_variant_numbers = {
                int(k)
                for k, vv in variants_state.items()
                if str(self._coerce_dict(vv).get("status") or "").strip().lower() == "succeeded"
            }
            pending_variants = [
                v for v in variants if int(v.get("variant_number") or 1) not in completed_variant_numbers
            ]

            if pending_variants:
                sem = asyncio.Semaphore(self._face_variant_concurrency())
                await asyncio.gather(
                    *(
                        self._run_variant_task(
                            sem=sem,
                            job_id=job_id,
                            user_id=user_id,
                            request_dict=payload_json,
                            resolved_config=resolved,
                            variant=v,
                            mode=mode,
                            shared_inputs=shared_inputs,
                            variants_requested=variants_requested,
                        )
                        for v in pending_variants
                    ),
                    return_exceptions=False,
                )

            completed_count = await self._count_completed_variants(job_id)
            if completed_count > 0:
                pricing = await self._commit_pricing_for_job(
                    job_id=job_id,
                    user_id=user_id,
                    pricing=pricing,
                    actual_units=completed_count,
                )
                await self.jobs_repo.update_status(
                    job_id,
                    "succeeded",
                    meta_patch={
                        "variants_completed": completed_count,
                        "variants_requested": variants_requested,
                    },
                )
                await _emit_notification_best_effort(
                    {
                        "event_type": "FACE_READY",
                        "category": "jobs",
                        "priority": "important",
                        "source_service": "svc-face",
                        "source_ref_type": "job",
                        "source_ref_id": str(job_id),
                        "actor_user_id": None,
                        "title": "Your Face output is ready",
                        "body": "Your desifaces.ai Face generation completed successfully.",
                        "action_route": "/notifications",
                        "action_label": "View result",
                        "image_url": None,
                        "payload_json": {"job_id": str(job_id), "completed_variants": int(completed_count)},
                        "metadata_json": {"job_id": str(job_id), "completed_variants": int(completed_count)},
                        "dedupe_key": f"face-ready:{job_id}",
                        "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": True, "email": True}}],
                    },
                    context={"job_id": str(job_id), "user_id": str(user_id), "event_type": "FACE_READY"},
                )
            else:
                await self._fail_job(
                    job_id=job_id,
                    user_id=user_id,
                    pricing=pricing,
                    error_code="ALL_VARIANTS_FAILED",
                    error_message="All image generation variants failed",
                    release_reason="all_variants_failed",
                )

        except Exception as e:
            logger.exception("Job processing failed", extra={"job_id": job_id})
            try:
                if user_id:
                    await self._release_pricing_for_job(
                        job_id=job_id,
                        user_id=user_id,
                        pricing=pricing,
                        reason="processing_exception",
                    )
            except Exception:
                pass

            await self.jobs_repo.update_status(
                job_id,
                "failed",
                error_code="PROCESSING_ERROR",
                error_message=str(e),
            )
            if user_id:
                await _emit_notification_best_effort(
                    {
                        "event_type": "FACE_FAILED",
                        "category": "jobs",
                        "priority": "important",
                        "source_service": "svc-face",
                        "source_ref_type": "job",
                        "source_ref_id": str(job_id),
                        "actor_user_id": None,
                        "title": "Your Face job needs attention",
                        "body": str(e),
                        "action_route": "/notifications",
                        "action_label": "Review issue",
                        "image_url": None,
                        "payload_json": {"job_id": str(job_id), "error_code": "PROCESSING_ERROR"},
                        "metadata_json": {"job_id": str(job_id), "error_code": "PROCESSING_ERROR"},
                        "dedupe_key": f"face-failed:{job_id}:PROCESSING_ERROR",
                        "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": True, "email": True}}],
                    },
                    context={"job_id": str(job_id), "user_id": str(user_id), "event_type": "FACE_FAILED", "error_code": "PROCESSING_ERROR"},
                )
        finally:
            tmp_src_path = str(shared_inputs.get("tmp_src_path") or "").strip()
            if tmp_src_path and os.path.exists(tmp_src_path):
                try:
                    os.remove(tmp_src_path)
                except Exception:
                    pass

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
        shared_inputs: Optional[Dict[str, Any]] = None,
        variants_requested: Optional[int] = None,
    ) -> GeneratedVariant:
        import httpx
        import uuid

        from app.services.providers.image_provider import ImageProviderRouter

        variant_num = int(variant.get("variant_number") or 1)
        seed = int(variant.get("seed") or 0)

        prompt = (variant.get("prompt") or "").strip()
        neg = (variant.get("negative_prompt") or "").strip()

        # Enforce Edit Face UI/backend identity negative prompt.
        # Variant prompts may be generated by CreatorPromptService, but the request-level
        # I2I contract owns source-identity locking and must always be merged.
        if mode == "image-to-image":
            request_negative = str(
                request_dict.get("negative_prompt")
                or request_dict.get("negativePrompt")
                or ""
            ).strip()
            if request_negative:
                neg = _merge_csv_terms(neg, request_negative)

            request_identity_lock = str(
                request_dict.get("identity_lock_instructions")
                or request_dict.get("strict_identity_instruction")
                or ""
            ).strip()
            if request_identity_lock and request_identity_lock.lower() not in prompt.lower():
                prompt = f"{request_identity_lock}\n\nUser requested edit: {prompt}" if prompt else request_identity_lock

        technical = self._coerce_dict(variant.get("technical_specs"))
        aspect_ratio = self._normalize_aspect_ratio(
            technical.get("aspect_ratio") or variant.get("aspect_ratio") or request_dict.get("aspect_ratio")
        )
        req_width, req_height, image_size_hint = self._size_hint_to_dimensions(
            aspect_ratio,
            technical.get("image_size_hint")
            or variant.get("image_size_hint")
            or request_dict.get("image_size_hint")
            or request_dict.get("size"),
        )
        width = int(technical.get("width") or request_dict.get("width") or req_width)
        height = int(technical.get("height") or request_dict.get("height") or req_height)
        num_steps = int(technical.get("num_inference_steps") or 28)
        guidance = float(technical.get("guidance_scale") or 3.5)
        technical["width"] = width
        technical["height"] = height
        technical["aspect_ratio"] = aspect_ratio
        technical["image_size_hint"] = image_size_hint

        source_image_ref: Optional[str] = None
        source_image_url: Optional[str] = None
        strength: Optional[float] = None

        router = getattr(self, "image_provider_router", None)
        if router is None:
            router = ImageProviderRouter()
            self.image_provider_router = router

        tmp_src_path: Optional[str] = None
        tmp_out_path: Optional[str] = None

        provider_name = "openai"
        payload_version = "face:v1"
        base_rh = str(variant.get("request_hash") or "").strip() or str(job_id)
        rh_variant = hashlib.sha256(f"{base_rh}|v={variant_num}|mode={mode}".encode("utf-8")).hexdigest()[
            : self.PRIME_HASH_BYTES
        ]
        idem_key = provider_idempotency_key(provider_name, payload_version, rh_variant)

        async def _download_to_tmp(url: str, dst_path: str) -> None:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, trust_env=False) as client:
                r = await client.get(url)
                r.raise_for_status()
                with open(dst_path, "wb") as f:
                    f.write(r.content)

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
                "aspect_ratio": aspect_ratio,
                "image_size_hint": image_size_hint,
                "shot_type_code": request_dict.get("shot_type_code"),
                "preservation_strength": None,
            },
            response_json={},
            meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
        )

        try:
            if mode == "image-to-image":
                payload_json = request_dict

                shared_inputs = shared_inputs or {}
                source_image_ref = (
                    self._string_or_none(shared_inputs.get("source_image_ref"))
                    or (payload_json.get("source_image_ref") or "").strip()
                    or (payload_json.get("source_image_asset_id") or "").strip()
                    or (payload_json.get("source_image_url") or "").strip()
                )
                if not source_image_ref:
                    raise ValueError("missing_source_image_url")

                source_image_url = self._string_or_none(shared_inputs.get("source_image_url")) or await self._resolve_source_image_ref(source_image_ref)
                if not source_image_url:
                    raise RuntimeError(f"unresolvable_source_image_ref:{source_image_ref}")

                parsed = urlparse(source_image_url)
                if parsed.scheme in ("http", "https"):
                    self._validate_remote_http_url(source_image_url)
                elif parsed.scheme != "file":
                    raise ValueError(f"unsupported_source_image_scheme:{parsed.scheme or 'missing'}")

                strength = self._clamp_strength(shared_inputs.get("preservation_strength"), payload_json.get("preservation_strength") or 0.25)

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
                        "aspect_ratio": aspect_ratio,
                        "image_size_hint": image_size_hint,
                        "shot_type_code": request_dict.get("shot_type_code"),
                        "preservation_strength": float(strength),
                        "source_image_url": source_image_url,
                    },
                    response_json={},
                    meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
                )

                shared_tmp_src_path = self._string_or_none(shared_inputs.get("tmp_src_path"))
                if shared_tmp_src_path and os.path.exists(shared_tmp_src_path):
                    tmp_src_path = shared_tmp_src_path
                else:
                    tmp_src_path = f"/tmp/df_i2i_src_{uuid.uuid4().hex}.png"

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
                    "aspect_ratio": aspect_ratio,
                    "image_size_hint": image_size_hint,
                    "shot_type_code": request_dict.get("shot_type_code"),
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
            elif hasattr(self.storage_service, "upload_from_file") and callable(
                getattr(self.storage_service, "upload_from_file")
            ):
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
            elif hasattr(self.storage_service, "upload_local_file") and callable(
                getattr(self.storage_service, "upload_local_file")
            ):
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
                    "aspect_ratio": aspect_ratio,
                    "image_size_hint": image_size_hint,
                    "shot_type_code": request_dict.get("shot_type_code"),
                    "prompt": prompt[:500],
                    "technical_specs": technical,
                    "creative_variations": creative_variations,
                    "provider": out.provider,
                    "provider_meta": out.meta,
                    "storage_path": storage_path,
                    "source_image_ref": source_image_ref,
                    "source_image_url": source_image_url,
                    "source_image_asset_id": (request_dict.get("source_image_asset_id") or None),
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
                    "shot_type_code": request_dict.get("shot_type_code"),
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
                    "aspect_ratio": aspect_ratio,
                    "image_size_hint": image_size_hint,
                    "shot_type_code": request_dict.get("shot_type_code"),
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
                    "source_image_asset_id": (request_dict.get("source_image_asset_id") or None),
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
                    "aspect_ratio": aspect_ratio,
                    "image_size_hint": image_size_hint,
                    "shot_type_code": request_dict.get("shot_type_code"),
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
                    "source_image_asset_id": (request_dict.get("source_image_asset_id") or None),
                    "preservation_strength": float(strength) if strength is not None else None,
                },
            )

            await self._mark_variant_succeeded(
                job_id,
                variant_num,
                image_url=image_url,
                media_asset_id=str(asset_id),
                face_profile_id=str(profile_id),
                variants_requested=variants_requested or 1,
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
                    "aspect_ratio": aspect_ratio,
                    "image_size_hint": image_size_hint,
                    "shot_type_code": request_dict.get("shot_type_code"),
                    "preservation_strength": float(strength) if strength is not None else None,
                },
                response_json={"error": str(e)[:500]},
                meta_json={"request_hash": base_rh, "rh_variant": rh_variant},
            )
            raise
        finally:
            shared_tmp_src_path = self._string_or_none((shared_inputs or {}).get("tmp_src_path"))
            for p in (tmp_src_path, tmp_out_path):
                if p and p != shared_tmp_src_path and os.path.exists(p):
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
            job_id,
            face_profile_id,
            output_asset_id,
            variant_number,
            prompt_used,
            negative_prompt,
            technical_specs,
            creative_variations
        )
        VALUES (
            $1::uuid,
            $2::uuid,
            $3::uuid,
            $4,
            $5,
            $6,
            $7::jsonb,
            $8::jsonb
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

          a.url as artifact_url,
          a.meta_json as artifact_meta_json,

          ma.storage_ref as storage_ref,
          ma.meta_json as asset_meta_json,

          fjo.prompt_used,
          fjo.technical_specs,
          fjo.creative_variations
        FROM face_job_outputs fjo
        LEFT JOIN media_assets ma ON ma.id = fjo.output_asset_id
        LEFT JOIN LATERAL (
          SELECT url, meta_json
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

            artifact_url = str(r.get("artifact_url") or "").strip()
            storage_ref = str(r.get("storage_ref") or "").strip()
            base_url = artifact_url or storage_ref

            asset_meta = self._coerce_dict(r.get("asset_meta_json"))
            artifact_meta = self._coerce_dict(r.get("artifact_meta_json"))
            meta_for_refresh: Dict[str, Any] = asset_meta or artifact_meta or {}

            image_url = base_url

            try:
                fn = getattr(self.storage_service, "get_readonly_sas_url", None)
                if callable(fn):
                    has_blob_meta = bool(
                        meta_for_refresh.get("blob_name")
                        or meta_for_refresh.get("storage_path")
                        or meta_for_refresh.get("storage_container")
                    )
                    looks_azure = bool(image_url) and ("blob.core.windows.net" in image_url)

                    if looks_azure or has_blob_meta:
                        refreshed = await fn(
                            storage_ref=image_url or None,
                            meta_json=meta_for_refresh if meta_for_refresh else None,
                            hours=24,
                            refresh_if_within_minutes=60,
                        )
                        if refreshed:
                            image_url = str(refreshed).strip()
            except Exception:
                pass

            variants.append(
                GeneratedVariant(
                    variant_number=int(r.get("variant_number") or 0),
                    face_profile_id=str(r.get("face_profile_id") or ""),
                    media_asset_id=str(r.get("media_asset_id") or ""),
                    image_url=str(image_url or ""),
                    prompt_used=str(r.get("prompt_used") or ""),
                    technical_specs=tech,
                    creative_variations=crea,
                )
            )

        payload_json = self._coerce_dict(self._row_get(job, "payload_json", None))
        meta_json = self._coerce_dict(self._row_get(job, "meta_json", None))
        requested: Optional[int] = None
        try:
            if meta_json.get("variants_requested") is not None:
                requested = int(meta_json.get("variants_requested"))
            elif payload_json.get("num_variants") is not None:
                requested = int(payload_json.get("num_variants"))
        except Exception:
            requested = None

        raw_status = self._job_status_str(self._row_get(job, "status", "queued") or "queued")
        try:
            status_enum = JobStatus(raw_status)
        except Exception:
            status_enum = JobStatus.QUEUED

        progress = self._get_progress_info(status_enum, len(variants), requested)
        if progress is not None:
            if meta_json.get("variants_failed") is not None:
                progress["variants_failed"] = int(meta_json.get("variants_failed") or 0)
            if meta_json.get("variants_running") is not None:
                progress["variants_running"] = int(meta_json.get("variants_running") or 0)

        pricing = self._pricing_from_job(job, payload_json)
        pricing_state = str(pricing.get("state") or "").strip()
        if progress is not None and pricing_state:
            progress["pricing_state"] = pricing_state
            if pricing.get("billing_mode"):
                progress["pricing_billing_mode"] = pricing.get("billing_mode")
            if pricing.get("settlement_mode"):
                progress["pricing_settlement_mode"] = pricing.get("settlement_mode")

        pricing_view = self._pricing_to_view(pricing)
        pricing_summary = self._pricing_summary_view(pricing)

        return JobStatusResponse(
            job_id=job_id,
            status=status_enum,
            message=self._get_status_message(status_enum),
            progress=progress,
            variants=variants if variants else None,
            error=self._row_get(job, "error_message", None),
            pricing=pricing_view,
            pricing_summary=pricing_summary,
            created_at=self._row_get(job, "created_at", None),
            updated_at=self._row_get(job, "updated_at", None),
        )

    def _get_status_message(self, status: JobStatus) -> str:
        messages = {
            JobStatus.QUEUED: "Job queued for processing",
            JobStatus.RUNNING: "Generating creator platform variants",
            JobStatus.SUCCEEDED: "Face generation completed successfully",
            JobStatus.FAILED: "Face generation failed",
            JobStatus.CANCELLED: "Job was cancelled",
        }
        return messages.get(status, "Unknown status")

    def _get_progress_info(
        self,
        status: JobStatus,
        variants_count: int,
        requested: Optional[int],
    ) -> Optional[Dict[str, Any]]:
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
            base = {
                "message": f"Generated {variants_count} variants successfully",
                "variants_completed": variants_count,
            }
            if requested is not None:
                base["variants_requested"] = requested
            return base

        return None