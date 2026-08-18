"""Shared desifaces-v3 persistence primitives.

These modules implement canonical persistence boundaries for media, generation,
storytelling and staged studio HITL workflows without depending on FastAPI or
any provider SDK.
"""

from .media_store import CanonicalMediaStore, MediaOwnershipError
from .generation_store import CanonicalGenerationStore, InvalidJobTransition
from .story_store import CanonicalStoryStore, StoryGraphNotFound, StoryOwnershipError
from .story_generation import CanonicalStoryGenerationStore, StoryGenerationPersistenceResult
from .studio_workflow_store import (
    CanonicalStudioWorkflowStore,
    StageDependencyNotApproved,
    StageReviewIncomplete,
    StudioWorkflowError,
)

__all__ = [
    "CanonicalMediaStore",
    "CanonicalGenerationStore",
    "CanonicalStoryStore",
    "CanonicalStoryGenerationStore",
    "CanonicalStudioWorkflowStore",
    "StoryGenerationPersistenceResult",
    "MediaOwnershipError",
    "InvalidJobTransition",
    "StoryGraphNotFound",
    "StoryOwnershipError",
    "StudioWorkflowError",
    "StageDependencyNotApproved",
    "StageReviewIncomplete",
]
