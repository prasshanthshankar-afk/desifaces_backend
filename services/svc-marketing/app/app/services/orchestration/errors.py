# services/svc-marketing/app/app/services/orchestration/errors.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MarketingRunFailed(Exception):
    """
    Structured failure used across orchestration.

    code: stable, machine-readable error code
    message: human/debug message
    stage: pipeline stage (planning/generate/branding/compose/publish)
    """
    code: str
    message: str
    stage: str = "error"

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"