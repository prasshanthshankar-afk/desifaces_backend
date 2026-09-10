#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import time

EXPECTED_HOST = "desifaces-dev"
BACKEND_ROOT = Path("/home/azureuser/workspace/desifaces-v3")
WEB_ROOT = Path("/home/azureuser/workspace/desifaces-web")
BACKEND_REF = "56956867dcad4278d63d1f2c8f652c084087e619"
WEB_REF = "732a2ce92727be9166c57d81e2c8b28cc98df47c"
NETWORK = "df-v3-net"
EXTENSION = "df-v3-svc-fusion-extension"
WEB = "df-v3-web"
WEB_PORT = 13000

NON_TARGETS = (
    "desifaces-v3-db",
    "desifaces-v3-redis",
    "df-v3-svc-core",
    "df-v3-svc-pricing",
    "df-v3-svc-face",
    "df-v3-svc-face-worker",
    "df-v3-svc-audio",
    "df-v3-svc-audio-worker",
    "df-v3-svc-fusion",
    "df-v3-svc-fusion-worker",
    "df-v3-svc-fusion-extension-stitch-worker",
    "df-v3-svc-director",
    "df-v3-svc-director-worker",
)


def run(args, *, cwd=None, input_text=None, capture=False, check=True):
    kwargs = {
        "cwd": str(cwd) if cwd else None,
        "input": input_text,
        "text": True,
        "check": check,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.run([str(x) for x in args], **kwargs)


def out(args, *, cwd=None, input_text=None):
    return run(args, cwd=cwd, input_text=input_text, capture=True).stdout.strip()


def exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def field(name: str, template: str) -> str:
    cp = run(["docker", "inspect", name, "--format", template], capture=True, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def snapshot(name: str) -> str:
    return field(name, "{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.Config.Image}}|{{.RestartCount}}")


def wait_http(url: str, label: str, attempts: int = 45) -> None:
    for i in range(1, attempts + 1):
        cp = run([
            "curl", "-sS", "--connect-timeout", "2", "--max-time", "5",
            "-o", "/dev/null", "-w", "%{http_code}", url,
        ], capture=True, check=False)
        code = cp.stdout.strip() if cp.returncode == 0 else "0"
        print(f"wait={i} target={label} http={code}", flush=True)
        if code == "200":
            return
        time.sleep(2)
    raise RuntimeError(f"{label} did not become healthy")


def free_port() -> int:
    for port in range(23001, 23021):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            s.close()
            continue
        s.close()
        return port
    raise RuntimeError("no free candidate port in 23001-23020")


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces DEV — MULTI-PERSON FUSION CONTRACT REPAIR")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("production_touch=FORBIDDEN")
    print(f"backend_ref={BACKEND_REF}")
    print(f"web_ref={WEB_REF}")
    print("runtime_scope=Fusion Extension API + persistent dev web only")
    print("provider_generation=NONE")
    print("database_schema_change=NONE")
    print("db_redis_restart=FORBIDDEN")
    print("director_face_audio_fusion_restart=FORBIDDEN")
    print("stitch_worker_restart=FORBIDDEN")
    print("resolved_environment_output=FORBIDDEN")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    if not (BACKEND_ROOT / ".git").exists():
        raise SystemExit(f"FAIL: backend Git checkout missing: {BACKEND_ROOT}")
    if not (WEB_ROOT / ".git").exists():
        raise SystemExit(f"FAIL: web Git checkout missing: {WEB_ROOT}")
    if not (BACKEND_ROOT / "infra/.env").is_file():
        raise SystemExit("FAIL: dev V3 env file missing")
    if not exists(EXTENSION) or not exists(WEB):
        raise SystemExit("FAIL: existing dev Fusion Extension API or web runtime missing")
    for name in NON_TARGETS:
        if not exists(name):
            raise SystemExit(f"FAIL: required non-target dev runtime missing: {name}")

    before = {name: snapshot(name) for name in NON_TARGETS}
    old_ext_id = field(EXTENSION, "{{.Image}}")
    old_ext_ref = field(EXTENSION, "{{.Config.Image}}") or "desifaces-v3-svc-fusion-extension"
    old_web_image = field(WEB, "{{.Config.Image}}")
    if not old_ext_id:
        raise SystemExit("FAIL: current Fusion Extension image ID unavailable")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    ext_rollback = f"desifaces-dev-rollback-fusion-extension:{stamp}"
    web_rollback = f"df-v3-web-rollback-fusion-contract-{stamp}"
    web_candidate = f"df-v3-web-candidate-fusion-contract-{stamp}"
    tmp = Path(tempfile.mkdtemp(prefix="desifaces-fusion-contract-dev-"))
    backend = tmp / "backend"
    webrepo = tmp / "webrepo"
    extension_replaced = False
    web_swapped = False
    web_started = False
    candidate_started = False
    success = False

    def compose(*args, capture=False, input_text=None, check=True):
        return run(
            ["bash", backend / "scripts/v3-compose.sh", *args],
            capture=capture,
            input_text=input_text,
            check=check,
        )

    def run_web(name: str, port: int, restart: str, image: str):
        return run([
            "docker", "run", "-d", "--name", name, "--restart", restart,
            "--network", NETWORK, "-p", f"127.0.0.1:{port}:3000",
            "-e", "CORE_BASE_URL=http://df-v3-svc-core:8000",
            "-e", "DASHBOARD_BASE_URL=http://df-v3-svc-dashboard:8005",
            "-e", "FACE_BASE_URL=http://df-v3-svc-face:8003",
            "-e", "AUDIO_BASE_URL=http://df-v3-svc-audio:8004",
            "-e", "FUSION_BASE_URL=http://df-v3-svc-fusion:8002",
            "-e", "DIRECTOR_BASE_URL=http://df-v3-svc-director:8011",
            "-e", "FUSION_EXTENSION_BASE_URL=http://df-v3-svc-fusion-extension:8006",
            "-e", "PRICING_BASE_URL=http://df-v3-svc-pricing:8009",
            "-e", "COMMERCE_BASE_URL=http://df-v3-svc-commerce:8008",
            "-e", "NOTIFICATION_BASE_URL=http://df-v3-svc-core:8000",
            "-e", "ASSISTANT_BASE_URL=http://df-v3-svc-assistant:8012",
            "-e", "COOKIE_SECURE=false",
            "--label", "desifaces.environment=dev",
            "--label", "desifaces.purpose=browser-acceptance",
            "--label", f"desifaces.web_sha={WEB_REF}",
            image,
        ])

    def rollback():
        print("===== DEV AUTOMATIC ROLLBACK =====", flush=True)
        if candidate_started and exists(web_candidate):
            run(["docker", "rm", "-f", web_candidate], check=False)
        if web_started and exists(WEB):
            run(["docker", "rm", "-f", WEB], check=False)
        if web_swapped and exists(web_rollback):
            run(["docker", "rename", web_rollback, WEB], check=False)
            run(["docker", "update", "--restart=unless-stopped", WEB], check=False)
            run(["docker", "start", WEB], check=False)
        if extension_replaced:
            run(["docker", "rm", "-f", EXTENSION], check=False)
            run(["docker", "tag", ext_rollback, old_ext_ref], check=False)
            compose("up", "-d", "--no-deps", "svc-fusion-extension", check=False)
            try:
                wait_http("http://127.0.0.1:18006/api/health", "rollback-fusion-extension", 30)
            except Exception:
                pass
        print("DEV_ROLLBACK=ATTEMPTED", flush=True)

    try:
        print("\n===== 1. MATERIALIZE IMMUTABLE SOURCE =====", flush=True)
        run(["git", "-C", BACKEND_ROOT, "fetch", "--no-tags", "origin", "feature/v3-multiperson-core-20260818"])
        run(["git", "-C", BACKEND_ROOT, "cat-file", "-e", f"{BACKEND_REF}^{{commit}}"])
        run(["git", "-C", BACKEND_ROOT, "worktree", "add", "--detach", backend, BACKEND_REF])
        shutil.copy2(BACKEND_ROOT / "infra/.env", backend / "infra/.env")
        os.chmod(backend / "infra/.env", 0o600)

        run(["git", "-C", WEB_ROOT, "fetch", "--no-tags", "origin", "main"])
        run(["git", "-C", WEB_ROOT, "cat-file", "-e", f"{WEB_REF}^{{commit}}"])
        run(["git", "-C", WEB_ROOT, "worktree", "add", "--detach", webrepo, WEB_REF])
        print("IMMUTABLE_BACKEND_SOURCE=PASS")
        print("IMMUTABLE_WEB_SOURCE=PASS")

        print("\n===== 2. CROSS-SERVICE STATIC CONTRACT =====", flush=True)
        director_pricing = (backend / "services/svc-director/app/app/fusion_execution_parent_pricing.py").read_text()
        ext_main = (backend / "services/svc-fusion-extension/app/app/main.py").read_text()
        ext_route = (backend / "services/svc-fusion-extension/app/app/api/routes/v3_scene_pricing.py").read_text()
        for suffix in ("preview", "reserve", "commit", "release"):
            full = f'/api/longform/v3/scene-pricing/{suffix}'
            if full not in director_pricing:
                raise RuntimeError(f"Director parent pricing contract missing {full}")
            if f'@router.post("/{suffix}"' not in ext_route:
                raise RuntimeError(f"Fusion Extension handler missing /{suffix}")
        if "app.include_router(v3_scene_pricing_router)" not in ext_main:
            raise RuntimeError("Fusion Extension main does not mount scene pricing router")
        if 'APIRouter(prefix="/api/longform/v3/scene-pricing"' not in ext_route:
            raise RuntimeError("Fusion Extension scene pricing prefix mismatch")
        print("DIRECTOR_EXTENSION_PARENT_PRICING_CONTRACT=PASS")
        print("PARENT_PRICING_LIFECYCLE=preview,reserve,commit,release")

        print("\n===== 3. BUILD FUSION EXTENSION API =====", flush=True)
        run(["docker", "tag", old_ext_id, ext_rollback])
        compose("build", "svc-fusion-extension")
        print("FUSION_EXTENSION_IMAGE_BUILD=PASS")

        print("\n===== 4. PRE-DEPLOY FUSION EXTENSION ROUTE PROOF =====", flush=True)
        probe = r'''
from app.main import app
routes={(m,getattr(r,'path','')) for r in app.routes for m in (getattr(r,'methods',None) or set())}
base='/api/longform/v3/scene-pricing/'
for suffix in ('preview','reserve','commit','release'):
    assert ('POST',base+suffix) in routes, (suffix,routes)
print('FUSION_EXTENSION_PARENT_PRICING_ROUTES_IMAGE=PASS')
'''
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-fusion-extension", input_text=probe)

        print("\n===== 5. REPLACE DEV FUSION EXTENSION API ONLY =====", flush=True)
        run(["docker", "rm", "-f", EXTENSION])
        extension_replaced = True
        compose("up", "-d", "--no-deps", "svc-fusion-extension")
        wait_http("http://127.0.0.1:18006/api/health", "fusion-extension")
        runtime_probe = r'''
from app.main import app
routes={(m,getattr(r,'path','')) for r in app.routes for m in (getattr(r,'methods',None) or set())}
base='/api/longform/v3/scene-pricing/'
for suffix in ('preview','reserve','commit','release'):
    assert ('POST',base+suffix) in routes
print('FUSION_EXTENSION_PARENT_PRICING_ROUTES_RUNTIME=PASS')
'''
        run(["docker", "exec", "-i", EXTENSION, "python", "-"], input_text=runtime_probe)

        director_network_probe = r'''
import json, urllib.error, urllib.request
body=json.dumps({
  'project_id':'00000000-0000-0000-0000-000000000001',
  'workflow_id':'00000000-0000-0000-0000-000000000002',
  'stage_run_id':'00000000-0000-0000-0000-000000000003',
}).encode()
for suffix in ('preview','commit'):
    req=urllib.request.Request(
      'http://df-v3-svc-fusion-extension:8006/api/longform/v3/scene-pricing/'+suffix,
      data=body,method='POST',headers={'Content-Type':'application/json'})
    try:
        urllib.request.urlopen(req,timeout=5); code=200
    except urllib.error.HTTPError as e:
        code=e.code
    assert code != 404, (suffix,code)
    assert code in {401,403,422}, (suffix,code)
print('DIRECTOR_TO_EXTENSION_PARENT_PRICING_NETWORK=PASS')
'''
        run(["docker", "exec", "-i", "df-v3-svc-director", "python", "-"], input_text=director_network_probe)

        print("\n===== 6. BUILD STAGE-LOCAL ASPECT WEB =====", flush=True)
        web_image = f"desifaces-web-dev:{WEB_REF[:13]}"
        run(["docker", "build", "-t", web_image, webrepo / "web"])
        run([
            "docker", "run", "--rm", "--entrypoint", "sh", web_image, "-lc",
            "grep -R -q 'Scene / Fusion format' /app/.next && "
            "grep -R -q 'Face format' /app/.next && "
            "grep -R -q 'MultiPersonStageAspectDock' /app/.next",
        ])
        print("WEB_STAGE_LOCAL_ASPECT_BUNDLE=PASS")

        print("\n===== 7. ISOLATED WEB CANDIDATE =====", flush=True)
        candidate_port = free_port()
        run_web(web_candidate, candidate_port, "no", web_image)
        candidate_started = True
        wait_http(f"http://127.0.0.1:{candidate_port}/auth/login", "web-candidate")
        print("WEB_CANDIDATE=PASS")

        print("\n===== 8. PERSISTENT DEV WEB CUTOVER =====", flush=True)
        run(["docker", "stop", WEB], check=False)
        run(["docker", "rename", WEB, web_rollback])
        run(["docker", "update", "--restart=no", web_rollback], check=False)
        web_swapped = True
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", WEB_PORT))
        finally:
            s.close()
        run_web(WEB, WEB_PORT, "unless-stopped", web_image)
        web_started = True
        wait_http("http://127.0.0.1:13000/auth/login", "persistent-dev-web")
        if field(WEB, '{{index .Config.Labels "desifaces.web_sha"}}') != WEB_REF:
            raise RuntimeError("persistent web revision label mismatch")
        print("DEV_WEB_STAGE_LOCAL_ASPECT_CUTOVER=PASS")

        print("\n===== 9. COMPLETE CROSS-SERVICE ROUTE MATRIX =====", flush=True)
        matrix = {
            "df-v3-svc-face": ["/api/face/assets/{media_asset_id}/read-url"],
            "df-v3-svc-audio": ["/api/audio/jobs/{job_id}/canonical-output", "/api/audio/assets/{media_id}/read-url"],
            "df-v3-svc-director": ["/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio"],
            EXTENSION: [
                "/api/longform/v3/scene-pricing/preview",
                "/api/longform/v3/scene-pricing/reserve",
                "/api/longform/v3/scene-pricing/commit",
                "/api/longform/v3/scene-pricing/release",
            ],
        }
        for container, paths in matrix.items():
            code = "from app.main import app; p={getattr(r,'path','') for r in app.routes}; required=" + repr(paths) + "; assert all(x in p for x in required)"
            cp = run(["docker", "exec", container, "python", "-c", code], check=False)
            if cp.returncode != 0:
                raise RuntimeError(f"route matrix failed: {container}")
        print("MULTIPERSON_CROSS_SERVICE_ROUTE_MATRIX=PASS")

        print("\n===== 10. NON-TARGET RUNTIME INVARIANTS =====", flush=True)
        for name, value in before.items():
            now = snapshot(name)
            if now != value:
                raise RuntimeError(f"non-target runtime changed: {name}")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("DB_REDIS_UNCHANGED=PASS")
        print("DIRECTOR_FACE_AUDIO_FUSION_UNCHANGED=PASS")
        print("STITCH_WORKER_UNCHANGED=PASS")

        if exists(web_candidate):
            run(["docker", "rm", "-f", web_candidate], check=False)
        candidate_started = False
        if exists(web_rollback):
            run(["docker", "rm", "-f", web_rollback], check=False)
        web_swapped = False
        run(["docker", "image", "rm", ext_rollback], check=False)
        success = True

        print("\n============================================================")
        print(" DEV MULTI-PERSON FUSION CONTRACT REPAIR PASS")
        print("============================================================")
        print("DIRECTOR_EXTENSION_PARENT_PRICING_CONTRACT=PASS")
        print("FUSION_EXTENSION_PARENT_PRICING_ROUTES_IMAGE=PASS")
        print("FUSION_EXTENSION_PARENT_PRICING_ROUTES_RUNTIME=PASS")
        print("DIRECTOR_TO_EXTENSION_PARENT_PRICING_NETWORK=PASS")
        print("WEB_STAGE_LOCAL_ASPECT_BUNDLE=PASS")
        print("WEB_CANDIDATE=PASS")
        print("DEV_WEB_STAGE_LOCAL_ASPECT_CUTOVER=PASS")
        print("MULTIPERSON_CROSS_SERVICE_ROUTE_MATRIX=PASS")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("PRODUCTION_TOUCH=NONE")
        print(f"old_web_image={old_web_image}")
        print(f"new_web_image={web_image}")
        print("NEXT=REFRESH_EXISTING_DEV_STORY_CHOOSE_16_9_AT_SCENE_PRODUCTION_AND_CHECK_PRICE")
        return 0
    except Exception as exc:
        print(f"DEV_FUSION_CONTRACT_REPAIR_FAIL={type(exc).__name__}:{exc}", flush=True)
        rollback()
        return 1
    finally:
        if not success and candidate_started and exists(web_candidate):
            run(["docker", "rm", "-f", web_candidate], check=False)
        if backend.exists():
            run(["git", "-C", BACKEND_ROOT, "worktree", "remove", "--force", backend], check=False)
        if webrepo.exists():
            run(["git", "-C", WEB_ROOT, "worktree", "remove", "--force", webrepo], check=False)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
