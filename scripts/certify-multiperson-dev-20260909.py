#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

EXPECTED_HOST = "desifaces-dev"
ROOT = Path("/home/azureuser/workspace/desifaces-v3")
BRANCH = "feature/v3-multiperson-core-20260818"
BACKEND_SHA = "a1168a5e6b5ce1ad625fde4735ca198b4a1f62ef"
WEB_REPO = "prasshanthshankar-afk/desifaces_web"
WEB_SHA = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"

REQUIRED = (
    "desifaces-v3-db",
    "desifaces-v3-redis",
    "df-v3-svc-face",
    "df-v3-svc-audio",
    "df-v3-svc-director",
    "df-v3-svc-fusion",
)
NON_TARGET = (
    "desifaces-v3-db",
    "desifaces-v3-redis",
    "df-v3-svc-face-worker",
    "df-v3-svc-audio-worker",
    "df-v3-svc-director-worker",
    "df-v3-svc-fusion",
    "df-v3-svc-fusion-worker",
)
TARGET_APIS = (
    ("svc-face", "df-v3-svc-face", "http://127.0.0.1:18003/api/health"),
    ("svc-audio", "df-v3-svc-audio", "http://127.0.0.1:18004/api/health"),
    ("svc-director", "df-v3-svc-director", "http://127.0.0.1:18011/api/health"),
)


def run(args, *, cwd=None, input_text=None, capture=False, check=True, env=None):
    kwargs = {
        "cwd": str(cwd) if cwd else None,
        "text": True,
        "check": check,
        "env": env,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.run([str(x) for x in args], **kwargs)


def out(args, *, cwd=None):
    return run(args, cwd=cwd, capture=True).stdout.strip()


def container_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def snapshot(name: str) -> str | None:
    if not container_exists(name):
        return None
    return out([
        "docker", "inspect", name, "--format",
        "{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.Config.Image}}",
    ])


def container_image_ref(name: str) -> str:
    return out(["docker", "inspect", name, "--format", "{{.Config.Image}}"])


def assert_dev_target_container(service: str, container: str) -> None:
    networks = out(["docker", "inspect", container, "--format", "{{json .NetworkSettings.Networks}}"])
    if '"df-v3-net"' not in networks:
        raise RuntimeError(f"refusing non-V3 target container: {container}; networks={networks}")
    labels = out(["docker", "inspect", container, "--format", "{{json .Config.Labels}}"])
    # Older V3 containers may have been created by a slightly different compose invocation,
    # which is exactly why a fixed container_name can conflict. Require either the expected
    # service label or no compose service label; never remove a differently-labelled service.
    try:
        parsed = json.loads(labels or "{}") or {}
    except Exception:
        parsed = {}
    label_service = str(parsed.get("com.docker.compose.service") or "").strip()
    if label_service and label_service != service:
        raise RuntimeError(
            f"refusing container owned by different compose service: {container}; "
            f"expected={service}; actual={label_service}"
        )


def wait_http(url: str, label: str, attempts: int = 45) -> None:
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "desifaces-dev-cert"})
            with urllib.request.urlopen(req, timeout=5) as response:
                code = response.getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception:
            code = 0
        print(f"wait={attempt} target={label} http={code}", flush=True)
        if code == 200:
            return
        time.sleep(2)
    raise RuntimeError(f"{label} did not become healthy: {url}")


def unauth_code(url: str) -> int:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "desifaces-dev-cert"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code


def free_port() -> int:
    for port in range(23001, 23021):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        sock.close()
        return port
    raise RuntimeError("no isolated web candidate port available in 23001-23020")


def prepare_web_source(run_dir: Path) -> tuple[Path, callable]:
    candidates = (
        Path("/home/azureuser/workspace/desifaces-web"),
        Path("/home/azureuser/workspace/desifaces-web-review"),
        Path("/home/azureuser/workspace/desifaces_web"),
    )
    for repo in candidates:
        if (repo / ".git").exists() and (repo / "web/Dockerfile").is_file():
            run(["git", "-C", repo, "fetch", "--no-tags", "origin", "main"])
            run(["git", "-C", repo, "cat-file", "-e", f"{WEB_SHA}^{{commit}}"])
            worktree = run_dir / "web-repo"
            run(["git", "-C", repo, "worktree", "add", "--detach", worktree, WEB_SHA])
            return worktree / "web", lambda: run(
                ["git", "-C", repo, "worktree", "remove", "--force", worktree], check=False
            )

    archive = run_dir / "web.tar.gz"
    url = f"https://api.github.com/repos/{WEB_REPO}/tarball/{WEB_SHA}"
    req = urllib.request.Request(url, headers={"User-Agent": "desifaces-dev-cert"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response, archive.open("wb") as target:
            shutil.copyfileobj(response, target)
    except Exception as exc:
        raise RuntimeError(f"web source unavailable and no local checkout found: {exc}") from exc
    extract = run_dir / "web-extract"
    extract.mkdir()
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(extract)
    roots = [p for p in extract.iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "web/Dockerfile").is_file():
        raise RuntimeError("downloaded web source has unexpected structure")
    return roots[0] / "web", lambda: None


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    stamp = str(int(time.time()))
    print("============================================================")
    print(" desifaces DEV — MULTI-PERSON PARITY CERTIFICATION")
    print("============================================================")
    print(f"host={host}")
    print(f"workspace={ROOT}")
    print(f"backend_sha={BACKEND_SHA}")
    print(f"web_sha={WEB_SHA}")
    print("environment=NON_PRODUCTION_ONLY")
    print("production_touch=FORBIDDEN")
    print("target_runtime=Face API + Audio API + Director API only")
    print("provider_generation=FORBIDDEN")
    print("db_schema_change=NONE")
    print("redis_change=NONE")
    print("replacement_mode=DEV_TARGET_REMOVE_RECREATE_WITH_COMMITTED_ROLLBACK_IMAGES")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: this certification may run only on {EXPECTED_HOST}; current={host}")
    if not (ROOT / ".git").exists():
        raise SystemExit(f"FAIL: expected dev Git checkout missing: {ROOT}")
    if not (ROOT / "infra/.env").is_file():
        raise SystemExit(f"FAIL: expected V3 env missing: {ROOT / 'infra/.env'}")
    for name in REQUIRED:
        if not container_exists(name):
            raise SystemExit(f"FAIL: required dev container missing: {name}")
    for service, container, _ in TARGET_APIS:
        assert_dev_target_container(service, container)

    before = {name: snapshot(name) for name in NON_TARGET}
    target_image_refs = {container: container_image_ref(container) for _, container, _ in TARGET_APIS}
    rollback_images = {
        container: f"desifaces-dev-cert-rollback:{service.removeprefix('svc-')}-{stamp}"
        for service, container, _ in TARGET_APIS
    }
    recreated: list[tuple[str, str, str]] = []
    candidate_name = f"df-v3-web-cert-{stamp}"
    candidate_started = False

    run_root = Path(tempfile.mkdtemp(prefix="desifaces-multiperson-dev-cert-"))
    backend = run_root / "backend"
    cleanup_web = lambda: None

    def compose(*args, capture=False, input_text=None):
        return run(
            ["bash", backend / "scripts/v3-compose.sh", *args],
            capture=capture,
            input_text=input_text,
        )

    def create_rollback_images() -> None:
        print("\n===== DEV ROLLBACK IMAGE CHECKPOINT =====", flush=True)
        for service, container, _ in TARGET_APIS:
            tag = rollback_images[container]
            run(["docker", "commit", container, tag])
            run(["docker", "image", "inspect", tag], capture=True)
            print(f"ROLLBACK_IMAGE_{service.upper().replace('-', '_')}=PASS", flush=True)

    def rollback_runtime() -> None:
        if not recreated:
            return
        print("===== DEV AUTOMATIC ROLLBACK =====", flush=True)
        errors: list[str] = []
        for service, container, health in reversed(recreated):
            try:
                run(["docker", "rm", "-f", container], check=False)
                rollback_tag = rollback_images[container]
                original_ref = target_image_refs[container]
                run(["docker", "tag", rollback_tag, original_ref])
                compose("up", "-d", "--no-deps", service)
                wait_http(health, f"rollback-{service}", attempts=30)
                print(f"rollback={service}=PASS", flush=True)
            except Exception as exc:
                errors.append(f"{service}:{exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    try:
        print("\n===== 1. MATERIALIZE IMMUTABLE DEV SOURCE =====", flush=True)
        run(["git", "-C", ROOT, "fetch", "--no-tags", "origin", BRANCH])
        run(["git", "-C", ROOT, "cat-file", "-e", f"{BACKEND_SHA}^{{commit}}"])
        run(["git", "-C", ROOT, "worktree", "add", "--detach", backend, BACKEND_SHA])
        shutil.copy2(ROOT / "infra/.env", backend / "infra/.env")
        print("BACKEND_IMMUTABLE_WORKTREE=PASS")

        print("\n===== 2. STATIC + COMPOSE CONTRACT GATES =====", flush=True)
        critical = (
            "services/svc-director/app/app/main.py",
            "services/svc-director/app/app/audio_execution_runtime.py",
            "services/svc-director/app/app/face_execution_runtime.py",
            "services/svc-director/app/app/fusion_input_performance.py",
            "services/svc-director/app/app/studio_aspect_routes.py",
            "services/svc-director/app/app/studio_routes_runtime.py",
            "services/svc-face/app/app/api/routes/face_media.py",
            "services/svc-audio/app/app/api/routes/v3_audio_output.py",
        )
        run(["python3", "-m", "py_compile", *[backend / p for p in critical]])
        run(["bash", "-n", backend / "scripts/v3-compose.sh"])
        compose("config", "svc-face", "svc-audio", "svc-director", "svc-fusion")
        text_checks = {
            "services/svc-director/app/app/main.py": "creative_director_state_temporarily_unavailable",
            "services/svc-director/app/app/audio_execution_runtime.py": "_sanitize_numeric_delivery",
            "services/svc-director/app/app/face_execution_runtime.py": "studio_input[\"aspect_ratio\"]",
            "services/svc-director/app/app/fusion_input_performance.py": "video: dict[str, Any] = {\"aspect_ratio\": aspect_ratio}",
            "services/svc-director/app/app/studio_aspect_routes.py": "workflow_consistent",
            "services/svc-face/app/app/api/routes/face_media.py": "/assets/{media_asset_id}/read-url",
            "services/svc-audio/app/app/api/routes/v3_audio_output.py": "/jobs/{job_id}/canonical-output",
        }
        for rel, marker in text_checks.items():
            if marker not in (backend / rel).read_text():
                raise RuntimeError(f"static contract missing {marker!r} in {rel}")
        fusion_enums = (backend / "services/svc-fusion/app/app/domain/enums.py").read_text()
        for ratio in ('"16:9"', '"9:16"', '"1:1"'):
            if ratio not in fusion_enums:
                raise RuntimeError(f"Fusion native aspect missing: {ratio}")
        print("STATIC_CONTRACT=PASS")
        print("COMPOSE_INTERPOLATION=PASS")
        print("FUSION_NATIVE_ASPECT_CONTRACT=PASS")

        create_rollback_images()

        print("\n===== 3. BUILD DEV API IMAGES =====", flush=True)
        compose("build", "svc-face", "svc-audio", "svc-director")
        print("DEV_API_IMAGES_BUILD=PASS")

        print("\n===== 4. PRE-DEPLOY IMAGE CONTRACT PROOF =====", flush=True)
        face_probe = """
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/face/assets/{media_asset_id}/read-url' in paths
print('FACE_READ_URL_ROUTE_IMAGE=PASS')
"""
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-face", input_text=face_probe)

        audio_probe = """
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/audio/jobs/{job_id}/canonical-output' in paths
assert '/api/audio/assets/{media_id}/read-url' in paths
print('AUDIO_CANONICAL_ROUTES_IMAGE=PASS')
"""
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-audio", input_text=audio_probe)

        director_probe = r'''
import asyncio
from types import SimpleNamespace
from pydantic import ValidationError
from psycopg.errors import AdminShutdown
from app.main import app, _aget_state_resilient
import app.face_execution_runtime as face_runtime
from app.audio_execution_runtime import _sanitize_numeric_delivery, _preserve_qualitative_direction
from app.fusion_input_performance import _scene_aspect_ratio
from app.studio_aspect_routes import StageAspectIn

paths={getattr(r,'path','') for r in app.routes}
assert '/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio' in paths
for ratio in ('9:16','16:9','1:1'):
    assert StageAspectIn(aspect_ratio=ratio).aspect_ratio == ratio
    assert _scene_aspect_ratio(SimpleNamespace(stage_metadata={'aspect_ratio':ratio})) == ratio
try:
    StageAspectIn(aspect_ratio='4:5')
except ValidationError:
    pass
else:
    raise AssertionError('unsupported aspect accepted')

old=face_runtime._original_base_compile_context_face_input
try:
    face_runtime._original_base_compile_context_face_input=lambda context:{'aspect_ratio':'9:16'}
    for ratio in ('9:16','16:9','1:1'):
        value=face_runtime.compile_context_face_input_with_stage_aspect(SimpleNamespace(metadata={'aspect_ratio':ratio}))
        assert value['aspect_ratio'] == ratio
finally:
    face_runtime._original_base_compile_context_face_input=old

payload={'context':'story proof','volume':'Softer than the previous line','rate':'1.05','pitch':0.0,'style_degree':'0.70'}
notes=_sanitize_numeric_delivery(payload)
assert 'volume' not in payload
assert payload['rate'] == 1.05 and payload['pitch'] == 0.0 and payload['style_degree'] == 0.70
assert notes == ['volume: Softer than the previous line']
_preserve_qualitative_direction(payload, notes)
assert 'delivery_direction=volume: Softer than the previous line' in payload['context']

class G:
    calls=0
    async def aget_state(self, config):
        self.calls += 1
        if self.calls == 1:
            raise AdminShutdown('dev certification transient shutdown')
        return 'RECOVERED'
assert asyncio.run(_aget_state_resilient(G(), {})) == 'RECOVERED'
print('DIRECTOR_CHECKPOINT_RETRY_IMAGE=PASS')
print('AUDIO_QUALITATIVE_DELIVERY_IMAGE=PASS')
print('FACE_ASPECT_PROPAGATION_IMAGE=PASS')
print('FUSION_ASPECT_PROPAGATION_IMAGE=PASS')
print('DIRECTOR_ASPECT_ROUTE_IMAGE=PASS')
'''
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-director", input_text=director_probe)

        print("\n===== 5. REPLACE DEV APIS ONLY =====", flush=True)
        for service, container, health in TARGET_APIS:
            assert_dev_target_container(service, container)
            run(["docker", "rm", "-f", container])
            recreated.append((service, container, health))
            compose("up", "-d", "--no-deps", service)
            wait_http(health, service)
            print(f"DEV_REPLACE_{service.upper().replace('-', '_')}=PASS", flush=True)
        print("DEV_API_RECREATE_SCOPE=PASS")

        print("\n===== 6. DEV API HEALTH + ROUTE CERTIFICATION =====", flush=True)
        for service, _, health in TARGET_APIS:
            wait_http(health, service)
        print("DEV_API_HEALTH=PASS")

        face_routes = out(["docker", "exec", "-i", "df-v3-svc-face", "python", "-c",
                           "from app.main import app; print('\\n'.join(sorted(getattr(r,'path','') for r in app.routes if 'face/assets' in getattr(r,'path',''))))"])
        if "/api/face/assets/{media_asset_id}/read-url" not in face_routes:
            raise RuntimeError("Face read-url route missing in dev runtime")
        audio_routes = out(["docker", "exec", "-i", "df-v3-svc-audio", "python", "-c",
                            "from app.main import app; print('\\n'.join(sorted(getattr(r,'path','') for r in app.routes if 'audio/assets' in getattr(r,'path','') or 'canonical-output' in getattr(r,'path',''))))"])
        if "/api/audio/jobs/{job_id}/canonical-output" not in audio_routes or "/api/audio/assets/{media_id}/read-url" not in audio_routes:
            raise RuntimeError("Audio canonical routes missing in dev runtime")
        director_routes = out(["docker", "exec", "-i", "df-v3-svc-director", "python", "-c",
                               "from app.main import app; print('\\n'.join(sorted(getattr(r,'path','') for r in app.routes if 'aspect-ratio' in getattr(r,'path',''))))"])
        if "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio" not in director_routes:
            raise RuntimeError("Director aspect route missing in dev runtime")
        print("DEV_ROUTE_INVENTORY=PASS")

        face_auth = unauth_code("http://127.0.0.1:18003/api/face/assets/00000000-0000-0000-0000-000000000000/read-url")
        audio_auth = unauth_code("http://127.0.0.1:18004/api/audio/jobs/00000000-0000-0000-0000-000000000000/canonical-output?project_id=00000000-0000-0000-0000-000000000000")
        if face_auth != 401:
            raise RuntimeError(f"Face read-url auth expected 401, got {face_auth}")
        if audio_auth != 401:
            raise RuntimeError(f"Audio canonical-output auth expected 401, got {audio_auth}")
        print("DEV_OWNER_SERVICE_AUTH_GUARDS=PASS")

        network_probe = r'''
import urllib.error, urllib.request
urls=[
 ('face','http://svc-face:8003/api/face/assets/00000000-0000-0000-0000-000000000000/read-url'),
 ('audio','http://svc-audio:8004/api/audio/jobs/00000000-0000-0000-0000-000000000000/canonical-output?project_id=00000000-0000-0000-0000-000000000000'),
]
for name,url in urls:
    try:
        urllib.request.urlopen(url,timeout=5); code=200
    except urllib.error.HTTPError as e:
        code=e.code
    assert code == 401, (name,code)
    print(f'DIRECTOR_TO_{name.upper()}_ROUTE=PASS')
'''
        run(["docker", "exec", "-i", "df-v3-svc-director", "python", "-"], input_text=network_probe)

        print("\n===== 7. ISOLATED WEB BUILD + CANDIDATE =====", flush=True)
        web_src, cleanup_web = prepare_web_source(run_root)
        web_image = f"desifaces-web-dev-cert:{WEB_SHA[:12]}"
        run(["docker", "build", "-t", web_image, web_src])
        run([
            "docker", "run", "--rm", "--entrypoint", "sh", web_image, "-lc",
            "grep -R -q 'Production format' /app/.next && grep -R -q 'Landscape' /app/.next && grep -R -q 'stage-runs' /app/.next",
        ])
        print("WEB_BUILD_AND_FORMAT_BUNDLE=PASS")

        port = free_port()
        run([
            "docker", "run", "-d", "--name", candidate_name, "--network", "df-v3-net",
            "-p", f"127.0.0.1:{port}:3000",
            "-e", "CORE_BASE_URL=http://svc-core:8000",
            "-e", "DASHBOARD_BASE_URL=http://svc-dashboard:8005",
            "-e", "FACE_BASE_URL=http://svc-face:8003",
            "-e", "AUDIO_BASE_URL=http://svc-audio:8004",
            "-e", "FUSION_BASE_URL=http://svc-fusion:8002",
            "-e", "DIRECTOR_BASE_URL=http://svc-director:8011",
            "-e", "FUSION_EXTENSION_BASE_URL=http://svc-fusion-extension:8006",
            "-e", "PRICING_BASE_URL=http://svc-pricing:8009",
            "-e", "COMMERCE_BASE_URL=http://svc-commerce:8008",
            "-e", "ASSISTANT_BASE_URL=http://svc-assistant:8012",
            "-e", "COOKIE_SECURE=false",
            web_image,
        ])
        candidate_started = True
        wait_http(f"http://127.0.0.1:{port}/auth/login", "isolated-web-candidate")
        print("WEB_CANDIDATE=PASS")

        print("\n===== 8. NON-TARGET DEV RUNTIME INVARIANTS =====", flush=True)
        for name, value in before.items():
            if value is None:
                continue
            now = snapshot(name)
            if now != value:
                raise RuntimeError(f"non-target dev runtime changed: {name}\nbefore={value}\nafter={now}")
        print("DB_REDIS_UNCHANGED=PASS")
        print("FACE_WORKER_UNCHANGED=PASS")
        print("AUDIO_WORKER_UNCHANGED=PASS")
        print("DIRECTOR_WORKER_UNCHANGED=PASS")
        print("FUSION_RUNTIME_UNCHANGED=PASS")

        print("\n===== 9. DISCARD DEV ROLLBACK CHECKPOINT IMAGES =====", flush=True)
        for tag in rollback_images.values():
            run(["docker", "image", "rm", tag], check=False)
        print("DEV_ROLLBACK_IMAGES_CLEANED=PASS")

        print("\n============================================================")
        print(" DEV MULTI-PERSON PARITY CERTIFICATION PASS")
        print("============================================================")
        print("BACKEND_IMMUTABLE_WORKTREE=PASS")
        print("STATIC_CONTRACT=PASS")
        print("COMPOSE_INTERPOLATION=PASS")
        print("FUSION_NATIVE_ASPECT_CONTRACT=PASS")
        print("DEV_ROLLBACK_IMAGE_CHECKPOINT=PASS")
        print("DEV_API_IMAGES_BUILD=PASS")
        print("FACE_READ_URL_ROUTE_IMAGE=PASS")
        print("AUDIO_CANONICAL_ROUTES_IMAGE=PASS")
        print("DIRECTOR_CHECKPOINT_RETRY_IMAGE=PASS")
        print("AUDIO_QUALITATIVE_DELIVERY_IMAGE=PASS")
        print("FACE_ASPECT_PROPAGATION_IMAGE=PASS")
        print("FUSION_ASPECT_PROPAGATION_IMAGE=PASS")
        print("DIRECTOR_ASPECT_ROUTE_IMAGE=PASS")
        print("DEV_API_HEALTH=PASS")
        print("DEV_ROUTE_INVENTORY=PASS")
        print("DEV_OWNER_SERVICE_AUTH_GUARDS=PASS")
        print("DIRECTOR_TO_FACE_ROUTE=PASS")
        print("DIRECTOR_TO_AUDIO_ROUTE=PASS")
        print("WEB_BUILD_AND_FORMAT_BUNDLE=PASS")
        print("WEB_CANDIDATE=PASS")
        print("DB_REDIS_UNCHANGED=PASS")
        print("FACE_WORKER_UNCHANGED=PASS")
        print("AUDIO_WORKER_UNCHANGED=PASS")
        print("DIRECTOR_WORKER_UNCHANGED=PASS")
        print("FUSION_RUNTIME_UNCHANGED=PASS")
        print(f"backend_sha={BACKEND_SHA}")
        print(f"web_sha={WEB_SHA}")
        print("NEXT=DEV_BROWSER_MULTI_PERSON_ACCEPTANCE_THEN_ONE_PRODUCTION_PROMOTION")
        return 0

    except Exception as exc:
        print(f"\nDEV_CERTIFICATION_FAIL={type(exc).__name__}:{exc}", file=sys.stderr, flush=True)
        try:
            rollback_runtime()
        except Exception as rollback_exc:
            print(f"DEV_ROLLBACK_ERROR={rollback_exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if candidate_started:
            run(["docker", "rm", "-f", candidate_name], check=False)
        try:
            cleanup_web()
        except Exception:
            pass
        if backend.exists():
            run(["git", "-C", ROOT, "worktree", "remove", "--force", backend], check=False)
        shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
