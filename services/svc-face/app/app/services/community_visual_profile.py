from __future__ import annotations

import re
from typing import Any

from app.services.visual_profiles import CommunityVisualProfile
from app.services.visual_profiles.india import INDIA_PROFILE_REGISTRATION


GLOBAL_PREMIUM_PROFILE = CommunityVisualProfile(
    profile_id="global.premium_human",
    version="1.0",
    t2i_quality_fragments=(
        (
            "premium photorealistic human imagery, expressive eyes and brows, "
            "believable lip expression and head posture, emotionally present and "
            "natural rather than stiff, synthetic or mechanically posed"
        ),
        (
            "when explicitly requested, allow sophisticated adult glamour, fashion, "
            "beauty and editorial presentation with confident expressive posing, "
            "refined makeup and styling, elegant fitted or revealing-but-non-explicit "
            "clothing, and polished cinematic presentation while preserving natural "
            "skin texture, anatomy and culturally coherent appearance"
        ),
        (
            "natural skin texture with realistic pores and tonal variation, "
            "believable facial proportions, physically coherent cinematic lighting "
            "when appropriate while faithfully honoring explicitly requested lighting "
            "and lens behavior; preserve the requested shot size, crop, camera angle, "
            "viewpoint, orientation and aspect ratio"
        ),
        (
            "when visible, use anatomically natural body proportions, realistic "
            "hands and fingers and physically plausible posture; maintain coherent "
            "environment scale, perspective, materials, shadows and lighting; polished "
            "high-end photographic quality without looking synthetic"
        ),
    ),
    i2i_quality_fragments=(
        (
            "premium photorealistic finish while preserving the source person's "
            "exact identity, facial geometry, age, skin tone and gender presentation"
        ),
        (
            "enhance only the requested wardrobe, environment, lighting, camera or "
            "styling changes; retain natural skin texture, realistic anatomy, hands, "
            "fabric behavior and physically coherent lighting"
        ),
    ),
    negative_fragments=(
        "plastic skin, waxy skin, over-smoothed skin, airbrushed beauty-filter face",
        "uncanny eyes, crossed eyes, distorted lips, malformed teeth",
        "extra fingers, fused fingers, malformed hands, broken anatomy, disproportionate limbs",
        "synthetic stock-photo look, overprocessed HDR, incoherent shadows or perspective",
    ),
)


class CommunityVisualProfileResolver:
    """Resolve premium visual guidance from existing backend request context."""

    registrations = (INDIA_PROFILE_REGISTRATION,)

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _normalize(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            value = (
                value.get("en")
                or value.get("code")
                or value.get("name")
                or ""
            )
        text = str(value).strip().upper()
        return re.sub(r"[^A-Z0-9]+", "_", text).strip("_")

    def resolve(
        self,
        request_dict: dict[str, Any],
        region: Any = None,
    ) -> CommunityVisualProfile:
        # Explicit request country is authoritative. A resolved region may carry
        # country metadata, but it must never override a country supplied by the
        # caller. This is critical for globally ambiguous region codes such as
        # TN, GA, OR, MN and LA.
        request_country_candidates = {
            self._normalize(request_dict.get("country_code")),
            self._normalize(request_dict.get("country")),
            self._normalize(request_dict.get("market_country_code")),
        }
        request_country_candidates.discard("")

        region_country_candidates = {
            self._normalize(self._get(region, "country_code")),
            self._normalize(self._get(region, "country")),
            self._normalize(self._get(region, "country_iso2")),
        }
        region_country_candidates.discard("")

        country_candidates = (
            request_country_candidates
            if request_country_candidates
            else region_country_candidates
        )

        region_candidates = {
            self._normalize(request_dict.get("region_code")),
            self._normalize(self._get(region, "code")),
            self._normalize(self._get(region, "display_name")),
        }
        region_candidates.discard("")

        profiles = [GLOBAL_PREMIUM_PROFILE]

        for registration in self.registrations:
            country_match = bool(
                country_candidates.intersection(registration.country_codes)
            )
            region_match = bool(
                region_candidates.intersection(registration.region_codes)
            )

            # Country is authoritative when present. Region-only matching is a
            # backward-compatible fallback for requests that do not carry country.
            # This prevents collisions such as US/TN (Tennessee vs Tamil Nadu),
            # US/GA (Georgia vs Goa), US/OR (Oregon vs Odisha), etc.
            if country_candidates:
                if country_match:
                    profiles.append(registration.profile)
            elif region_match:
                profiles.append(registration.profile)

        def merge(attr: str) -> tuple[str, ...]:
            output: list[str] = []
            seen: set[str] = set()
            for profile in profiles:
                for value in getattr(profile, attr):
                    if value and value not in seen:
                        output.append(value)
                        seen.add(value)
            return tuple(output)

        return CommunityVisualProfile(
            profile_id="+".join(profile.profile_id for profile in profiles),
            version="+".join(profile.version for profile in profiles),
            t2i_demographic_fragments=merge("t2i_demographic_fragments"),
            t2i_quality_fragments=merge("t2i_quality_fragments"),
            i2i_quality_fragments=merge("i2i_quality_fragments"),
            negative_fragments=merge("negative_fragments"),
            applied_profiles=tuple(profile.profile_id for profile in profiles),
        )
