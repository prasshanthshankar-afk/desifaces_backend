from __future__ import annotations

import os
import httpx


class AzureTTSService:
    def __init__(self):
        self.key = os.getenv("AZURE_SPEECH_KEY", "").strip()
        self.region = os.getenv("AZURE_SPEECH_REGION", "").strip()
        if not self.key:
            raise RuntimeError("missing_azure_speech_key")
        if not self.region:
            raise RuntimeError("missing_azure_speech_region")

        self.endpoint = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"

        # Optional default (can be overridden per call)
        self.default_output_format = os.getenv(
            "AZURE_SPEECH_OUTPUT_FORMAT",
            "audio-48khz-192kbitrate-mono-mp3",
        ).strip()

    async def synthesize(self, *, ssml: str, output_format: str) -> bytes:
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": output_format,
            "User-Agent": "desifaces-svc-audio",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(self.endpoint, headers=headers, content=ssml.encode("utf-8"))
            if r.status_code != 200:
                raise RuntimeError(f"azure_tts_failed status={r.status_code} body={r.text[:500]}")
            return r.content

    async def synthesize_wav(self, *, ssml: str) -> bytes:
        return await self.synthesize(ssml=ssml, output_format="riff-24khz-16bit-mono-pcm")

    async def synthesize_mp3(self, *, ssml: str) -> bytes:
        # Common MP3 format; you can change via env if you want
        fmt = self.default_output_format or "audio-48khz-192kbitrate-mono-mp3"
        return await self.synthesize(ssml=ssml, output_format=fmt)