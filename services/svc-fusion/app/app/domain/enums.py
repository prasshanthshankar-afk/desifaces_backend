from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class StepCode(str, Enum):
    provider_submit = "PROVIDER_SUBMIT"
    provider_poll = "PROVIDER_POLL"
    finalize = "FINALIZE"


class StepStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ProviderName(str, Enum):
    heygen_av4 = "heygen_av4"


class ProviderRunStatus(str, Enum):
    created = "created"
    submitted = "submitted"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"


class VoiceMode(str, Enum):
    audio = "audio"
    tts = "tts"  # script + voice_id


class AspectRatio(str, Enum):
    ar_16_9 = "16:9"
    ar_9_16 = "9:16"
    ar_1_1 = "1:1"