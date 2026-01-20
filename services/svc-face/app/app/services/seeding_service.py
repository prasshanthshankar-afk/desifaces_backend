from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Optional

SEED_MODULUS = 2**31 - 1
DEFAULT_CONTEXT = "df:seed:v1"


@dataclass(frozen=True)
class SeedPlan:
    seed_mode: str          # "random" | "deterministic"
    job_seed: int           # server-side base seed
    variant_seed: int       # derived per variant (provider-friendly int)


class HmacSeedService:
    """
    HMAC-based seed mixer:
    - Unpredictable to clients without secret
    - Stable/reproducible server-side if job_seed + secret stay the same
    """

    def __init__(self, secret: bytes, *, context: str = DEFAULT_CONTEXT):
        if not secret or len(secret) < 16:
            raise ValueError("HmacSeedService secret must be >= 16 bytes (recommend 32+).")
        self._secret = secret
        self._context = context

    @classmethod
    def from_env(cls) -> "HmacSeedService":
        hx = (os.getenv("DF_SEED_SECRET_HEX") or "").strip()
        if not hx:
            # Fail fast is best, but if you prefer non-breaking behavior, swap this
            # to a warning + a derived/dev secret.
            raise RuntimeError("Missing DF_SEED_SECRET_HEX (required for HMAC seeding).")
        return cls(secret=bytes.fromhex(hx))

    @staticmethod
    def new_job_seed(bits: int = 63) -> int:
        # Big space; stored in DB meta_json as int
        return secrets.randbits(bits)

    def derive_variant_seed(
        self,
        job_seed: int,
        variant_index: int,
        *,
        purpose: str = "face:gen",
        extra: str = "",
    ) -> int:
        if variant_index < 0:
            raise ValueError("variant_index must be >= 0")

        msg = f"{self._context}|{purpose}|job_seed={job_seed}|v={variant_index}"
        if extra:
            msg += f"|{extra}"

        digest = hmac.new(self._secret, msg.encode("utf-8"), hashlib.sha256).digest()
        n = int.from_bytes(digest[:8], "big")
        return int(n % SEED_MODULUS)

    def resolve_job_seed(self, seed_mode: str, user_seed: Optional[int]) -> tuple[str, int]:
        """
        Resolve job seed mode + job_seed:
        - auto: deterministic if user_seed provided else random
        """
        m = (seed_mode or "auto").strip().lower()
        if m not in ("auto", "random", "deterministic"):
            m = "auto"

        if m == "auto":
            m = "deterministic" if user_seed is not None else "random"

        if m == "deterministic":
            if user_seed is None:
                raise ValueError("deterministic mode requires seed")
            return m, int(user_seed)

        # random
        return "random", self.new_job_seed()