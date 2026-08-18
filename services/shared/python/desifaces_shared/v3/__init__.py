"""Shared desifaces-v3 persistence primitives.

These modules implement canonical persistence boundaries for media and generation
without depending on FastAPI or any provider SDK.
"""

from .media_store import CanonicalMediaStore, MediaOwnershipError
from .generation_store import CanonicalGenerationStore, InvalidJobTransition

__all__ = [
    "CanonicalMediaStore",
    "CanonicalGenerationStore",
    "MediaOwnershipError",
    "InvalidJobTransition",
]
