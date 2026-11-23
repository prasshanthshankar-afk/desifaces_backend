import ast
import asyncio
import json
import unittest
from pathlib import Path

SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app/services/dev_provider_translation.py"
)
SOURCE = SOURCE_PATH.read_text()
SHARED_PATH = (
    Path(__file__).resolve().parents[4]
    / "services/shared/python/desifaces_shared/v3"
    / "translation_route_contract.py"
)
SHARED_SOURCE = SHARED_PATH.read_text()


class Rejected(Exception):
    pass


tree = ast.parse(SOURCE)
resolver_node = next(
    n for n in ast.parse(SHARED_SOURCE).body
    if isinstance(n, ast.AsyncFunctionDef)
    and n.name == "resolve_approved_translation_route"
)

namespace = {
    "json": json,
    "TranslationRouteRejected": Rejected,
}

exec(
    compile(
        ast.fix_missing_locations(
            ast.Module(body=[resolver_node], type_ignores=[])
        ),
        str(SHARED_PATH),
        "exec",
    ),
    namespace,
)

resolve = namespace["resolve_approved_translation_route"]


class FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_count = 0

    async def fetch(self, sql, source, target):
        self.fetch_count += 1
        assert "public.tts_locales" in sql

        return [
            {
                "locale": r["locale"],
                "language_code": r["language_code"],
                "meta_json": r["meta_json"],
            }
            for r in self.rows
            if r["enabled"]
            and (
                r["locale"] == target
                or r["locale"] == source
                or (
                    "-" not in source
                    and r["language_code"].lower() == source.lower()
                )
            )
        ]


def entry(source, target, **changes):
    value = {
        "source_locale": source,
        "target_locale": target,
        "provider": "test-provider",
        "model": "test-model",
        "provider_source_code": source,
        "provider_target_code": target,
        "enabled": True,
        "approved": True,
    }
    value.update(changes)
    return value


def catalog(entries, target="hi-IN", routing_enabled=True):
    return FakePool([
        {
            "locale": "en-US",
            "language_code": "en",
            "meta_json": {},
            "enabled": True,
        },
        {
            "locale": "en-IN",
            "language_code": "en",
            "meta_json": {},
            "enabled": True,
        },
        {
            "locale": target,
            "language_code": target.split("-")[0],
            "meta_json": {
                "translation_routing_v1": {
                    "enabled": routing_enabled,
                    "capabilities": entries,
                }
            },
            "enabled": True,
        },
    ])


def run(pool, source, target):
    return asyncio.run(
        resolve(
            pool,
            source_locale=source,
            target_locale=target,
        )
    )


class ApprovedDirectionTests(unittest.TestCase):

    def test_exact_source_approval(self):
        p = catalog([
            entry("en-IN", "hi-IN"),
            entry("en-US", "hi-IN"),
        ])
        route = run(p, "en-US", "hi-IN")
        self.assertEqual(route["provider_code"], "test-provider")
        self.assertEqual(route["model_code"], "test-model")
        self.assertEqual(route["provider_source_code"], "en-US")
        self.assertEqual(p.fetch_count, 1)

    def test_second_approved_direction(self):
        p = catalog([
            entry("en-IN", "hi-IN"),
            entry("en-US", "hi-IN"),
        ])
        self.assertEqual(
            run(p, "en-IN", "hi-IN")["provider_source_code"],
            "en-IN",
        )

    def test_generic_target_locale(self):
        p = catalog(
            [entry("en-US", "es-MX")],
            target="es-MX",
        )
        self.assertEqual(
            run(p, "en-US", "es-MX")["provider_target_code"],
            "es-MX",
        )

    def test_unapproved_route_blocked(self):
        p = catalog([
            entry("en-US", "hi-IN", approved=False)
        ])
        with self.assertRaisesRegex(
            Rejected, "no_approved_translation_direction"
        ):
            run(p, "en-US", "hi-IN")

    def test_disabled_route_blocked(self):
        p = catalog(
            [entry("en-US", "hi-IN")],
            routing_enabled=False,
        )
        with self.assertRaisesRegex(
            Rejected, "no_approved_translation_direction"
        ):
            run(p, "en-US", "hi-IN")

    def test_duplicate_route_blocked(self):
        p = catalog([
            entry("en-US", "hi-IN"),
            entry("en-US", "hi-IN"),
        ])
        with self.assertRaisesRegex(
            Rejected, "ambiguous_translation_route"
        ):
            run(p, "en-US", "hi-IN")

    def test_ambiguous_base_source_blocked(self):
        p = catalog([
            entry("en-US", "hi-IN"),
            entry("en-IN", "hi-IN"),
        ])
        with self.assertRaisesRegex(
            Rejected, "ambiguous_source_locale"
        ):
            run(p, "en", "hi-IN")

    def test_missing_source_blocked(self):
        p = catalog([entry("en-US", "hi-IN")])
        with self.assertRaisesRegex(
            Rejected, "no_approved_translation_direction"
        ):
            run(p, "fr-FR", "hi-IN")

    def test_auto_not_accepted_as_prevalidated_source(self):
        p = catalog([entry("en-US", "hi-IN")])
        with self.assertRaisesRegex(
            Rejected, "explicit_source_and_target_locale_required"
        ):
            run(p, "auto", "hi-IN")
        self.assertEqual(p.fetch_count, 0)

    def test_no_provider_execution_inside_resolver(self):
        body = ast.get_source_segment(SHARED_SOURCE, resolver_node)
        self.assertNotIn(
            "translate_via_configured_provider", body
        )
        self.assertNotIn("httpx.AsyncClient", body)

    def test_original_execution_uses_shared_selection(self):
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "translate_approved"
        )
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "resolve_approved_translation_route"
        ]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
