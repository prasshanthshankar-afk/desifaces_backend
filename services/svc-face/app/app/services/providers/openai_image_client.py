from __future__ import annotations

import base64
import mimetypes
import os
from typing import Optional, Dict, Any, Tuple
import requests


_ALLOWED_GPT_IMAGE_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}


def _normalize_gpt_image_size(size: Optional[str]) -> str:
    """
    GPT Image models only support: 1024x1024, 1536x1024, 1024x1536, auto.
    Anything else will cause 400. :contentReference[oaicite:1]{index=1}
    """
    if not size:
        return "auto"
    s = str(size).strip().lower()
    # Preserve canonical formatting if passed in correctly
    if s in _ALLOWED_GPT_IMAGE_SIZES:
        return s
    # If caller passed something like "720x1280" we must not forward it.
    return "auto"


def _guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


class OpenAIImageClient:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("missing_openai_api_key")

        # Allow override for proxies / gateways
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

        self.model_t2i = os.getenv("OPENAI_IMAGE_MODEL_T2I", "gpt-image-1.5")
        self.model_edit = os.getenv("OPENAI_IMAGE_MODEL_EDIT", "gpt-image-1.5")

        # For GPT Image models, "auto" is a safe default (prevents accidental bad sizes)
        self.image_size = os.getenv("OPENAI_IMAGE_SIZE", "auto")
        self.quality = os.getenv("OPENAI_IMAGE_QUALITY", "high")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _raise_for_status_with_body(self, r: requests.Response) -> None:
        if r.status_code < 400:
            return
        req_id = r.headers.get("x-request-id")
        # Make failures actionable in logs
        raise RuntimeError(f"openai_images_error status={r.status_code} req_id={req_id} body={r.text}")

    def generate_image(
        self,
        *,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
    ) -> bytes:
        # returns PNG bytes (GPT Image models always return base64) :contentReference[oaicite:2]{index=2}
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
        # Optional: if you want better identity preservation, you can switch model_edit to gpt-image-1
        # and then pass input_fidelity="high" here (only supported on gpt-image-1). :contentReference[oaicite:3]{index=3}
        input_fidelity: Optional[str] = None,
        output_format: Optional[str] = None,  # png/jpeg/webp; GPT Image only :contentReference[oaicite:4]{index=4}
    ) -> bytes:
        # returns image bytes (base64)
        data: Dict[str, Any] = {
            "model": self.model_edit,
            "prompt": prompt,
            "size": _normalize_gpt_image_size(size or self.image_size),
            "quality": quality or self.quality,
        }

        if output_format:
            data["output_format"] = output_format

        # input_fidelity is only supported for gpt-image-1. :contentReference[oaicite:5]{index=5}
        if input_fidelity and self.model_edit == "gpt-image-1":
            data["input_fidelity"] = input_fidelity

        # Use context managers so file handles always close
        img_ct = _guess_content_type(image_path)
        if mask_path:
            # mask must be PNG <4MB and same dimensions (OpenAI requirement) :contentReference[oaicite:6]{index=6}
            with open(image_path, "rb") as img_f, open(mask_path, "rb") as mask_f:
                files = {
                    "image": (os.path.basename(image_path), img_f, img_ct),
                    "mask": (os.path.basename(mask_path), mask_f, "image/png"),
                }
                r = requests.post(
                    f"{self.base_url}/images/edits",
                    headers=self._headers(),
                    data=data,
                    files=files,
                    timeout=300,
                )
        else:
            with open(image_path, "rb") as img_f:
                files = {
                    "image": (os.path.basename(image_path), img_f, img_ct),
                }
                r = requests.post(
                    f"{self.base_url}/images/edits",
                    headers=self._headers(),
                    data=data,
                    files=files,
                    timeout=300,
                )

        self._raise_for_status_with_body(r)

        j = r.json()
        b64 = j["data"][0]["b64_json"]
        return base64.b64decode(b64)