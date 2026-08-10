from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import fal_client


class FalQueueError(RuntimeError):
    pass


class FalQueueClient:
    """
    Official fal_client-backed transport.

    This version correctly normalizes fal_client status objects such as:
    - InProgress(...)
    - Queued(...)
    - Completed(...)
    instead of assuming a dict with a `status` field.
    """

    def __init__(self) -> None:
        pass

    @staticmethod
    def _coerce_submit_response(handler: Any) -> Dict[str, Any]:
        request_id = getattr(handler, "request_id", None)
        response_url = getattr(handler, "response_url", None)
        status_url = getattr(handler, "status_url", None)
        cancel_url = getattr(handler, "cancel_url", None)

        if not request_id:
            raise FalQueueError(f"fal submit missing request_id: {handler!r}")

        return {
            "request_id": str(request_id),
            "response_url": str(response_url) if response_url else None,
            "status_url": str(status_url) if status_url else None,
            "cancel_url": str(cancel_url) if cancel_url else None,
        }

    @staticmethod
    def _status_name_from_object(status_obj: Any) -> str:
        if isinstance(status_obj, dict):
            raw = str(status_obj.get("status") or "").strip()
            if raw:
                return raw

        cls_name = type(status_obj).__name__.strip().lower()
        if cls_name == "completed":
            return "COMPLETED"
        if cls_name in {"inprogress", "in_progress"}:
            return "IN_PROGRESS"
        if cls_name in {"queued", "pending"}:
            return "IN_QUEUE"
        if cls_name in {"failed", "error"}:
            return "FAILED"
        if cls_name in {"cancelled", "canceled"}:
            return "CANCELLED"

        raw = str(getattr(status_obj, "status", "") or "").strip()
        return raw or ""

    @classmethod
    def _coerce_status_response(cls, status_obj: Any) -> Dict[str, Any]:
        if isinstance(status_obj, dict):
            out = dict(status_obj)
            if "status" not in out:
                inferred = cls._status_name_from_object(status_obj)
                if inferred:
                    out["status"] = inferred
            return out

        out: Dict[str, Any] = {}
        inferred = cls._status_name_from_object(status_obj)
        if inferred:
            out["status"] = inferred

        for key in (
            "request_id",
            "response_url",
            "queue_position",
            "logs",
            "metrics",
            "error",
            "error_type",
        ):
            value = getattr(status_obj, key, None)
            if value is not None:
                out[key] = value

        if out:
            return out

        raise FalQueueError(f"fal returned unexpected status object: {status_obj!r}")


    @classmethod
    def _coerce_any(cls, value: Any, *, _depth: int = 0) -> Any:
        if _depth > 8:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): cls._coerce_any(v, _depth=_depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._coerce_any(v, _depth=_depth + 1) for v in value]

        out: Dict[str, Any] = {}
        for key in (
            "data", "video", "videos", "outputs", "output", "result", "file", "url",
            "audio", "audio_url", "status", "logs", "detail", "error", "error_type",
            "request_id", "response_url", "queue_position", "duration", "content_type", "file_name", "name"
        ):
            try:
                value_attr = getattr(value, key, None)
            except Exception:
                value_attr = None
            if value_attr is not None:
                out[key] = cls._coerce_any(value_attr, _depth=_depth + 1)
        if out:
            return out

        try:
            as_dict = vars(value)
            if as_dict:
                return {str(k): cls._coerce_any(v, _depth=_depth + 1) for k, v in as_dict.items()}
        except Exception:
            pass

        return str(value)

    @staticmethod
    def _coerce_result_response(result_obj: Any) -> Dict[str, Any]:
        if isinstance(result_obj, dict):
            return result_obj
        coerced = FalQueueClient._coerce_any(result_obj)
        if isinstance(coerced, dict):
            return coerced
        raise FalQueueError(f"fal returned unexpected result object: {result_obj!r}")

    async def submit(self, model_id: str, payload: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        model = str(model_id or "").strip()
        if not model:
            raise FalQueueError("invalid_model_id")

        try:
            handler = await asyncio.to_thread(
                fal_client.submit,
                model,
                arguments=dict(payload or {}),
            )
        except Exception as e:
            raise FalQueueError(f"fal submit failed for {model}: {e}") from e

        return self._coerce_submit_response(handler)

    async def status(self, model_id: str, request_id: str, *, logs: bool = True) -> Dict[str, Any]:
        model = str(model_id or "").strip()
        req = str(request_id or "").strip()
        if not model:
            raise FalQueueError("invalid_model_id")
        if not req:
            raise FalQueueError("missing_request_id")

        try:
            status_obj = await asyncio.to_thread(
                fal_client.status,
                model,
                req,
                with_logs=bool(logs),
            )
        except Exception as e:
            raise FalQueueError(f"fal status failed for {model} request_id={req}: {e}") from e

        return self._coerce_status_response(status_obj)

    async def result(self, model_id: str, request_id: str) -> Dict[str, Any]:
        model = str(model_id or "").strip()
        req = str(request_id or "").strip()
        if not model:
            raise FalQueueError("invalid_model_id")
        if not req:
            raise FalQueueError("missing_request_id")

        try:
            result_obj = await asyncio.to_thread(
                fal_client.result,
                model,
                req,
            )
        except Exception as e:
            raise FalQueueError(f"fal result failed for {model} request_id={req}: {e}") from e

        return self._coerce_result_response(result_obj)

    @staticmethod
    def normalize_status(status_json: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        raw = str(status_json.get("status") or "").strip().upper()
        error_message = str(
            status_json.get("error")
            or status_json.get("error_type")
            or status_json.get("detail")
            or ""
        ).strip() or None

        if raw in {"IN_QUEUE", "QUEUED", "PENDING"}:
            return "processing", None
        if raw in {"IN_PROGRESS", "PROCESSING"}:
            return "processing", None
        if raw in {"COMPLETED", "OK", "SUCCEEDED"}:
            if error_message:
                return "failed", error_message
            return "succeeded", None
        if raw in {"FAILED", "ERROR"}:
            return "failed", error_message or str(status_json)
        if raw in {"CANCELLED", "CANCELED"}:
            return "canceled", str(status_json)
        return "processing", None

    @classmethod
    def extract_video_url(cls, result_json: Dict[str, Any]) -> Optional[str]:
        def _walk(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                s = value.strip()
                if s.startswith(("http://", "https://")) and any(token in s.lower() for token in (".mp4", ".mov", ".webm", "video", "fal.media", "fal.ai", "storage.googleapis.com")):
                    return s
                return None
            if isinstance(value, dict):
                direct = value.get("url")
                if isinstance(direct, str) and direct.strip().startswith(("http://", "https://")):
                    return direct.strip()
                for key in ("video", "videos", "output", "outputs", "data", "result", "file"):
                    found = _walk(value.get(key))
                    if found:
                        return found
                for nested in value.values():
                    found = _walk(nested)
                    if found:
                        return found
                return None
            if isinstance(value, list):
                for item in value:
                    found = _walk(item)
                    if found:
                        return found
                return None
            try:
                coerced = cls._coerce_any(value)
            except Exception:
                coerced = None
            if coerced is value:
                return None
            return _walk(coerced)

        coerced = cls._coerce_any(result_json)
        return _walk(coerced)
