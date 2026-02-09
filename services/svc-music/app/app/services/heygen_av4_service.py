import asyncio
import httpx
from app.config import settings

class HeyGenAV4Service:
    async def create_video(self, *, image_url: str, audio_url: str) -> str:
        url = f"{settings.HEYGEN_BASE_URL}/v2/video/av4/generate"
        headers = {"X-Api-Key": settings.HEYGEN_API_KEY, "Content-Type": "application/json"}
        payload = {
            "title": "desifaces-music",
            "input": {
                "avatar": {"type": "photo", "photo_url": image_url},
                "audio": {"type": "audio", "audio_url": audio_url},
            },
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            j = r.json()
            vid = (j.get("data") or {}).get("video_id")
            if not vid:
                raise RuntimeError(f"heygen_missing_video_id: {j}")
            return vid

    async def poll_video_url(self, *, video_id: str, timeout_s: int = 900) -> str:
        url = f"{settings.HEYGEN_BASE_URL}/v1/video_status.get"
        headers = {"X-Api-Key": settings.HEYGEN_API_KEY}
        async with httpx.AsyncClient(timeout=60) as client:
            waited = 0
            while waited < timeout_s:
                r = await client.get(url, headers=headers, params={"video_id": video_id})
                r.raise_for_status()
                j = r.json()
                data = j.get("data") or {}
                status = data.get("status")
                if status == "completed":
                    out = data.get("video_url")
                    if not out:
                        raise RuntimeError(f"heygen_completed_missing_url: {j}")
                    return out
                if status == "failed":
                    raise RuntimeError(f"heygen_failed: {j}")
                await asyncio.sleep(5)
                waited += 5
        raise RuntimeError("heygen_timeout")