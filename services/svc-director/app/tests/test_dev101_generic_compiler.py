import ast
import asyncio
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "app"
PATH = ROOT / "experience_compiler.py"
TREE = ast.parse(PATH.read_text())

compiler_class = next(
    node for node in TREE.body
    if isinstance(node, ast.ClassDef)
    and node.name == "UserOwnedStoryCompiler"
)
method = next(
    node for node in compiler_class.body
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "compile"
)

boundary = next(
    index for index, statement in enumerate(method.body)
    if isinstance(statement, ast.Assign)
    and any(
        isinstance(target, ast.Name) and target.id == "graph"
        for target in statement.targets
    )
)

method.body = method.body[:boundary] + [
    ast.Return(value=ast.Constant(value="PRECHECK_PASS"))
]

source_validator = runpy.run_path(
    str(ROOT / "localization_source_contract.py")
)["validate_english_source"]


class TranslationRouteRejected(RuntimeError):
    pass


async def resolve_route(conn, *, source_locale, target_locale):
    conn.calls.append((source_locale, target_locale))
    if not conn.allowed:
        raise TranslationRouteRejected("no_approved_translation_direction")
    return {"provider_code": "fixture-provider"}


namespace = {
    "validate_english_source": source_validator,
    "TranslationRouteRejected": TranslationRouteRejected,
    "resolve_approved_translation_route": resolve_route,
}

module = ast.fix_missing_locations(ast.Module(
    body=[
        ast.ImportFrom(
            module="__future__",
            names=[ast.alias(name="annotations")],
            level=0,
        ),
        method,
    ],
    type_ignores=[],
))
exec(compile(module, "<compiler-preflight>", "exec"), namespace)


class FakePool:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def state(source="en-US", target="hi-IN", text="Hello"):
    brief = SimpleNamespace(
        locale=source,
        constraints={
            "source_dialogue_locale": source,
            "requested_output_locale": target,
        },
    )
    turn = SimpleNamespace(
        kind="speech",
        sequence=1,
        speaker_ref="Maya",
        text=text,
    )
    plan = SimpleNamespace(
        scenes=[SimpleNamespace(sequence=1, dialogue=[turn])]
    )
    snapshot = {
        "source_locale": source,
        "target_locale": target,
        "turns": [{
            "turn_id": "1:1",
            "speaker": "Maya",
            "source_text": text,
            "text": "Approved localized text",
        }],
    }
    return brief, plan, snapshot


class GenericCompilerTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, brief, plan, snapshot,
                    *, allowed=True, error=None):
        pool = FakePool(allowed=allowed)
        compiler = SimpleNamespace(_pool=pool)

        async def call():
            return await namespace["compile"](
                compiler,
                brief=brief,
                plan=plan,
                retrieved_context={
                    "_approved_hitl_localization": snapshot
                } if snapshot is not None else {},
            )

        if error:
            with self.assertRaisesRegex(ValueError, error):
                await call()
        else:
            self.assertEqual(await call(), "PRECHECK_PASS")
            self.assertEqual(len(pool.calls), 1)
        return pool

    async def test_approved_existing_direction(self):
        await self.check(*state())

    async def test_generic_non_hindi_fixture(self):
        await self.check(*state("fr-CA", "ar-SA"))

    async def test_missing_approval(self):
        brief, plan, _ = state()
        await self.check(
            brief, plan, None,
            error="localization_approval_required",
        )

    async def test_wrong_target(self):
        brief, plan, snap = state()
        snap["target_locale"] = "fr-CA"
        await self.check(
            brief, plan, snap,
            error="localization_approval_required",
        )

    async def test_wrong_source(self):
        brief, plan, snap = state()
        snap["source_locale"] = "fr-CA"
        await self.check(
            brief, plan, snap,
            error="approved_hitl_source_locale_mismatch",
        )

    async def test_stale_source_turn(self):
        brief, plan, snap = state()
        snap["turns"][0]["source_text"] = "Old script"
        await self.check(
            brief, plan, snap,
            error="approved_hitl_precompile_mismatch",
        )

    async def test_wrong_speaker(self):
        brief, plan, snap = state()
        snap["turns"][0]["speaker"] = "Another person"
        await self.check(
            brief, plan, snap,
            error="approved_hitl_precompile_mismatch",
        )

    async def test_empty_approved_text(self):
        brief, plan, snap = state()
        snap["turns"][0]["text"] = " "
        await self.check(
            brief, plan, snap,
            error="approved_hitl_precompile_mismatch",
        )

    async def test_route_rejected_by_db(self):
        await self.check(
            *state(), allowed=False,
            error="translation_direction_not_approved",
        )

    async def test_english_source_safety_preserved(self):
        await self.check(
            *state(text="नमस्ते दुनिया"),
            error="canonical_english_source_locale_mismatch",
        )

    async def test_no_localization_request(self):
        brief, plan, _ = state("en-US", "en-US")
        compiler = SimpleNamespace(_pool=FakePool())
        result = await namespace["compile"](
            compiler, brief=brief, plan=plan,
            retrieved_context={},
        )
        self.assertEqual(result, "PRECHECK_PASS")
        self.assertEqual(compiler._pool.calls, [])

    async def test_unrequested_snapshot_rejected(self):
        brief, plan, snap = state("en-US", "en-US")
        await self.check(
            brief, plan, snap,
            error="unrequested_hitl_localization",
        )


if __name__ == "__main__":
    unittest.main()
