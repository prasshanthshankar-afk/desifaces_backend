from __future__ import annotations

import os
import sys

import app
import app.repos.locale_context_repo as locale_context_repo
import app.repos.tts_catalog_repo as catalog
import app.services.locale_context_resolver as locale_context
import app.services.locale_resolver as locale
import app.services.tts_model_resolver as model
import app.services.azure_tts_adapter as azure_adapter
import app.services.tts_provider_adapter as provider_adapter
import app.services.tts_resolution_planner as planner
import app.services.tts_voice_resolver as voice


expected_root = os.path.realpath(
    os.path.join(
        os.getcwd(),
        "services",
        "svc-audio",
        "app",
    )
)


def assert_under_expected(label: str, path: str) -> None:
    actual = os.path.realpath(path)

    print(f"{label:20s}= {actual}")

    if not actual.startswith(expected_root + os.sep):
        raise SystemExit(
            f"FATAL: {label} loaded outside release under test"
        )


print("python_executable    =", sys.executable)
print("expected_root        =", expected_root)

# `app` may be a namespace package and therefore have __file__ = None.
app_paths = [
    os.path.realpath(str(p))
    for p in getattr(app, "__path__", [])
]

print("app_namespace_paths  =", app_paths)

if not app_paths:
    raise SystemExit(
        "FATAL: app namespace has no search paths"
    )

if not any(
    p.startswith(expected_root + os.sep)
    or p == expected_root + "/app"
    for p in app_paths
):
    raise SystemExit(
        "FATAL: app namespace does not include release under test"
    )

assert_under_expected(
    "locale_resolver",
    locale.__file__,
)
assert_under_expected(
    "locale_context_repo",
    locale_context_repo.__file__,
)
assert_under_expected(
    "locale_context",
    locale_context.__file__,
)
assert_under_expected(
    "tts_catalog_repo",
    catalog.__file__,
)
assert_under_expected(
    "model_resolver",
    model.__file__,
)
assert_under_expected(
    "provider_adapter",
    provider_adapter.__file__,
)
assert_under_expected(
    "azure_adapter",
    azure_adapter.__file__,
)
assert_under_expected(
    "voice_resolver",
    voice.__file__,
)
assert_under_expected(
    "resolution_planner",
    planner.__file__,
)

print("source_provenance=PASS")
