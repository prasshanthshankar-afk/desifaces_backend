from __future__ import annotations

import unittest

from app.repos.tts_catalog_repo import (
    TTSModelCandidate,
    TTSRoutingPolicy,
)
from app.services.tts_model_resolver import (
    ResolvedTTSModel,
    TTSModelResolutionError,
    TTSModelResolutionRequest,
    TTSModelResolver,
)


def candidate(
    *,
    provider_code: str = "provider-x",
    model_code: str = "model-x",
    canonical_locale: str = "hi-IN",
) -> TTSModelCandidate:
    return TTSModelCandidate(
        provider_code=provider_code,
        adapter_key=provider_code,
        model_code=model_code,
        provider_model_id=model_code,
        quality_class="high",
        canonical_locale=canonical_locale,
        language_code="hi",
        provider_locale_code=canonical_locale,
        provider_language_code=None,
        capability_scope="locale",
        max_input_chars=5000,
        supports_streaming=True,
        supports_multilingual=True,
        supports_ssml=True,
        supports_styles=True,
        supports_emotions=True,
        supports_pace=True,
        quality_score=None,
    )


class FakeCatalog:
    def __init__(
        self,
        *,
        candidates=None,
        policy=None,
        revision: int = 9,
    ):
        self.candidates = list(candidates or [])
        self.policy = policy or TTSRoutingPolicy(
            policy_code="test-policy",
            require_approved_capability=True,
            require_approved_quality=False,
            allow_provider_fallback=True,
        )
        self.revision = revision
        self.last_query = None

    async def get_default_routing_policy(self):
        return self.policy

    async def get_masterdata_revision(self):
        return self.revision

    async def list_routing_enabled_model_candidates(
        self,
        **kwargs,
    ):
        self.last_query = dict(kwargs)
        return list(self.candidates)


class TTSModelResolverTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_resolves_single_data_driven_candidate(self):
        catalog = FakeCatalog(
            candidates=[
                candidate(
                    provider_code="provider-from-db",
                    model_code="model-from-db",
                )
            ],
            revision=42,
        )

        resolver = TTSModelResolver(catalog)

        result = await resolver.resolve(
            TTSModelResolutionRequest(
                canonical_locale="hi-IN",
                text_length=120,
                output_format="mp3",
                country_code="US",
                region_code="VA",
                accent_code="requested-accent",
            )
        )

        self.assertIsInstance(result, ResolvedTTSModel)
        self.assertEqual(
            result.provider_code,
            "provider-from-db",
        )
        self.assertEqual(
            result.model_code,
            "model-from-db",
        )
        self.assertEqual(result.masterdata_revision, 42)

        # Geography is request context. It does not select a provider
        # in application code.
        self.assertEqual(result.country_code, "US")
        self.assertEqual(result.region_code, "VA")
        self.assertEqual(
            result.accent_code,
            "requested-accent",
        )

    async def test_zero_candidates_fails_closed(self):
        resolver = TTSModelResolver(
            FakeCatalog(candidates=[])
        )

        with self.assertRaisesRegex(
            TTSModelResolutionError,
            "no_eligible_tts_model",
        ):
            await resolver.resolve(
                TTSModelResolutionRequest(
                    canonical_locale="fr",
                    text_length=50,
                )
            )

    async def test_multiple_candidates_choose_first_ranked_candidate(self):
        resolver = TTSModelResolver(
            FakeCatalog(
                candidates=[
                    candidate(
                        provider_code="provider-a",
                        model_code="model-a",
                    ),
                    candidate(
                        provider_code="provider-b",
                        model_code="model-b",
                    ),
                ]
            )
        )

        resolved = await resolver.resolve(
            TTSModelResolutionRequest(
                canonical_locale="en-US",
                text_length=100,
                output_format="mp3",
            )
        )

        self.assertEqual(resolved.provider_code, "provider-a")
        self.assertEqual(resolved.model_code, "model-a")

    async def test_policy_requirements_are_forwarded(self):
        catalog = FakeCatalog(
            candidates=[candidate()],
            policy=TTSRoutingPolicy(
                policy_code="strict",
                require_approved_capability=True,
                require_approved_quality=True,
                allow_provider_fallback=False,
            ),
        )

        resolver = TTSModelResolver(catalog)

        await resolver.resolve(
            TTSModelResolutionRequest(
                canonical_locale="hi-IN",
                text_length=321,
                output_format="WAVE",
                requires_style=True,
                requires_emotion=True,
                requires_streaming=True,
            )
        )

        self.assertEqual(
            catalog.last_query[
                "require_approved_capability"
            ],
            True,
        )
        self.assertEqual(
            catalog.last_query[
                "require_approved_quality"
            ],
            True,
        )
        self.assertEqual(
            catalog.last_query["output_format"],
            "wav",
        )
        self.assertEqual(
            catalog.last_query["requires_style"],
            True,
        )
        self.assertEqual(
            catalog.last_query["requires_emotion"],
            True,
        )
        self.assertEqual(
            catalog.last_query["requires_streaming"],
            True,
        )

    async def test_missing_default_policy_fails_closed(self):
        catalog = FakeCatalog(
            candidates=[candidate()]
        )
        catalog.policy = None

        resolver = TTSModelResolver(catalog)

        with self.assertRaisesRegex(
            TTSModelResolutionError,
            "missing_default_tts_routing_policy",
        ):
            await resolver.resolve(
                TTSModelResolutionRequest(
                    canonical_locale="hi-IN",
                    text_length=5,
                )
            )

    async def test_negative_text_length_rejected(self):
        resolver = TTSModelResolver(
            FakeCatalog(candidates=[candidate()])
        )

        with self.assertRaisesRegex(
            TTSModelResolutionError,
            "invalid_text_length",
        ):
            await resolver.resolve(
                TTSModelResolutionRequest(
                    canonical_locale="hi-IN",
                    text_length=-1,
                )
            )


if __name__ == "__main__":
    unittest.main()
