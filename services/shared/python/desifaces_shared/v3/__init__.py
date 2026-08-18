"""Shared desifaces-v3 persistence primitives.

These modules implement canonical persistence boundaries for media, generation and
storytelling without depending on FastAPI or any provider SDK.
"""

from .media_store import CanonicalMediaStore, MediaOwnershipError
from .generation_store import CanonicalGenerationStore, InvalidJobTransition
from .story_store import CanonicalStoryStore, StoryGraphNotFound, StoryOwnershipError

__all__ = [
    "CanonicalMediaStore",
    "CanonicalGenerationStore",
    "CanonicalStoryStore",
    "MediaOwnershipError",
    "InvalidJobTransition",
    "StoryGraphNotFound",
    "StoryOwnershipError",
]
