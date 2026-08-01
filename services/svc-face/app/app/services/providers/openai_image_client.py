from __future__ import annotations

import base64
import io
import mimetypes
import os
from typing import Optional, Dict, Any, Tuple

import requests
from PIL import Image, ImageOps, UnidentifiedImageError


_ALLOWED_GPT_IMAGE_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
_ALLOWED_EDIT_OUTPUT_FORMATS = {"png", "jpeg", "jpg", "webp"}
_MAX_EDIT_IMAGE_SIDE = int(os.getenv("OPENAI_EDIT_MAX_SIDE", "4096"))
_DEFAULT_EDIT_UPLOAD_FORMAT = os.getenv("OPENAI_EDIT_UPLOAD_FORMAT", "png").strip().lower() or "png"

_HEIF_REGISTRATION_ATTEMPTED = False
_HEIF_SUPPORT_AVAILABLE = False


def _normalize_gpt_image_size(size: Optional[str]) -> str:
    """
    GPT Image models only support: 1024x1024, 1536x1024, 1024x1536, auto.
    Anything else will cause 400.
    """
    if not size:
        return "auto"
    s = str(size).strip().lower()
    if s in _ALLOWED_GPT_IMAGE_SIZES:
        return s
    return "auto"


def _guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def _safe_output_format(fmt: Optional[str]) -> Optional[str]:
    if not fmt:
        return None
    f = str(fmt).strip().lower()
    if f == "jpg":
        f = "jpeg"
    return f if f in _ALLOWED_EDIT_OUTPUT_FORMATS else None


def _pil_save_format(fmt: str) -> str:
    if fmt == "jpg":
        return "JPEG"
    return fmt.upper()


def _suffix_for_format(fmt: str) -> str:
    return ".jpg" if fmt == "jpeg" else f".{fmt}"


def _is_heic_like(filename_hint: str, image_bytes: bytes) -> bool:
    fn = str(filename_hint or "").lower()
    if fn.endswith((".heic", ".heif")):
        return True
    if len(image_bytes) >= 12 and image_bytes[4:12] in {b"ftypheic", b"ftypheix", b"ftyphevc", b"ftyphevx", b"ftypmif1", b"ftypmsf1"}:
        return True
    return False


def _ensure_heif_support_if_needed(filename_hint: str, image_bytes: bytes) -> None:
    global _HEIF_REGISTRATION_ATTEMPTED, _HEIF_SUPPORT_AVAILABLE
    if not _is_heic_like(filename_hint, image_bytes):
        return
    if _HEIF_REGISTRATION_ATTEMPTED:
        if not _HEIF_SUPPORT_AVAILABLE:
            raise RuntimeError("invalid_image_file_heic_support_missing")
        return

    _HEIF_REGISTRATION_ATTEMPTED = True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _HEIF_SUPPORT_AVAILABLE = True
    except Exception as exc:
        _HEIF_SUPPORT_AVAILABLE = False
        raise RuntimeError("invalid_image_file_heic_support_missing") from exc


def _ensure_reasonable_dimensions(img: Image.Image) -> Image.Image:
    w, h = img.size
    if w <= 0 or h <= 0:
        raise RuntimeError("invalid_image_dimensions")
    longest = max(w, h)
    if longest <= _MAX_EDIT_IMAGE_SIDE:
        return img
    scale = _MAX_EDIT_IMAGE_SIDE / float(longest)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _normalize_image_for_edit(
    *,
    image_bytes: bytes,
    filename_hint: str,
    force_format: Optional[str] = None,
    preserve_alpha: bool = True,
) -> Tuple[bytes, str, str, Dict[str, Any]]:
    """
    OpenAI image edits are sensitive to file validity and color mode.
    Normalize any incoming image into a clean PNG/JPEG/WEBP stream with
    EXIF orientation applied and unsupported modes converted.
    """
    if not image_bytes or len(image_bytes) < 64:
        raise RuntimeError("invalid_image_file_empty")

    try:
        _ensure_heif_support_if_needed(filename_hint, image_bytes)
        with Image.open(io.BytesIO(image_bytes)) as opened:
            img = ImageOps.exif_transpose(opened)
            original_format = str(opened.format or "").upper() or None
            original_mode = str(img.mode or "")

            # Single-frame only; animated formats are not reliable for edits.
            try:
                img.seek(0)
            except Exception:
                pass

            img = _ensure_reasonable_dimensions(img)

            has_alpha = "A" in img.getbands()

            if preserve_alpha:
                if img.mode not in ("RGBA", "RGB"):
                    img = img.convert("RGBA" if has_alpha else "RGB")
            else:
                if img.mode != "RGB":
                    if has_alpha:
                        base = Image.new("RGB", img.size, (255, 255, 255))
                        rgba = img.convert("RGBA")
                        base.paste(rgba, mask=rgba.getchannel("A"))
                        img = base
                    else:
                        img = img.convert("RGB")

            out_fmt = _safe_output_format(force_format) or _safe_output_format(_DEFAULT_EDIT_UPLOAD_FORMAT) or "png"
            if out_fmt == "jpeg":
                # JPEG cannot preserve alpha.
                if img.mode != "RGB":
                    if "A" in img.getbands():
                        base = Image.new("RGB", img.size, (255, 255, 255))
                        rgba = img.convert("RGBA")
                        base.paste(rgba, mask=rgba.getchannel("A"))
                        img = base
                    else:
                        img = img.convert("RGB")
            elif out_fmt == "png":
                if img.mode not in ("RGBA", "RGB"):
                    img = img.convert("RGBA" if preserve_alpha and "A" in img.getbands() else "RGB")
            elif out_fmt == "webp":
                if img.mode not in ("RGBA", "RGB"):
                    img = img.convert("RGBA" if preserve_alpha and "A" in img.getbands() else "RGB")

            out = io.BytesIO()
            save_kwargs: Dict[str, Any] = {}
            if out_fmt == "jpeg":
                save_kwargs.update({"quality": 95, "optimize": True})
            elif out_fmt == "png":
                save_kwargs.update({"optimize": True})
            elif out_fmt == "webp":
                save_kwargs.update({"quality": 95, "method": 6})

            img.save(out, format=_pil_save_format(out_fmt), **save_kwargs)
            payload = out.getvalue()

            base_name = os.path.splitext(os.path.basename(filename_hint or "image"))[0] or "image"
            filename = f"{base_name}_normalized{_suffix_for_format(out_fmt)}"
            content_type = {
                "png": "image/png",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
            }[out_fmt]
            meta = {
                "original_format": original_format,
                "original_mode": original_mode,
                "normalized_format": out_fmt.upper(),
                "normalized_mode": img.mode,
                "size": img.size,
                "bytes": len(payload),
            }
            return payload, filename, content_type, meta
    except UnidentifiedImageError as e:
        if _is_heic_like(filename_hint, image_bytes):
            raise RuntimeError("invalid_image_file_heic_unreadable_or_unsupported") from e
        raise RuntimeError("invalid_image_file_unreadable") from e


def _normalize_mask_png(mask_bytes: bytes, filename_hint: str, target_size: Tuple[int, int]) -> Tuple[bytes, str, str]:
    if not mask_bytes or len(mask_bytes) < 64:
        raise RuntimeError("invalid_mask_file_empty")

    try:
        with Image.open(io.BytesIO(mask_bytes)) as opened:
            mask = ImageOps.exif_transpose(opened)
            try:
                mask.seek(0)
            except Exception:
                pass

            if mask.size != target_size:
                mask = mask.resize(target_size, Image.Resampling.NEAREST)

            if mask.mode != "RGBA":
                # OpenAI mask behavior is most reliable with RGBA PNG.
                mask = mask.convert("RGBA")

            out = io.BytesIO()
            mask.save(out, format="PNG", optimize=True)
            base_name = os.path.splitext(os.path.basename(filename_hint or "mask"))[0] or "mask"
            return out.getvalue(), f"{base_name}_normalized.png", "image/png"
    except UnidentifiedImageError as e:
        raise RuntimeError("invalid_mask_file_unreadable") from e


class OpenAIImageClient:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("missing_openai_api_key")

        # Allow override for proxies / gateways
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

        self.model_t2i = os.getenv("OPENAI_IMAGE_MODEL_T2I", "gpt-image-2")
        self.model_edit = os.getenv("OPENAI_IMAGE_MODEL_EDIT", "gpt-image-2")

        # For GPT Image models, "auto" is a safe default (prevents accidental bad sizes)
        self.image_size = os.getenv("OPENAI_IMAGE_SIZE", "auto")
        self.quality = os.getenv("OPENAI_IMAGE_QUALITY", "high")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _raise_for_status_with_body(self, r: requests.Response) -> None:
        if r.status_code < 400:
            return
        req_id = r.headers.get("x-request-id")
        raise RuntimeError(f"openai_images_error status={r.status_code} req_id={req_id} body={r.text}")

    def generate_image(
        self,
        *,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
    ) -> bytes:
        data: Dict[str, Any] = {
            "model": self.model_t2i,
            "prompt": prompt,
            "size": _normalize_gpt_image_size(size or self.image_size),
            "quality": quality or self.quality,
        }

        r = requests.post(
            f"{self.base_url}/images/generations",
            headers=self._headers(),
            json=data,
            timeout=300,
        )
        self._raise_for_status_with_body(r)

        j = r.json()
        b64 = j["data"][0]["b64_json"]
        return base64.b64decode(b64)

    def edit_image(
        self,
        *,
        prompt: str,
        image_path: str,
        mask_path: Optional[str] = None,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        input_fidelity: Optional[str] = None,
        output_format: Optional[str] = None,
    ) -> bytes:
        data: Dict[str, Any] = {
            "model": self.model_edit,
            "prompt": prompt,
            "size": _normalize_gpt_image_size(size or self.image_size),
            "quality": quality or self.quality,
        }

        safe_output_format = _safe_output_format(output_format)
        if safe_output_format:
            data["output_format"] = safe_output_format

        if input_fidelity and self.model_edit == "gpt-image-1":
            data["input_fidelity"] = input_fidelity

        with open(image_path, "rb") as img_f:
            raw_image_bytes = img_f.read()

        normalized_image_bytes, normalized_image_name, normalized_image_ct, image_meta = _normalize_image_for_edit(
            image_bytes=raw_image_bytes,
            filename_hint=image_path,
            # PNG is the safest upload format for edits.
            force_format="png",
            preserve_alpha=True,
        )

        image_file = io.BytesIO(normalized_image_bytes)

        files: Dict[str, Any] = {
            "image": (normalized_image_name, image_file, normalized_image_ct),
        }

        mask_file: Optional[io.BytesIO] = None
        if mask_path:
            with open(mask_path, "rb") as mask_f:
                raw_mask_bytes = mask_f.read()

            normalized_mask_bytes, normalized_mask_name, normalized_mask_ct = _normalize_mask_png(
                raw_mask_bytes,
                mask_path,
                tuple(image_meta["size"]),
            )
            mask_file = io.BytesIO(normalized_mask_bytes)
            files["mask"] = (normalized_mask_name, mask_file, normalized_mask_ct)

        try:
            r = requests.post(
                f"{self.base_url}/images/edits",
                headers=self._headers(),
                data=data,
                files=files,
                timeout=300,
            )
        finally:
            image_file.close()
            if mask_file:
                mask_file.close()

        self._raise_for_status_with_body(r)

        j = r.json()
        b64 = j["data"][0]["b64_json"]
        return base64.b64decode(b64)
