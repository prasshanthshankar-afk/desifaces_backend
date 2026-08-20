"""Runtime assembly for canonical V3 Studio routes."""

from . import studio_routes as _routes
from .config import settings
from .face_execution_runtime import ParticipantFaceExecutionService

_routes.face_execution = ParticipantFaceExecutionService(
    face_base_url=settings.DF_FACE_BASE_URL,
    store=_routes.store,
)

router = _routes.router

# Additive multi-person Audio/Fusion control-plane routes. Studio services remain
# pricing/execution owners; Director only coordinates dependency, HITL and lineage.
from .studio_e2e_routes import router as _e2e_router  # noqa: E402
from .audio_voice_routes import router as _audio_voice_router  # noqa: E402

router.include_router(_e2e_router)
router.include_router(_audio_voice_router)
