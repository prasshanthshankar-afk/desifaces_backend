from __future__ import annotations

import base64
import io
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import (
    AnalyzeImageOptions,
    AnalyzeTextOptions,
    ImageData,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError

from app.config import settings

logger = logging.getLogger("svc-face.safety")

# Prompt safety must not hard-block broad creator terms like "bikini",
# "political", "kid", "fight", or "weapon" by keyword alone. Those are
# context signals for the moderation provider, not automatic user-facing
# rejections. Keep direct keyword blocks only for clearly disallowed content.
#
# Azure Content Safety severities commonly use 0 / 2 / 4 / 6. Moving the
# default block threshold from 2 to 3 gives the requested launch leeway:
# borderline severity 2 is allowed, while severity 4+ remains blocked.
DEFAULT_TEXT_SAFETY_BLOCK_SEVERITY = 3
DEFAULT_IMAGE_SAFETY_BLOCK_SEVERITY = 3

HARD_BLOCK_PATTERNS = [
    {
        "pattern": r"\b(child|kid|minor|underage)\b.{0,80}\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|seductive|lingerie|bikini|revealing)\b",
        "category": "minors",
        "not_permitted": "sexualized or unsafe content involving minors",
        "suggested_changes": "Remove references to minors in sexual, nude, revealing, or unsafe contexts.",
    },
    {
        "pattern": (
            r"\b(?:[0-9]|1[0-7])\s*[- ]?\s*(?:years?|yrs?)\s*[- ]?\s*old\b"
            r".{0,80}\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|"
            r"seductive|lingerie|bikini|revealing)\b"
        ),
        "category": "minors",
        "not_permitted": "sexualized or unsafe content involving minors",
        "suggested_changes": "Remove sexual, nude, lingerie, revealing, or unsafe treatment of anyone under 18.",
    },
    {
        "pattern": (
            r"\b(?:teen|teenage|teenager)\b.{0,80}"
            r"\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|"
            r"seductive|lingerie|revealing)\b"
        ),
        "category": "minors",
        "not_permitted": "sexualized or unsafe content involving young people",
        "suggested_changes": "Use an explicitly adult subject and remove sexual or unsafe young-person context.",
    },
    {
        "pattern": r"\b(nude|naked|porn|pornographic|nsfw|obscene|explicit sexual|sexual act|sex act|erotic sex|exposed genitals)\b",
        "category": "sexual",
        "not_permitted": "nudity, pornography, explicit sexual content, or exposed intimate body parts",
        "suggested_changes": "Keep the subject fully clothed and describe fashion, lighting, mood, and setting instead.",
    },
    {
        "pattern": r"\b(rape|sexual assault|molest|child abuse|child exploitation|underage sex)\b",
        "category": "abuse",
        "not_permitted": "sexual violence, exploitation, or abuse",
        "suggested_changes": "Remove abuse or exploitation references and rewrite the prompt in a safe, non-sexual context.",
    },
    {
        "pattern": r"\b(gore|gory|bloodbath|dismember|decapitat(?:e|ed|ion)|mutilat(?:e|ed|ion))\b",
        "category": "violence",
        "not_permitted": "graphic gore, mutilation, or extreme violence",
        "suggested_changes": "Use non-graphic action or dramatic mood without blood, gore, or bodily harm.",
    },
    {
        "pattern": r"\b(make|manufacture|cook|synthesize|traffic|sell)\b.{0,60}\b(cocaine|heroin|meth|fentanyl|illegal drugs?)\b",
        "category": "illegal_drugs",
        "not_permitted": "instructions or facilitation for illegal drug activity",
        "suggested_changes": "Remove drug-making, selling, trafficking, or usage instructions.",
    },
]

SAFETY_NEGATIVE_PROMPT = """
nude, nudity, naked, nsfw, explicit, sexual, pornographic, obscene,
inappropriate, explicit sexual content, pornographic presentation,
exposed intimate body parts, sexual acts, explicitly sexual poses,
violence, blood, gore, weapons, fighting, abuse,
child in inappropriate context, underage,
political symbols, political figures, controversial symbols,
hate symbols, offensive gestures, drugs, smoking, alcohol abuse,
ugly, distorted, deformed, extra limbs, bad anatomy,
low quality, blurry, watermark, text overlay
"""

AZURE_IMAGE_MAX_BYTES = 4 * 1024 * 1024


def _setting_int(name: str, default: int, *, min_value: int = 0, max_value: int = 6) -> int:
    raw = getattr(settings, name, None)
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(f"DF_{name}")

    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        logger.warning("invalid_safety_threshold", extra={"setting": name, "value": raw, "default": default})
        value = int(default)

    return max(min_value, min(max_value, value))


def _text_block_threshold() -> int:
    return _setting_int("TEXT_SAFETY_BLOCK_SEVERITY", DEFAULT_TEXT_SAFETY_BLOCK_SEVERITY)


def _image_block_threshold() -> int:
    return _setting_int("IMAGE_SAFETY_BLOCK_SEVERITY", DEFAULT_IMAGE_SAFETY_BLOCK_SEVERITY)


def _policy_message(
    *,
    category: str,
    not_permitted: str,
    suggested_changes: str,
    source: str = "prompt",
) -> str:
    target = "Prompt" if source == "prompt" else "Image"
    return (
        f"PROMPT_POLICY_BLOCKED: {target} needs changes. "
        f"Blocked category: {category}. "
        f"Not permitted: {not_permitted}. "
        f"Please change: {suggested_changes} "
        "You can retry after updating the prompt or image."
    )


def _category_policy_message(category: str, *, source: str = "prompt") -> str:
    normalized = category.replace("_", " ").lower()
    if normalized == "sexual":
        return _policy_message(
            category="sexual",
            not_permitted="nudity, pornography, explicit sexual content, or exposed intimate body parts",
            suggested_changes="keep subjects fully clothed and focus on clothing, lighting, mood, camera style, and setting",
            source=source,
        )
    if normalized == "hate":
        return _policy_message(
            category="hate",
            not_permitted="hate, demeaning, or targeted abusive content toward protected groups",
            suggested_changes="remove slurs, hateful framing, and targeted abuse; keep the prompt respectful and neutral",
            source=source,
        )
    if normalized == "self harm":
        return _policy_message(
            category="self_harm",
            not_permitted="self-harm encouragement, instructions, or graphic self-harm content",
            suggested_changes="remove self-harm details and use a supportive, non-graphic framing",
            source=source,
        )
    if normalized == "violence":
        return _policy_message(
            category="violence",
            not_permitted="graphic violence, gore, mutilation, or instructions to harm people",
            suggested_changes="use non-graphic dramatic mood or action without blood, gore, weapons-use instructions, or bodily harm",
            source=source,
        )
    return _policy_message(
        category=normalized or "content_safety",
        not_permitted="content that violates the generation safety policy",
        suggested_changes="rewrite in a family-friendly way and focus on safe visual details",
        source=source,
    )


def _hard_keyword_policy_message(text: str) -> Optional[str]:
    normalized = (text or "").lower()
    for rule in HARD_BLOCK_PATTERNS:
        if re.search(str(rule["pattern"]), normalized, flags=re.IGNORECASE | re.DOTALL):
            return _policy_message(
                category=str(rule["category"]),
                not_permitted=str(rule["not_permitted"]),
                suggested_changes=str(rule["suggested_changes"]),
                source="prompt",
            )
    return None


def _extract_text_severity(response: object, category_name: str) -> int:
    legacy_attr = f"{category_name.lower()}_result"
    legacy = getattr(response, legacy_attr, None)
    if legacy is not None:
        try:
            return int(getattr(legacy, "severity", 0) or 0)
        except Exception:
            return 0

    categories = getattr(response, "categories_analysis", None)
    if categories is None and isinstance(response, dict):
        categories = response.get("categories_analysis") or response.get("categoriesAnalysis")
    if categories:
        wanted = category_name.replace("_", "").lower()
        for item in categories:
            cat = getattr(item, "category", None)
            sev = getattr(item, "severity", None)
            if cat is None and isinstance(item, dict):
                cat = item.get("category")
                sev = item.get("severity")
            cat_name = str(cat).split(".")[-1].replace("_", "").lower() if cat is not None else ""
            if cat_name == wanted:
                try:
                    return int(sev or 0)
                except Exception:
                    return 0

    return 0


class ImageSafetyUnavailableError(RuntimeError):
    pass


class UnsupportedImageFormatError(RuntimeError):
    pass


class ImageTooLargeError(RuntimeError):
    pass


def _default_filename(filename: Optional[str]) -> str:
    name = os.path.basename((filename or "").strip())
    return name or "upload"


def _split_base_ext(filename: Optional[str]) -> Tuple[str, str]:
    base = _default_filename(filename)
    stem, ext = os.path.splitext(base)
    return stem or "upload", (ext or "").lower()


def _filename_with_ext(filename: Optional[str], new_ext: str) -> str:
    stem, _ = _split_base_ext(filename)
    return f"{stem}{new_ext}"


def _content_type_for_extension(ext: str) -> str:
    ext = (ext or "").lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(ext, "image/jpeg")


def _detect_heic(filename: Optional[str], content_type: Optional[str], image_bytes: bytes) -> bool:
    fn = (filename or "").lower()
    ct = (content_type or "").lower()
    if fn.endswith((".heic", ".heif")):
        return True
    if ct in {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}:
        return True
    if len(image_bytes) >= 12 and image_bytes[4:12] in {b"ftypheic", b"ftypheix", b"ftyphevc", b"ftyphevx", b"ftypmif1", b"ftypmsf1"}:
        return True
    return False


def _convert_heic_to_jpeg(image_bytes: bytes) -> bytes:
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
    except Exception as exc:
        raise UnsupportedImageFormatError(
            "HEIC/HEIF image uploaded, but HEIC conversion support is not installed on the server"
        ) from exc

    register_heif_opener()
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=92, optimize=True)
            return out.getvalue()
    except Exception as exc:
        raise UnsupportedImageFormatError("Failed to convert HEIC/HEIF image to JPEG") from exc


def _normalize_image_for_azure(image_bytes: bytes, *, filename: Optional[str], content_type: Optional[str]) -> bytes:
    data = image_bytes
    if _detect_heic(filename, content_type, data):
        data = _convert_heic_to_jpeg(data)

    # If already within Azure limit, use as-is.
    if len(data) <= AZURE_IMAGE_MAX_BYTES:
        return data

    # Re-encode / resize with Pillow to fit limit.
    try:
        from PIL import Image
    except Exception as exc:
        raise ImageTooLargeError(
            f"Image exceeds Azure Content Safety limit ({AZURE_IMAGE_MAX_BYTES} bytes) and Pillow is not available for resizing"
        ) from exc

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            width, height = img.size
            attempts = [
                {"scale": 1.0, "quality": 90},
                {"scale": 0.9, "quality": 85},
                {"scale": 0.8, "quality": 80},
                {"scale": 0.7, "quality": 75},
                {"scale": 0.6, "quality": 70},
            ]
            for attempt in attempts:
                candidate = img
                if attempt["scale"] < 1.0:
                    new_size = (
                        max(1, int(width * attempt["scale"])),
                        max(1, int(height * attempt["scale"])),
                    )
                    candidate = img.resize(new_size)
                out = io.BytesIO()
                candidate.save(out, format="JPEG", quality=attempt["quality"], optimize=True)
                payload = out.getvalue()
                if len(payload) <= AZURE_IMAGE_MAX_BYTES:
                    return payload
    except Exception as exc:
        raise ImageTooLargeError("Failed to normalize image for Azure Content Safety") from exc

    raise ImageTooLargeError(
        f"Image is still too large after normalization for Azure Content Safety ({AZURE_IMAGE_MAX_BYTES} byte limit)"
    )


def _extract_image_severity(response: object, category_name: str) -> int:
    # Newer SDK/sample shape: response.categories_analysis -> list of {category, severity}
    categories = getattr(response, "categories_analysis", None)
    if categories is None and isinstance(response, dict):
        categories = response.get("categories_analysis") or response.get("categoriesAnalysis")
    if categories:
        wanted = category_name.replace("_", "").lower()
        for item in categories:
            cat = getattr(item, "category", None)
            sev = getattr(item, "severity", None)
            if cat is None and isinstance(item, dict):
                cat = item.get("category")
                sev = item.get("severity")
            cat_name = str(cat).split(".")[-1].replace("_", "").lower() if cat is not None else ""
            if cat_name == wanted:
                try:
                    return int(sev or 0)
                except Exception:
                    return 0

    # Older / alternate convenience shape fallback.
    legacy_attr = f"{category_name.lower()}_result"
    legacy = getattr(response, legacy_attr, None)
    if legacy is not None:
        try:
            return int(getattr(legacy, "severity", 0) or 0)
        except Exception:
            return 0

    return 0


def _readable_image_metadata(image_bytes: bytes) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception as exc:
        raise UnsupportedImageFormatError(
            "Server image normalization dependencies are not installed"
        ) from exc

    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            img = ImageOps.exif_transpose(opened)
            return {
                "format": str(opened.format or "").upper() or None,
                "mode": str(img.mode or "") or None,
                "width": int(img.size[0]),
                "height": int(img.size[1]),
            }
    except UnidentifiedImageError as exc:
        raise UnsupportedImageFormatError("Uploaded image format is not readable by the server") from exc




class SafetyService:
    """Content safety validation using Azure Content Safety."""

    def __init__(self) -> None:
        endpoint = (getattr(settings, "AZURE_CONTENT_MODERATOR_ENDPOINT", None) or "").strip()
        key = (getattr(settings, "AZURE_CONTENT_MODERATOR_KEY", None) or "").strip()

        self._endpoint = endpoint
        self._key = key
        self.client: Optional[ContentSafetyClient] = None

        if endpoint and key:
            self.client = ContentSafetyClient(
                endpoint=endpoint,
                credential=AzureKeyCredential(key),
            )

    def check_keywords(self, text: str) -> Tuple[bool, str]:
        reason = _hard_keyword_policy_message(text)
        if reason:
            return False, reason
        return True, ""

    async def validate_text(self, text: str) -> Tuple[bool, str]:
        is_safe, reason = self.check_keywords(text)
        if not is_safe:
            return False, reason

        if self.client is None:
            return True, ""

        try:
            request = AnalyzeTextOptions(text=text)
            response = self.client.analyze_text(request)
            threshold = _text_block_threshold()

            category_severities = {
                "hate": _extract_text_severity(response, "hate"),
                "self_harm": _extract_text_severity(response, "self_harm"),
                "sexual": _extract_text_severity(response, "sexual"),
                "violence": _extract_text_severity(response, "violence"),
            }

            for category, severity in category_severities.items():
                if severity >= threshold:
                    logger.info(
                        "text_safety_blocked",
                        extra={
                            "category": category,
                            "severity": severity,
                            "threshold": threshold,
                        },
                    )
                    return False, _category_policy_message(category, source="prompt")

            return True, ""
        except Exception:
            logger.exception("azure_text_safety_unavailable_fail_open")
            return True, ""

    async def validate_image(
        self,
        image_bytes: bytes,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        fail_open: bool = False,
    ) -> Tuple[bool, str]:
        if not image_bytes:
            return False, "Empty image"

        if self.client is None:
            if fail_open:
                return True, ""
            raise ImageSafetyUnavailableError("Azure Content Safety is not configured")

        try:
            normalized = _normalize_image_for_azure(
                image_bytes,
                filename=filename,
                content_type=content_type,
            )
            image_b64 = base64.b64encode(normalized).decode("utf-8")
            request = AnalyzeImageOptions(image=ImageData(content=image_b64))
            response = self.client.analyze_image(request)

            hate_severity = _extract_image_severity(response, "hate")
            self_harm_severity = _extract_image_severity(response, "self_harm")
            sexual_severity = _extract_image_severity(response, "sexual")
            violence_severity = _extract_image_severity(response, "violence")

            category_severities = {
                "hate": hate_severity,
                "self_harm": self_harm_severity,
                "sexual": sexual_severity,
                "violence": violence_severity,
            }
            threshold = _image_block_threshold()
            for category, severity in category_severities.items():
                if severity >= threshold:
                    logger.info(
                        "image_safety_blocked",
                        extra={
                            "category": category,
                            "severity": severity,
                            "threshold": threshold,
                        },
                    )
                    return False, _category_policy_message(category, source="image")

            return True, ""
        except (UnsupportedImageFormatError, ImageTooLargeError):
            raise
        except HttpResponseError as exc:
            logger.exception("azure_image_safety_http_error")
            if fail_open:
                return True, ""
            raise ImageSafetyUnavailableError("Azure Content Safety analyze_image failed") from exc
        except Exception as exc:
            logger.exception("azure_image_safety_unavailable")
            if fail_open:
                return True, ""
            raise ImageSafetyUnavailableError("Azure Content Safety analyze_image failed") from exc

    async def normalize_image_for_storage_and_generation(
        self,
        image_bytes: bytes,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[bytes, str, str, Dict[str, Any]]:
        if not image_bytes:
            raise UnsupportedImageFormatError("Empty image")

        original_filename = _default_filename(filename)
        original_content_type = (content_type or "").strip().lower() or None
        original_size = len(image_bytes)

        if _detect_heic(filename, content_type, image_bytes):
            converted = _convert_heic_to_jpeg(image_bytes)
            meta = _readable_image_metadata(converted)
            meta.update(
                {
                    "original_filename": original_filename,
                    "original_content_type": original_content_type,
                    "original_size_bytes": original_size,
                    "normalized": True,
                    "normalized_reason": "heic_to_jpeg",
                }
            )
            return converted, _filename_with_ext(original_filename, ".jpg"), "image/jpeg", meta

        meta = _readable_image_metadata(image_bytes)
        stem, ext = _split_base_ext(original_filename)
        normalized_ext = ext or {
            "JPEG": ".jpg",
            "PNG": ".png",
            "WEBP": ".webp",
            "GIF": ".gif",
            "BMP": ".bmp",
            "TIFF": ".tiff",
        }.get(str(meta.get("format") or "").upper(), ".jpg")
        normalized_filename = f"{stem}{normalized_ext}"
        normalized_content_type = original_content_type or _content_type_for_extension(normalized_ext)
        meta.update(
            {
                "original_filename": original_filename,
                "original_content_type": original_content_type,
                "original_size_bytes": original_size,
                "normalized": False,
                "normalized_reason": None,
            }
        )
        return image_bytes, normalized_filename, normalized_content_type, meta

    def get_safety_negative_prompt(self) -> str:
        return SAFETY_NEGATIVE_PROMPT.strip()

    def build_safe_prompt(self, user_prompt: str) -> str:
        safety_additions = """
        elegant, professional photography, high-quality image, well-lit, clear details, flattering angles, tasteful composition,
        """
        return f"{user_prompt}, {safety_additions.strip()}"
