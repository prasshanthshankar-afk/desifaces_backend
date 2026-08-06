from __future__ import annotations

import unittest

from app.repos.locale_context_repo import (
    LocaleContextCandidate,
    LocaleContextFacts,
)
from app.services.locale_context_resolver import (
    LocaleContextResolutionError,
    LocaleContextResolver,
)


class FakeRepository:
    def __init__(
        self,
        *,
        facts,
        candidates=None,
    ):
        self.facts = facts
        self.candidates = list(
            candidates or []
        )
        self.last_query = None

    async def get_locale_facts(self, locale):
        self.requested_locale = locale
        return self.facts

    async def list_matching_candidates(
        self,
        **kwargs,
    ):
        self.last_query = dict(kwargs)
        return list(self.candidates)


class LocaleContextResolverTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_explicit_regional_locale_is_preserved(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="ta-IN",
                language_code="ta",
                region_code="IN",
                country_code="IN",
            )
        )

        result = await LocaleContextResolver(
            repo
        ).resolve(
            canonical_locale="ta-IN",
            country_code="US",
        )

        self.assertEqual(
            result.locale,
            "ta-IN",
        )
        self.assertEqual(
            result.resolution_source,
            "explicit_regional_locale",
        )
        self.assertIsNone(repo.last_query)

    async def test_generic_without_context_is_preserved(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="hi",
                language_code="hi",
                region_code=None,
                country_code=None,
            )
        )

        result = await LocaleContextResolver(
            repo
        ).resolve(
            canonical_locale="hi"
        )

        self.assertEqual(result.locale, "hi")
        self.assertEqual(
            result.resolution_source,
            "generic_no_context",
        )

    async def test_generic_refines_from_database_candidate(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="hi",
                language_code="hi",
                region_code=None,
                country_code=None,
            ),
            candidates=[
                LocaleContextCandidate(
                    locale="regional-from-db",
                    language_code="hi",
                    match_score=1.0,
                    matched_context_count=1,
                )
            ],
        )

        result = await LocaleContextResolver(
            repo
        ).resolve(
            canonical_locale="hi",
            country_code="IN",
        )

        self.assertEqual(
            result.locale,
            "regional-from-db",
        )
        self.assertEqual(
            result.resolution_source,
            "context_refined",
        )

        self.assertEqual(
            repo.last_query["language_code"],
            "hi",
        )
        self.assertEqual(
            repo.last_query["country_code"],
            "IN",
        )
        self.assertEqual(
            repo.last_query[
                "requested_context_count"
            ],
            1,
        )

    async def test_all_supplied_context_dimensions_are_forwarded(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="en",
                language_code="en",
                region_code=None,
                country_code=None,
            )
        )

        await LocaleContextResolver(
            repo
        ).resolve(
            canonical_locale="en",
            country_code="IN",
            region_code="IN",
            accent_code="accent-x",
            dialect_code="dialect-x",
        )

        self.assertEqual(
            repo.last_query[
                "requested_context_count"
            ],
            4,
        )

    async def test_no_match_preserves_generic_for_language_model(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="fr",
                language_code="fr",
                region_code=None,
                country_code=None,
            ),
            candidates=[],
        )

        result = await LocaleContextResolver(
            repo
        ).resolve(
            canonical_locale="fr",
            country_code="US",
        )

        self.assertEqual(
            result.locale,
            "fr",
        )
        self.assertEqual(
            result.resolution_source,
            "generic_no_context_match",
        )

    async def test_equal_top_context_candidates_fail_closed(self):
        repo = FakeRepository(
            facts=LocaleContextFacts(
                locale="en",
                language_code="en",
                region_code=None,
                country_code=None,
            ),
            candidates=[
                LocaleContextCandidate(
                    locale="candidate-a",
                    language_code="en",
                    match_score=1.0,
                    matched_context_count=1,
                ),
                LocaleContextCandidate(
                    locale="candidate-b",
                    language_code="en",
                    match_score=1.0,
                    matched_context_count=1,
                ),
            ],
        )

        with self.assertRaisesRegex(
            LocaleContextResolutionError,
            "ambiguous_locale_context",
        ):
            await LocaleContextResolver(
                repo
            ).resolve(
                canonical_locale="en",
                country_code="XX",
            )

    async def test_missing_locale_fails_closed(self):
        repo = FakeRepository(
            facts=None
        )

        with self.assertRaisesRegex(
            LocaleContextResolutionError,
            "locale_not_found",
        ):
            await LocaleContextResolver(
                repo
            ).resolve(
                canonical_locale="unknown",
                country_code="IN",
            )


if __name__ == "__main__":
    unittest.main()
