# services/svc-marketing/app/app/services/secrets/secret_provider.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class SecretValue:
    value: str


class SecretProvider:
    async def resolve(self, ref: Optional[str]) -> Optional[SecretValue]:
        raise NotImplementedError


class DefaultSecretProvider(SecretProvider):
    """
    Supported formats:
      - env:VAR_NAME           -> reads environment variable
      - literal:the-secret     -> inline secret (avoid in production)
      - keyvault:NAME          -> placeholder for Key Vault integration
      - plain (no prefix)      -> treated as literal
    """

    async def resolve(self, ref: Optional[str]) -> Optional[SecretValue]:
        if not ref:
            return None
        ref = ref.strip()
        if not ref:
            return None

        if ref.startswith("env:"):
            key = ref.split(":", 1)[1].strip()
            v = os.getenv(key)
            return SecretValue(v) if v else None

        if ref.startswith("literal:"):
            return SecretValue(ref.split(":", 1)[1])

        if ref.startswith("keyvault:"):
            # TODO: integrate Azure Key Vault lookup (recommended)
            # For now, allow fallback to env var with same name:
            name = ref.split(":", 1)[1].strip()
            v = os.getenv(name)
            return SecretValue(v) if v else None

        # default: treat as literal
        return SecretValue(ref)