from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class IdentityProfile:
    signature: str           # short stable id for audit/debug
    tokens: str              # prompt tokens to encourage different facial identity
    negative_tokens: str     # negative prompt tokens to avoid “same archetype” collapse


class IdentityProfileService:
    """
    Generates a "facial identity" token bundle from a job_seed.
    Goal: for the same prompt, different job_seed => noticeably different face identity
    (not only hair/clothes).
    """

    # Reuse your existing seed secret (so identity is not guessable)
    SECRET_ENV_HEX = "DF_SEED_SECRET_HEX"
    CONTEXT = "df:identity:v1"

    # Keep tokens concrete (shape/structure), not styling (hair/clothes).
    FACE_SHAPES = ["oval", "round", "square", "heart-shaped", "diamond-shaped"]
    JAWLINES = ["soft jawline", "defined jawline", "sharp jawline", "gentle jawline"]
    CHEEKBONES = ["high cheekbones", "soft cheekbones", "pronounced cheekbones"]
    NOSES = ["straight nose", "button nose", "aquiline nose", "broad nose", "narrow nose"]
    EYES = ["almond eyes", "round eyes", "hooded eyes", "deep-set eyes", "upturned eyes"]
    EYEBROWS = ["arched eyebrows", "straight eyebrows", "thick eyebrows", "soft eyebrows"]
    LIPS = ["full lips", "thin lips", "balanced lips"]
    CHINS = ["rounded chin", "pointed chin", "square chin"]
    DIMPLING = ["no dimples", "subtle dimples"]  # keep it explicit

    # “Archetype collapse” negatives
    DEFAULT_NEG = (
        "same person, identical face, twin, clone, repeated identity, "
        "same facial structure, same nose, same jawline, same cheekbones"
    )

    @classmethod
    def _secret(cls) -> Optional[bytes]:
        hx = (os.getenv(cls.SECRET_ENV_HEX) or "").strip()
        if not hx:
            return None
        try:
            b = bytes.fromhex(hx)
            return b if len(b) >= 16 else None
        except Exception:
            return None

    @classmethod
    def _pick(cls, *, job_seed: int, request_hash: str, key: str, options: List[str]) -> str:
        """
        Stable pick from options using HMAC(secret, msg) if available;
        else fallback to sha256(msg).
        """
        idx_max = max(1, len(options))
        msg = f"{cls.CONTEXT}|{key}|job_seed={int(job_seed)}|rh={request_hash}".encode("utf-8")
        secret = cls._secret()
        if secret:
            digest = hmac.new(secret, msg, hashlib.sha256).digest()
        else:
            digest = hashlib.sha256(msg).digest()
        n = int.from_bytes(digest[:8], "big")
        return options[n % idx_max]

    @classmethod
    def build(cls, *, job_seed: int, request_hash: str) -> IdentityProfile:
        face = cls._pick(job_seed=job_seed, request_hash=request_hash, key="face_shape", options=cls.FACE_SHAPES)
        jaw = cls._pick(job_seed=job_seed, request_hash=request_hash, key="jawline", options=cls.JAWLINES)
        cheek = cls._pick(job_seed=job_seed, request_hash=request_hash, key="cheekbones", options=cls.CHEEKBONES)
        nose = cls._pick(job_seed=job_seed, request_hash=request_hash, key="nose", options=cls.NOSES)
        eyes = cls._pick(job_seed=job_seed, request_hash=request_hash, key="eyes", options=cls.EYES)
        brows = cls._pick(job_seed=job_seed, request_hash=request_hash, key="brows", options=cls.EYEBROWS)
        lips = cls._pick(job_seed=job_seed, request_hash=request_hash, key="lips", options=cls.LIPS)
        chin = cls._pick(job_seed=job_seed, request_hash=request_hash, key="chin", options=cls.CHINS)
        dimple = cls._pick(job_seed=job_seed, request_hash=request_hash, key="dimple", options=cls.DIMPLING)

        signature = hashlib.sha256(f"{job_seed}|{request_hash}|{cls.CONTEXT}".encode("utf-8")).hexdigest()[:12]

        tokens = (
            "distinct facial identity, "
            f"{face} face, {jaw}, {cheek}, {nose}, {eyes}, {brows}, {lips}, {chin}, {dimple}, "
            "natural human facial asymmetry, realistic pores, unique bone structure"
        )

        negative_tokens = cls.DEFAULT_NEG

        return IdentityProfile(signature=signature, tokens=tokens, negative_tokens=negative_tokens)

    @classmethod
    def inject_into_prompt(cls, prompt: str, identity: IdentityProfile) -> str:
        p = (prompt or "").strip()
        if not p:
            return identity.tokens
        return f"{p}, {identity.tokens}"

    @classmethod
    def inject_into_negative(cls, negative_prompt: str, identity: IdentityProfile) -> str:
        n = (negative_prompt or "").strip()
        if not n:
            return identity.negative_tokens
        return f"{n}, {identity.negative_tokens}"