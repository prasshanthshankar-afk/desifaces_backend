from __future__ import annotations

import base64
import io
import logging
import os
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

BLOCKED_KEYWORDS = [
    "nude", "naked", "nsfw", "explicit", "sexual", "pornographic", "obscene",
    "lingerie", "bikini", "revealing", "transparent", "see-through", "exposed",
    "violence", "blood", "gore", "weapon", "kill", "fight", "abuse",
    "political", "election", "modi", "gandhi", "rahul", "bjp", "congress",
    "child abuse", "underage", "minor", "kid",
    "drugs", "cocaine", "heroin", "meth",
]

SAFETY_NEGATIVE_PROMPT = """
nude, nudity, naked, nsfw, explicit, sexual, pornographic, obscene,
inappropriate, adult content, revealing clothing, transparent clothing,
see-through, exposed body parts, sexual acts, suggestive poses,
violence, blood, gore, weapons, fighting, abuse,
child in inappropriate context, underage,
political symbols, political figures, controversial symbols,
hate symbols, offensive gestures, drugs, smoking, alcohol abuse,
ugly, distorted, deformed, extra limbs, bad anatomy,
low quality, blurry, watermark, text overlay
"""

AZURE_IMAGE_MAX_BYTES = 4 * 1024 * 1024


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
        text_lower = (text or "").lower()
        for keyword in BLOCKED_KEYWORDS:
            if keyword in text_lower:
                return False, f"Blocked keyword detected: {keyword}"
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

            if response.hate_result and response.hate_result.severity >= 2:
                return False, "Content contains hate speech"
            if response.self_harm_result and response.self_harm_result.severity >= 2:
                return False, "Content contains self-harm references"
            if response.sexual_result and response.sexual_result.severity >= 2:
                return False, "Content contains sexual references"
            if response.violence_result and response.violence_result.severity >= 2:
                return False, "Content contains violence"

            return True, ""
        except Exception:
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

            if max(hate_severity, self_harm_severity, sexual_severity, violence_severity) >= 2:
                return False, "Image contains inappropriate content"

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
