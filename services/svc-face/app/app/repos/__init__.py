"""
Repositories package.

IMPORTANT:
- Do not instantiate repos here.
- Do not reference db_pool or 'self' here.
- Keep this module side-effect free.
"""

__all__ = [
    "BaseRepository",
    "FaceJobsRepo",
    "FaceProfilesRepo",
    "MediaAssetsRepo",
    "CreatorPlatformConfigRepo",
    "ArtifactsRepo",
]

from .base_repo import BaseRepository
from .face_jobs_repo import FaceJobsRepo
from .face_profiles_repo import FaceProfilesRepo
from .media_assets_repo import MediaAssetsRepo
from .creator_config_repo import CreatorPlatformConfigRepo
from .artifacts_repo import ArtifactsRepo