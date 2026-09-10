#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

EXPECTED_HOST = "desifaces-dev"
ROOT = Path("/home/azureuser/workspace/desifaces-v3")
NETWORK = "df-v3-net"
WEB_SHA = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"


def run(args, *, input_text=None, check=True):
    return subprocess.run(
        [str(x) for x in args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def out(args, *, input_text=None):
    return run(args, input_text=input_text).stdout.strip()


def exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def field(name: str, template: str) -> str:
    cp = run(["docker", "inspect", name, "--format", template], check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def container_sha(name: str, path: str) -> str | None:
    if not exists(name) or field(name, "{{.State.Status}}") != "running":
        return None
    cp = run(["docker", "exec", name, "sha256sum", path], check=False)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    return cp.stdout.split()[0]


def psql(sql: str) -> str:
    cp = run(
        [
            "docker", "exec", "desifaces-v3-db", "sh", "-lc",
            'psql -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
            "--", sql,
        ],
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError("database query failed")
    return cp.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()

    critical: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    passes: list[str] = []

    def ok(code: str):
        passes.append(code)
        print(f"PASS  {code}")

    def crit(code: str, msg: str):
        critical.append((code, msg))
        print(f"CRIT  {code}: {msg}")

    def warn(code: str, msg: str):
        warnings.append((code, msg))
        print(f"WARN  {code}: {msg}")

    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces V3 DEV — READ-ONLY INTEGRITY AUDIT v3")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("runtime_mutations=NONE")
    print("container_builds=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")
    print("secret_values_output=FORBIDDEN")
    print(f"source_ref={args.source_ref}")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    if not (ROOT / ".git").exists():
        raise SystemExit(f"FAIL: dev checkout missing: {ROOT}")
    if not (ROOT / "infra/.env").is_file():
        raise SystemExit(f"FAIL: dev env missing: {ROOT / 'infra/.env'}")

    tmp = Path(tempfile.mkdtemp(prefix="desifaces-v3-audit-v3-"))
    src = tmp / "source"
    try:
        print("\n===== 1. IMMUTABLE SOURCE =====")
        fetch = run(["git", "-C", ROOT, "fetch", "--no-tags", "origin"], check=False)
        if fetch.returncode != 0:
            crit("GIT_FETCH_FAILED", "cannot refresh Git object database")
            return 2
        if run(["git", "-C", ROOT, "cat-file", "-e", f"{args.source_ref}^{{commit}}"], check=False).returncode != 0:
            crit("SOURCE_REF_MISSING", args.source_ref)
            return 2
        run(["git", "-C", ROOT, "worktree", "add", "--detach", src, args.source_ref])
        shutil.copy2(ROOT / "infra/.env", src / "infra/.env")
        ok("IMMUTABLE_SOURCE_MATERIALIZED")

        print("\n===== 2. LOCAL OPERATIONAL FILE ALIGNMENT =====")
        for rel in ("docker-compose.yml", "docker-compose.v3.yml", "scripts/v3-compose.sh"):
            local = ROOT / rel
            pinned = src / rel
            if not local.is_file():
                crit("LOCAL_OPERATIONAL_FILE_MISSING", rel)
            elif sha256_file(local) == sha256_file(pinned):
                ok("LOCAL_MATCH_" + rel.replace("/", "_").replace(".", "_").upper())
            else:
                warn("LOCAL_OPERATIONAL_FILE_DRIFT", f"{rel}; immutable launchers remain authoritative")
        dirty = out(["git", "-C", ROOT, "status", "--porcelain"])
        print(f"local_dirty_entries={len([x for x in dirty.splitlines() if x.strip()])}")
        if dirty:
            warn("LOCAL_CHECKOUT_DIRTY", "do not use mutable checkout as deployment source")

        print("\n===== 3. CERTIFIED COMPOSE SERVICE SET =====")
        cp = run(
            [
                "bash", src / "scripts/v3-compose.sh",
                "--profile", "v3-orchestration", "--profile", "v3-execution",
                "config", "--services",
            ],
            check=False,
        )
        if cp.returncode != 0:
            crit("CERTIFIED_COMPOSE_RESOLUTION_FAILED", "service-only Compose resolution failed")
            services: set[str] = set()
        else:
            services = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
            required = {
                "desifaces-db", "desifaces-redis", "svc-core", "svc-pricing",
                "svc-face", "svc-face-worker", "svc-audio", "svc-audio-worker",
                "svc-fusion", "svc-fusion-worker", "svc-fusion-extension",
                "svc-fusion-extension-stitch-worker", "svc-director", "svc-director-worker",
            }
            missing = sorted(required - services)
            if missing:
                crit("CERTIFIED_COMPOSE_SERVICES_MISSING", ",".join(missing))
            else:
                ok("CERTIFIED_COMPOSE_SERVICE_SET")
            print(f"certified_service_count={len(services)}")

        print("\n===== 4. REQUIRED RUNTIME INVENTORY =====")
        required_containers = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-core", "df-v3-svc-pricing",
            "df-v3-svc-face", "df-v3-svc-face-worker", "df-v3-svc-audio", "df-v3-svc-audio-worker",
            "df-v3-svc-fusion", "df-v3-svc-fusion-worker", "df-v3-svc-fusion-extension",
            "df-v3-svc-fusion-extension-stitch-worker", "df-v3-svc-director", "df-v3-svc-director-worker",
            "df-v3-web",
        ]
        inventory: dict[str, dict[str, str]] = {}
        for name in required_containers:
            if not exists(name):
                crit("REQUIRED_CONTAINER_MISSING", name)
                continue
            info = {
                "status": field(name, "{{.State.Status}}"),
                "health": field(name, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"),
                "exit": field(name, "{{.State.ExitCode}}"),
                "restart": field(name, "{{.HostConfig.RestartPolicy.Name}}"),
                "image_id": field(name, "{{.Image}}"),
                "image_ref": field(name, "{{.Config.Image}}"),
            }
            inventory[name] = info
            print(
                f"container={name} status={info['status']} health={info['health']} "
                f"exit={info['exit']} restart_policy={info['restart']} image={info['image_ref']}"
            )
            if info["status"] != "running":
                crit("REQUIRED_CONTAINER_NOT_RUNNING", f"{name}:{info['status']}:exit={info['exit']}")
            if name in {"df-v3-svc-director", "df-v3-svc-director-worker"} and info["restart"] not in {"always", "unless-stopped"}:
                crit("DIRECTOR_RESTART_POLICY_WEAK", f"{name}:{info['restart'] or 'none'}")

        print("\n===== 5. DIRECTOR IMAGE + NETWORK PARITY =====")
        dapi = inventory.get("df-v3-svc-director")
        dworker = inventory.get("df-v3-svc-director-worker")
        if dapi and dworker and dapi["image_id"] == dworker["image_id"]:
            ok("DIRECTOR_API_WORKER_IMAGE_PARITY")
        else:
            crit("DIRECTOR_API_WORKER_IMAGE_DRIFT", "API and worker must use one certified image")

        network_required = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-pricing", "df-v3-svc-face",
            "df-v3-svc-audio", "df-v3-svc-fusion", "df-v3-svc-fusion-extension",
            "df-v3-svc-director", "df-v3-svc-director-worker", "df-v3-web",
        ]
        network_bad = False
        for name in network_required:
            if not exists(name):
                continue
            nets = field(name, "{{json .NetworkSettings.Networks}}")
            if NETWORK not in nets:
                network_bad = True
                crit("NETWORK_MEMBERSHIP_MISSING", f"{name}:{NETWORK}")
        if not network_bad:
            ok("V3_NETWORK_MEMBERSHIP")

        if dapi and dapi["status"] == "running":
            probe = r'''
import socket
for host,port in [
 ('desifaces-db',5432),('desifaces-redis',6379),('svc-pricing',8009),
 ('svc-face',8003),('svc-audio',8004),('svc-fusion',8002),('svc-fusion-extension',8006)
]:
    socket.gethostbyname(host)
    with socket.create_connection((host,port),timeout=3): pass
print('PASS')
'''
            live = run(["docker", "exec", "-i", "df-v3-svc-director", "python", "-"], input_text=probe, check=False)
            if live.returncode == 0 and "PASS" in live.stdout:
                ok("DIRECTOR_LIVE_DNS_TCP")
            else:
                crit("DIRECTOR_LIVE_DNS_TCP_FAILED", "internal dependency DNS/TCP failed")

        print("\n===== 6. DURABLE QUEUE =====")
        try:
            counts = psql("select state||':'||count(*) from public.v3_director_runs group by state order by state;")
            eligible = int(psql("select count(*) from public.v3_director_runs where state='queued' and available_at<=now() and attempt_count<max_attempts;") or "0")
            stale = int(psql("select count(*) from public.v3_director_runs where state='running' and lease_expires_at is not null and lease_expires_at<now();") or "0")
            print("queue_state_counts=" + (counts.replace("\n", ",") if counts else "none"))
            print(f"eligible_queued={eligible}")
            print(f"stale_running={stale}")
            worker_running = bool(dworker and dworker["status"] == "running")
            if eligible and not worker_running:
                crit("DIRECTOR_QUEUE_WITHOUT_CONSUMER", f"eligible_queued={eligible}")
            elif eligible:
                warn("DIRECTOR_QUEUE_ACTIVE", f"eligible_queued={eligible}; worker is running")
            else:
                ok("DIRECTOR_QUEUE_NO_ORPHANED_ELIGIBLE_WORK")
            if stale:
                crit("DIRECTOR_STALE_RUNNING_LEASES", f"stale_running={stale}")
            else:
                ok("DIRECTOR_NO_STALE_RUNNING_LEASES")
        except Exception:
            crit("DIRECTOR_QUEUE_AUDIT_FAILED", "unable to read durable queue")

        print("\n===== 7. CERTIFIED SOURCE vs RUNNING HASHES =====")
        mappings = {
            "df-v3-svc-director": [
                ("services/svc-director/app/app/main.py", "/app/app/main.py"),
                ("services/svc-director/app/app/worker.py", "/app/app/worker.py"),
                ("services/svc-director/app/app/run_store.py", "/app/app/run_store.py"),
                ("services/svc-director/app/app/audio_execution_runtime.py", "/app/app/audio_execution_runtime.py"),
                ("services/svc-director/app/app/face_execution_runtime.py", "/app/app/face_execution_runtime.py"),
                ("services/svc-director/app/app/fusion_input_performance.py", "/app/app/fusion_input_performance.py"),
                ("services/svc-director/app/app/studio_aspect_routes.py", "/app/app/studio_aspect_routes.py"),
            ],
            "df-v3-svc-audio": [
                ("services/svc-audio/app/app/api/__init__.py", "/app/app/api/__init__.py"),
                ("services/svc-audio/app/app/api/routes/v3_audio_output.py", "/app/app/api/routes/v3_audio_output.py"),
            ],
            "df-v3-svc-face": [
                ("services/svc-face/app/app/api/routes/face_media.py", "/app/app/api/routes/face_media.py"),
            ],
            "df-v3-svc-fusion": [
                ("services/svc-fusion/app/app/domain/enums.py", "/app/app/domain/enums.py"),
            ],
        }
        for name, pairs in mappings.items():
            for rel, inside in pairs:
                actual = container_sha(name, inside)
                expected = sha256_file(src / rel)
                if actual == expected:
                    ok("SOURCE_HASH_" + name.replace("-", "_") + "_" + Path(rel).name.replace(".", "_"))
                else:
                    crit("RUNTIME_SOURCE_DRIFT", f"{name}:{rel}")

        # Worker uses the same image as Director API, but verify its worker module directly too.
        worker_hash = container_sha("df-v3-svc-director-worker", "/app/app/worker.py")
        if worker_hash == sha256_file(src / "services/svc-director/app/app/worker.py"):
            ok("SOURCE_HASH_DIRECTOR_WORKER")
        else:
            crit("DIRECTOR_WORKER_SOURCE_DRIFT", "worker.py differs from certified source")

        print("\n===== 8. REQUIRED ROUTES =====")
        route_checks = [
            ("df-v3-svc-face", "/api/face/assets/{media_asset_id}/read-url"),
            ("df-v3-svc-audio", "/api/audio/jobs/{job_id}/canonical-output"),
            ("df-v3-svc-audio", "/api/audio/assets/{media_id}/read-url"),
            ("df-v3-svc-director", "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio"),
        ]
        for name, route in route_checks:
            code = "from app.main import app; import sys; sys.exit(0 if " + repr(route) + " in {getattr(r,'path','') for r in app.routes} else 7)"
            cp = run(["docker", "exec", name, "python", "-c", code], check=False) if exists(name) else None
            if cp is not None and cp.returncode == 0:
                ok("ROUTE_PRESENT_" + name.replace("-", "_") + "_" + str(route_checks.index((name, route))))
            else:
                crit("REQUIRED_ROUTE_MISSING", f"{name}:{route}")

        fusion_enum = (src / "services/svc-fusion/app/app/domain/enums.py").read_text()
        if all(x in fusion_enum for x in ('"16:9"', '"9:16"', '"1:1"')):
            ok("FUSION_ASPECT_9_16_16_9_1_1")
        else:
            crit("FUSION_ASPECT_SET_INCOMPLETE", "expected 9:16,16:9,1:1")

        print("\n===== 9. DEV WEB =====")
        web = inventory.get("df-v3-web")
        if web and web["status"] == "running":
            binding = field("df-v3-web", "{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostIp}}:{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostPort}}")
            print(f"web_binding={binding or 'unknown'}")
            if binding == "127.0.0.1:13000":
                ok("DEV_WEB_LOOPBACK_13000")
            else:
                crit("DEV_WEB_BINDING_DRIFT", binding or "unknown")
            curl = run(["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:13000/auth/login"], check=False)
            if curl.returncode == 0 and "desifaces" in curl.stdout.lower():
                ok("DEV_WEB_HTTP")
            else:
                crit("DEV_WEB_HTTP_FAILED", "login route unhealthy")
            label = field("df-v3-web", '{{index .Config.Labels "desifaces.web_sha"}}')
            if label == WEB_SHA:
                ok("DEV_WEB_REVISION_LABEL")
            else:
                warn("DEV_WEB_REVISION_LABEL", f"label={label or 'none'} expected={WEB_SHA[:12]}")

        print("\n===== 10. RESILIENCE + RELEASE HYGIENE =====")
        worker_text = (src / "services/svc-director/app/app/worker.py").read_text()
        store_text = (src / "services/svc-director/app/app/run_store.py").read_text()
        compose_v3 = (src / "docker-compose.v3.yml").read_text()
        if "director_worker_transient_retry" in worker_text and "requeue_transient" in worker_text and "requeue_transient" in store_text:
            ok("DIRECTOR_WORKER_TRANSIENT_RESILIENCE")
        else:
            crit("DIRECTOR_WORKER_TRANSIENT_RESILIENCE_MISSING", "supervisor/requeue contract incomplete")
        if compose_v3.count("restart: unless-stopped") >= 2 and "svc-director-worker:" in compose_v3:
            ok("DIRECTOR_RESTART_POLICY_SOURCE")
        else:
            crit("DIRECTOR_RESTART_POLICY_SOURCE_MISSING", "Director API/worker must restart unless-stopped")

        # Effective V3 overlay explicitly requires the DB password; a legacy fallback
        # in the generic base Compose is therefore not an effective V3 secret default.
        if "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?V3 POSTGRES_PASSWORD is required}" in compose_v3:
            ok("V3_POSTGRES_PASSWORD_REQUIRED")
        else:
            crit("V3_POSTGRES_PASSWORD_NOT_REQUIRED", "V3 overlay must fail closed without POSTGRES_PASSWORD")

        tracked = out(["git", "-C", src, "ls-files"]).splitlines()
        if "infra/.env" in tracked:
            crit("ENV_TRACKED", "infra/.env must remain untracked")
        else:
            ok("ENV_NOT_TRACKED")
        venv_paths = [p for p in tracked if "/.venv/" in p or p.startswith(".venv/")]
        if venv_paths:
            crit("TRACKED_VIRTUAL_ENVIRONMENT", f"tracked_entries={len(venv_paths)}")
        else:
            ok("NO_TRACKED_VIRTUAL_ENVIRONMENT")

        secret_hits: set[str] = set()
        patterns = [
            re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
            re.compile(r"AccountKey=[A-Za-z0-9+/=]{24,}"),
            re.compile(r"whsec_[A-Za-z0-9]{20,}"),
            re.compile(r"sk_live_[A-Za-z0-9]{20,}"),
        ]
        for rel in tracked:
            if "/site-packages/" in rel or "/.venv/" in rel:
                continue
            p = src / rel
            if not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            if any(rx.search(text) for rx in patterns):
                secret_hits.add(rel)
        if secret_hits:
            crit("TRACKED_SECRET_LIKE_CONTENT", "files=" + ",".join(sorted(secret_hits)))
        else:
            ok("NO_TRACKED_SECRET_LIKE_CONTENT")

        retired_cert = (src / "scripts/certify-multiperson-dev-20260909.py").read_text()
        retired_recovery = (src / "scripts/recover-certify-director-worker-dev-20260910.sh").read_text()
        if "RETIRED" in retired_cert and "RETIRED" in retired_recovery:
            ok("UNSAFE_LEGACY_OPERATIONS_RETIRED")
        else:
            crit("UNSAFE_LEGACY_OPERATIONS_ACTIVE", "legacy mutable/secret-emitting scripts still executable")

        print("\n============================================================")
        print(" V3 DEV INTEGRITY AUDIT v3 SUMMARY")
        print("============================================================")
        print(f"PASS_COUNT={len(passes)}")
        print(f"WARNING_COUNT={len(warnings)}")
        print(f"CRITICAL_COUNT={len(critical)}")
        print("RUNTIME_MUTATIONS=NONE")
        print("PRODUCTION_TOUCH=NONE")
        if warnings:
            print("WARNINGS=")
            for code, msg in warnings:
                print(f"  - {code}: {msg}")
        if critical:
            print("CRITICALS=")
            for code, msg in critical:
                print(f"  - {code}: {msg}")
            print("OVERALL=BLOCKED")
            return 2
        if warnings:
            print("OVERALL=PASS_WITH_WARNINGS")
            return 0
        print("OVERALL=PASS")
        return 0
    finally:
        if src.exists():
            run(["git", "-C", ROOT, "worktree", "remove", "--force", src], check=False)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
