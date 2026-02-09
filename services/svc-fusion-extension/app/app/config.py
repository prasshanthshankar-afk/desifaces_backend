from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # DB
    DATABASE_URL: str = Field(...)

    # Auth (same pattern as svc-dashboard)
    JWT_SECRET: str = Field(...)
    JWT_ALG: str = Field(default="HS256")
    JWT_ISSUER: str = Field(default="desifaces")
    JWT_AUDIENCE: str = Field(default="desifaces_clients")

    # Downstream services (Docker DNS names)
    SVC_FUSION_BASE_URL: str = Field(default="http://df-svc-fusion:8002")
    SVC_FUSION_CREATE_PATH: str = Field(default="/jobs")
    SVC_FUSION_STATUS_PATH: str = Field(default="/jobs/{job_id}")

    SVC_AUDIO_BASE_URL: str = Field(default="http://df-svc-audio:8004")
    SVC_AUDIO_TTS_PATH: str = Field(default="/api/audio/tts")
    SVC_AUDIO_STATUS_PATH: str = Field(default="/api/audio/jobs/{job_id}/status")

    # Longform worker knobs (segment generation)
    WORKER_ENABLED: bool = Field(default=True)
    WORKER_POLL_SECONDS: float = Field(default=1.0)
    WORKER_BATCH_SIZE: int = Field(default=10)

    # Polling behavior for downstream jobs
    AUDIO_POLL_SECONDS: float = Field(default=1.5)
    AUDIO_TIMEOUT_SECONDS: int = Field(default=300)   # TTS can be slower under load
    FUSION_POLL_SECONDS: float = Field(default=3.0)
    FUSION_TIMEOUT_SECONDS: int = Field(default=900)  # HeyGen segment gen can take minutes

    # Chunking defaults (MUST respect svc-fusion duration_sec <= 120)
    DEFAULT_SEGMENT_SECONDS: int = Field(default=60)
    MAX_SEGMENT_SECONDS: int = Field(default=120)

    # Guardrails / throttling
    MAX_INFLIGHT_SEGMENTS_PER_JOB: int = Field(default=2)
    MAX_TOTAL_SEGMENTS_PER_JOB: int = Field(default=500)  # safety rail

    # Stitch worker knobs
    STITCH_WORKER_ENABLED: bool = Field(default=True)
    STITCH_WORKER_POLL_SECONDS: float = Field(default=2.0)
    STITCH_WORKER_BATCH_SIZE: int = Field(default=2)

    # Azure storage for FINAL stitched output
    AZURE_STORAGE_CONNECTION_STRING: str = Field(...)
    # default keep existing container; can switch later to "longform-output" without code changes
    AZURE_FINAL_VIDEO_CONTAINER: str = Field(default="video-output")
    FINAL_SAS_TTL_SECONDS: int = Field(default=15 * 24 * 3600)

    LOG_LEVEL: str = Field(default="INFO")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()