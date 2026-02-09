from __future__ import annotations

# Compatibility shim:
# Older code imported MediaAssetsRepo from app.repos.music_assets_repo.
# Canonical location is app.repos.media_assets_repo.
from .media_assets_repo import MediaAssetsRepo

__all__ = ["MediaAssetsRepo"]