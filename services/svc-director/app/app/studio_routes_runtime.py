"""Runtime assembly for canonical V3 Studio routes."""

from . import studio_routes as _routes
from .config import settings
from .face_execution_runtime import ParticipantFaceExecutionService

_routes.face_execution = ParticipantFaceExecutionService(
    face_base_url=settings.DF_FACE_BASE_URL,
    store=_routes.store,
)

router = _routes.router

# Install participant-level Audio and resilient Fusion execution boundaries before
# studio_e2e_routes instantiates the owner-service bridge classes.
from . import audio_execution_runtime as _audio_execution_runtime  # noqa: E402,F401
from . import fusion_execution_runtime as _fusion_execution_runtime  # noqa: E402,F401

# Additive multi-person control-plane routes. Studio services remain
# pricing/execution owners; Director only coordinates dependency, HITL, reuse,
# production preflight and canonical lineage.
from .studio_e2e_routes import router as _e2e_router  # noqa: E402
from .audio_voice_routes import router as _audio_voice_router  # noqa: E402
from .face_reuse_routes import router as _face_reuse_router  # noqa: E402
from .studio_preflight_routes import router as _preflight_router  # noqa: E402

router.include_router(_e2e_router)
router.include_router(_audio_voice_router)
router.include_router(_face_reuse_router)
router.include_router(_preflight_router)
