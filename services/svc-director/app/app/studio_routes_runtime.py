"""Runtime assembly for Studio routes.

The route surface remains stable while the Face execution service is upgraded to
include C5 generation binding, atomic attempt idempotency and MediaAsset linkage.
"""

from . import studio_routes as _routes
from .config import settings
from .face_execution_runtime import ParticipantFaceExecutionService


_routes.face_execution = ParticipantFaceExecutionService(
    face_base_url=settings.DF_FACE_BASE_URL,
    store=_routes.store,
)

router = _routes.router
