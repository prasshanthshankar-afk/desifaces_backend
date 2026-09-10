#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

EXPECTED_HOST = "desifaces-dev"
ROOT = Path("/home/azureuser/workspace/desifaces-v3")
BRANCH = "feature/v3-multiperson-core-20260818"
APP_REF = "0e46d6267e27d77bcae5613f9b0d8fd2d6ec2773"
NETWORK = "df-v3-net"

AUDIO = "df-v3-svc-audio"
DIRECTOR = "df-v3-svc-director"
WORKER = "df-v3-svc-director-worker"
DB = "desifaces-v3-db"

NON_TARGETS = (
    "desifaces-v3-db",
    "desifaces-v3-redis",
    "df-v3-svc-core",
    "df-v3-svc-pricing",
    "df-v3-svc-face",
    "df-v3-svc-face-worker",
    "df-v3-svc-audio-worker",
    "df-v3-svc-fusion",
    "df-v3-svc-fusion-worker",
    "df-v3-svc-fusion-extension",
    "df-v3-svc-fusion-extension-stitch-worker",
    "df-v3-web",
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
    return subprocess.run(
        ["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def field(name: str, template: str) -> str:
    cp = run(["docker", "inspect", name, "--format", template], capture=True, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def snapshot(name: str) -> str:
    return field(name, "{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.Config.Image}}|{{.RestartCount}}")


def wait_http(url: str, label: str, attempts: int = 45) -> None:
    for i in range(1, attempts + 1):
        cp = run(
            ["curl", "-sS", "--connect-timeout", "2", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture=True,
            check=False,
        )
        code = cp.stdout.strip() if cp.returncode == 0 else "0"
        print(f"wait={i} target={label} http={code}", flush=True)
        if code == "200":
            return
        time.sleep(2)
    raise RuntimeError(f"{label} did not become healthy")


def wait_worker(attempts: int = 45) -> None:
    for i in range(1, attempts + 1):
        status = field(WORKER, "{{.State.Status}}")
        health = field(WORKER, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}")
        print(f"wait={i} target=director-worker status={status or 'missing'} health={health or 'none'}", flush=True)
        if status == "running" and health in {"healthy", "none", "starting"}:
            if health != "starting" or i >= 3:
                return
        if status in {"exited", "dead"}:
            break
        time.sleep(1)
    raise RuntimeError("Director worker did not remain running")


def psql(sql: str) -> str:
    cp = run(
        [
            "docker", "exec", DB, "sh", "-lc",
            'psql -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            "--", sql,
        ],
        capture=True,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError("database query failed")
    return cp.stdout.strip()


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces V3 DEV — INTEGRITY HARDENING + CERTIFICATION")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("production_touch=FORBIDDEN")
    print(f"application_ref={APP_REF}")
    print("runtime_scope=Audio API + Director API + Director worker")
    print("provider_generation=NONE")
    print("db_schema_change=NONE")
    print("redis_change=NONE")
    print("resolved_environment_output=FORBIDDEN")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    if not (ROOT / ".git").exists():
        raise SystemExit(f"FAIL: dev Git checkout missing: {ROOT}")
    if not (ROOT / "infra/.env").is_file():
        raise SystemExit(f"FAIL: dev env missing: {ROOT / 'infra/.env'}")
    for name in NON_TARGETS + (AUDIO, DIRECTOR):
        if not exists(name):
            raise SystemExit(f"FAIL: required dev container missing: {name}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = Path("/home/azureuser/backups") / f"v3-integrity-hardening-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)

    tmp = Path(tempfile.mkdtemp(prefix="desifaces-v3-hardening-"))
    src = tmp / "source"
    audio_rollback = f"desifaces-dev-rollback-audio:{stamp}"
    director_rollback = f"desifaces-dev-rollback-director:{stamp}"
    audio_replaced = False
    director_replaced = False
    worker_replaced = False
    success = False
    non_target_before = {name: snapshot(name) for name in NON_TARGETS}

    def compose(*args, capture=False, input_text=None, check=True):
        return run(
            ["bash", src / "scripts/v3-compose.sh", *args],
            capture=capture,
            input_text=input_text,
            check=check,
        )

    def restore_apis() -> None:
        print("===== DEV AUTOMATIC ROLLBACK =====", flush=True)
        if worker_replaced and exists(WORKER):
            run(["docker", "rm", "-f", WORKER], check=False)
        if director_replaced:
            run(["docker", "rm", "-f", DIRECTOR], check=False)
            run(["docker", "tag", director_rollback, "desifaces-v3-svc-director"], check=False)
            compose("up", "-d", "--no-deps", "svc-director", check=False)
            try:
                wait_http("http://127.0.0.1:18011/api/health", "rollback-director", 30)
            except Exception:
                pass
        if audio_replaced:
            run(["docker", "rm", "-f", AUDIO], check=False)
            run(["docker", "tag", audio_rollback, "desifaces-v3-svc-audio"], check=False)
            compose("up", "-d", "--no-deps", "svc-audio", check=False)
            try:
                wait_http("http://127.0.0.1:18004/api/health", "rollback-audio", 30)
            except Exception:
                pass
        print("DEV_ROLLBACK=ATTEMPTED", flush=True)

    try:
        print("\n===== 1. MATERIALIZE IMMUTABLE HARDENED SOURCE =====", flush=True)
        run(["git", "-C", ROOT, "fetch", "--no-tags", "origin", BRANCH])
        run(["git", "-C", ROOT, "cat-file", "-e", f"{APP_REF}^{{commit}}"])
        run(["git", "-C", ROOT, "worktree", "add", "--detach", src, APP_REF])
        shutil.copy2(ROOT / "infra/.env", src / "infra/.env")
        os.chmod(src / "infra/.env", 0o600)
        print("IMMUTABLE_SOURCE=PASS")

        print("\n===== 2. STATIC HARDENING CONTRACT =====", flush=True)
        worker_text = (src / "services/svc-director/app/app/worker.py").read_text()
        store_text = (src / "services/svc-director/app/app/run_store.py").read_text()
        compose_text = (src / "docker-compose.v3.yml").read_text()
        audio_router = (src / "services/svc-audio/app/app/api/__init__.py").read_text()
        audit_text = (src / "scripts/audit-v3-dev-integrity-v3-20260910.py").read_text()
        assert "director_worker_transient_retry" in worker_text
        assert "requeue_transient" in worker_text and "requeue_transient" in store_text
        assert "svc-director-worker:" in compose_text
        assert compose_text.count("restart: unless-stopped") >= 2
        assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?V3 POSTGRES_PASSWORD is required}" in compose_text
        assert "v3_audio_output_router" in audio_router
        assert "READ-ONLY INTEGRITY AUDIT v3" in audit_text
        tracked = out(["git", "-C", src, "ls-files"])
        assert "services/svc-fusion/app/.venv/" not in tracked
        assert "RETIRED" in (src / "scripts/certify-multiperson-dev-20260909.py").read_text()
        assert "RETIRED" in (src / "scripts/recover-certify-director-worker-dev-20260910.sh").read_text()
        run([
            "python3", "-m", "py_compile",
            src / "services/svc-director/app/app/worker.py",
            src / "services/svc-director/app/app/run_store.py",
            src / "scripts/audit-v3-dev-integrity-v3-20260910.py",
        ])
        print("STATIC_HARDENING_CONTRACT=PASS")
        print("TRACKED_VENV_REMOVED=PASS")
        print("UNSAFE_LEGACY_SCRIPTS_RETIRED=PASS")
        print("V3_EFFECTIVE_SECRET_DEFAULTS=PASS")

        print("\n===== 3. CERTIFIED COMPOSE SERVICE INVENTORY — NAMES ONLY =====", flush=True)
        cp = compose(
            "--profile", "v3-orchestration", "--profile", "v3-execution",
            "config", "--services", capture=True,
        )
        services = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
        for required in ("svc-audio", "svc-director", "svc-director-worker"):
            if required not in services:
                raise RuntimeError(f"certified compose service missing: {required}")
        print(f"certified_service_count={len(services)}")
        print("CERTIFIED_COMPOSE_SERVICE_SET=PASS")

        print("\n===== 4. CREATE API IMAGE ROLLBACK CHECKPOINTS =====", flush=True)
        old_audio_id = field(AUDIO, "{{.Image}}")
        old_director_id = field(DIRECTOR, "{{.Image}}")
        if not old_audio_id or not old_director_id:
            raise RuntimeError("cannot resolve current API image IDs")
        run(["docker", "image", "inspect", old_audio_id], capture=True)
        run(["docker", "image", "inspect", old_director_id], capture=True)
        run(["docker", "tag", old_audio_id, audio_rollback])
        run(["docker", "tag", old_director_id, director_rollback])
        print("API_ROLLBACK_IMAGES=PASS")

        if exists(WORKER):
            # Preserve diagnostics without writing them to the terminal. The prior
            # worker is already exited, so rollback of runtime means returning to
            # no consumer rather than attempting to revive known-broken code.
            with (backup / "director-worker-before.log").open("w") as f:
                subprocess.run(["docker", "logs", "--tail", "300", WORKER], stdout=f, stderr=subprocess.STDOUT, text=True)
            os.chmod(backup / "director-worker-before.log", 0o600)

        print("\n===== 5. BUILD HARDENED AUDIO + DIRECTOR IMAGES =====", flush=True)
        compose("build", "svc-audio", "svc-director")
        print("HARDENED_IMAGES_BUILD=PASS")

        print("\n===== 6. PRE-DEPLOY IMAGE CONTRACT PROOF =====", flush=True)
        audio_probe = r'''
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/audio/jobs/{job_id}/canonical-output' in paths
assert '/api/audio/assets/{media_id}/read-url' in paths
print('AUDIO_CANONICAL_ROUTES_IMAGE=PASS')
'''
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-audio", input_text=audio_probe)

        director_probe = r'''
import socket
from app.main import app
from app.worker import _is_transient_infrastructure_error
from app.run_store import DirectorRunStore
paths={getattr(r,'path','') for r in app.routes}
assert '/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio' in paths
assert _is_transient_infrastructure_error(socket.gaierror(-3,'Temporary failure in name resolution'))
assert hasattr(DirectorRunStore,'requeue_transient')
print('DIRECTOR_ASPECT_ROUTE_IMAGE=PASS')
print('DIRECTOR_WORKER_TRANSIENT_RESILIENCE_IMAGE=PASS')
print('DIRECTOR_TRANSIENT_REQUEUE_IMAGE=PASS')
'''
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-director", input_text=director_probe)

        run([
            "docker", "run", "--rm", "--network", NETWORK,
            "--entrypoint", "python", "desifaces-v3-svc-director:latest", "-c",
            "import socket; socket.gethostbyname('desifaces-db'); s=socket.create_connection(('desifaces-db',5432),5); s.close(); print('DIRECTOR_IMAGE_TO_DB_NETWORK=PASS')",
        ])

        print("\n===== 7. CAPTURE EXISTING QUEUED RUN =====", flush=True)
        queued_run = psql(
            "select coalesce(run_id::text,'') from public.v3_director_runs "
            "where state='queued' and available_at<=now() and attempt_count<max_attempts "
            "order by created_at desc limit 1;"
        )
        print(f"queued_run_present={'YES' if queued_run else 'NO'}")

        print("\n===== 8. REPLACE AUDIO API ONLY =====", flush=True)
        run(["docker", "rm", "-f", AUDIO])
        audio_replaced = True
        compose("up", "-d", "--no-deps", "svc-audio")
        wait_http("http://127.0.0.1:18004/api/health", "audio")
        route_check = run([
            "docker", "exec", AUDIO, "python", "-c",
            "from app.main import app; p={getattr(r,'path','') for r in app.routes}; assert '/api/audio/jobs/{job_id}/canonical-output' in p and '/api/audio/assets/{media_id}/read-url' in p",
        ], check=False)
        if route_check.returncode != 0:
            raise RuntimeError("Audio canonical routes absent after replacement")
        print("AUDIO_RUNTIME_ROUTES=PASS")

        print("\n===== 9. REPLACE DIRECTOR API ONLY =====", flush=True)
        run(["docker", "rm", "-f", DIRECTOR])
        director_replaced = True
        compose("up", "-d", "--no-deps", "svc-director")
        wait_http("http://127.0.0.1:18011/api/health", "director")
        if field(DIRECTOR, "{{.HostConfig.RestartPolicy.Name}}") != "unless-stopped":
            raise RuntimeError("Director API restart policy did not converge")
        print("DIRECTOR_API_RUNTIME=PASS")

        print("\n===== 10. REPLACE DIRECTOR WORKER ONLY =====", flush=True)
        if exists(WORKER):
            run(["docker", "rm", "-f", WORKER])
        compose("--profile", "v3-orchestration", "up", "-d", "--no-deps", "svc-director-worker")
        worker_replaced = True
        wait_worker()
        if field(WORKER, "{{.HostConfig.RestartPolicy.Name}}") != "unless-stopped":
            raise RuntimeError("Director worker restart policy did not converge")
        if field(WORKER, "{{.Image}}") != field(DIRECTOR, "{{.Image}}"):
            raise RuntimeError("Director API/worker image parity failed")
        print("DIRECTOR_WORKER_RUNTIME=PASS")
        print("DIRECTOR_API_WORKER_IMAGE_PARITY=PASS")

        print("\n===== 11. VERIFY DURABLE QUEUE CONSUMPTION =====", flush=True)
        if queued_run:
            state = "queued"
            attempts = "0"
            error = ""
            for i in range(1, 91):
                row = psql(
                    "select coalesce(state,'')||'|'||coalesce(attempt_count::text,'0')||'|'||coalesce(last_error,'') "
                    f"from public.v3_director_runs where run_id='{queued_run}'::uuid;"
                )
                state, attempts, error = (row.split("|", 2) + ["", ""])[:3]
                print(f"queue_check={i} state={state} attempts={attempts}", flush=True)
                if state in {"running", "awaiting_review", "ready"}:
                    break
                if state == "failed":
                    raise RuntimeError(f"Director queued run failed: {error[:700]}")
                time.sleep(1)
            if state not in {"running", "awaiting_review", "ready"}:
                raise RuntimeError("Director queued run was not consumed within 90 seconds")
            print(f"DIRECTOR_QUEUE_CONSUMER=PASS state={state}")
        else:
            print("DIRECTOR_QUEUE_CONSUMER=PASS no_eligible_queue")

        print("\n===== 12. SYNCHRONIZE LOCAL OPERATIONAL FILES =====", flush=True)
        for rel in ("docker-compose.yml", "docker-compose.v3.yml", "scripts/v3-compose.sh"):
            local = ROOT / rel
            source_file = src / rel
            if local.exists():
                dest = backup / rel.replace("/", "__")
                shutil.copy2(local, dest)
                os.chmod(dest, 0o600)
            shutil.copy2(source_file, local)
        os.chmod(ROOT / "scripts/v3-compose.sh", 0o755)
        local_services = out([
            "bash", ROOT / "scripts/v3-compose.sh",
            "--profile", "v3-orchestration", "--profile", "v3-execution",
            "config", "--services",
        ])
        if "svc-director-worker" not in set(local_services.splitlines()):
            raise RuntimeError("local operational Compose still cannot address Director worker")
        print("LOCAL_OPERATIONAL_FILES_SYNCED=PASS")
        print(f"local_backup={backup}")

        print("\n===== 13. NON-TARGET RUNTIME INVARIANTS =====", flush=True)
        for name, before in non_target_before.items():
            after = snapshot(name)
            if after != before:
                raise RuntimeError(f"non-target runtime changed: {name}")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("DB_REDIS_UNCHANGED=PASS")
        print("FACE_FUSION_WORKERS_WEB_UNCHANGED=PASS")

        print("\n===== 14. READ-ONLY INTEGRITY AUDIT v3 =====", flush=True)
        audit = run(
            ["python3", src / "scripts/audit-v3-dev-integrity-v3-20260910.py", "--source-ref", APP_REF],
            capture=True,
            check=False,
        )
        print(audit.stdout, end="" if audit.stdout.endswith("\n") else "\n")
        if audit.returncode != 0:
            raise RuntimeError(f"post-hardening integrity audit failed rc={audit.returncode}")
        print("POST_HARDENING_AUDIT=PASS")

        success = True
        run(["docker", "image", "rm", audio_rollback], check=False)
        run(["docker", "image", "rm", director_rollback], check=False)

        print("\n============================================================")
        print(" V3 DEV INTEGRITY HARDENING PASS")
        print("============================================================")
        print(f"application_ref={APP_REF}")
        print("STATIC_HARDENING_CONTRACT=PASS")
        print("CERTIFIED_COMPOSE_SERVICE_SET=PASS")
        print("AUDIO_CANONICAL_ROUTES_IMAGE=PASS")
        print("AUDIO_RUNTIME_ROUTES=PASS")
        print("DIRECTOR_WORKER_TRANSIENT_RESILIENCE_IMAGE=PASS")
        print("DIRECTOR_TRANSIENT_REQUEUE_IMAGE=PASS")
        print("DIRECTOR_API_RUNTIME=PASS")
        print("DIRECTOR_WORKER_RUNTIME=PASS")
        print("DIRECTOR_API_WORKER_IMAGE_PARITY=PASS")
        print("DIRECTOR_QUEUE_CONSUMER=PASS")
        print("LOCAL_OPERATIONAL_FILES_SYNCED=PASS")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("POST_HARDENING_AUDIT=PASS")
        print("PRODUCTION_TOUCH=NONE")
        print("NEXT=REFRESH_DEV_MULTI_PERSON_AND_COMPLETE_FACE_AUDIO_FUSION_BROWSER_ACCEPTANCE")
        return 0

    except Exception as exc:
        print(f"\nV3_DEV_HARDENING_FAIL={type(exc).__name__}:{exc}", file=sys.stderr, flush=True)
        try:
            restore_apis()
        except Exception as rollback_exc:
            print(f"V3_DEV_ROLLBACK_ERROR={rollback_exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if src.exists():
            run(["git", "-C", ROOT, "worktree", "remove", "--force", src], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        if success:
            print(f"evidence_backup={backup}")


if __name__ == "__main__":
    raise SystemExit(main())
