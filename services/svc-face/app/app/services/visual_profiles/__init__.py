from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommunityVisualProfile:
    profile_id: str
    version: str
    t2i_demographic_fragments: tuple[str, ...] = ()
    t2i_quality_fragments: tuple[str, ...] = ()
    i2i_quality_fragments: tuple[str, ...] = ()
    negative_fragments: tuple[str, ...] = ()
    applied_profiles: tuple[str, ...] = ()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "applied_profiles": list(
                self.applied_profiles or (self.profile_id,)
            ),
        }


@dataclass(frozen=True)
class VisualProfileRegistration:
    profile: CommunityVisualProfile
    country_codes: frozenset[str] = frozenset()
    region_codes: frozenset[str] = frozenset()
