#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

EXPECTED_HOST = "desifaces-dev"
ROOT = Path("/home/azureuser/workspace/desifaces-v3")
BRANCH = "feature/v3-multiperson-core-20260818"
BASELINE = "89e30d3fab49db0231548c1b72dc3720ad032e87"
NETWORK = "df-v3-net"
WEB_SHA = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"

CRITICAL: list[tuple[str, str]] = []
WARNING: list[tuple[str, str]] = []
PASSES: list[str] = []


def run(args, *, cwd=None, input_text=None, check=True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(x) for x in args],
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def output(args, *, cwd=None, input_text=None) -> str:
    return run(args, cwd=cwd, input_text=input_text).stdout.strip()


def pass_(code: str) -> None:
    PASSES.append(code)
    print(f"PASS  {code}")


def crit(code: str, message: str) -> None:
    CRITICAL.append((code, message))
    print(f"CRIT  {code}: {message}")


def warn(code: str, message: str) -> None:
    WARNING.append((code, message))
    print(f"WARN  {code}: {message}")


def exists(container: str) -> bool:
    return subprocess.run(
        ["docker", "inspect", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def inspect_field(container: str, template: str) -> str:
    cp = run(["docker", "inspect", container, "--format", template], check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def container_sha(container: str, path: str) -> str | None:
    if not exists(container):
        return None
    status = inspect_field(container, "{{.State.Status}}")
    if status != "running":
        return None
    cp = run(["docker", "exec", container, "sha256sum", path], check=False)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    return cp.stdout.split()[0]


def psql_query(sql: str) -> str:
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


def runtime_inventory(container: str, required: bool) -> dict[str, str] | None:
    if not exists(container):
        if required:
            crit("RUNTIME_CONTAINER_MISSING", container)
        else:
            warn("OPTIONAL_CONTAINER_MISSING", container)
        return None
    fields = {
        "status": inspect_field(container, "{{.State.Status}}"),
        "health": inspect_field(container, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"),
        "exit": inspect_field(container, "{{.State.ExitCode}}"),
        "restart_count": inspect_field(container, "{{.RestartCount}}"),
        "restart_policy": inspect_field(container, "{{.HostConfig.RestartPolicy.Name}}"),
        "image_id": inspect_field(container, "{{.Image}}"),
        "image_ref": inspect_field(container, "{{.Config.Image}}"),
    }
    print(
        f"container={container} status={fields['status']} health={fields['health']} "
        f"exit={fields['exit']} restarts={fields['restart_count']} "
        f"restart_policy={fields['restart_policy']} image={fields['image_ref']}"
    )
    if required and fields["status"] != "running":
        crit("REQUIRED_CONTAINER_NOT_RUNNING", f"{container} status={fields['status']} exit={fields['exit']}")
    if required and fields["restart_policy"] not in {"always", "unless-stopped"}:
        crit("RESTART_POLICY_WEAK", f"{container} restart_policy={fields['restart_policy'] or 'none'}")
    return fields


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces V3 DEV — READ-ONLY INTEGRITY AUDIT")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("runtime_mutations=NONE")
    print("production_touch=NONE")
    print("secrets_output=FORBIDDEN")
    print(f"immutable_source={BASELINE}")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    if not (ROOT / ".git").exists():
        raise SystemExit(f"FAIL: dev checkout missing: {ROOT}")
    if not (ROOT / "infra/.env").is_file():
        raise SystemExit(f"FAIL: dev env missing: {ROOT / 'infra/.env'}")

    run_dir = Path(tempfile.mkdtemp(prefix="desifaces-v3-integrity-audit-"))
    source = run_dir / "source"
    try:
        print("\n===== 1. IMMUTABLE SOURCE + LOCAL CHECKOUT ALIGNMENT =====")
        fetch = run(["git", "-C", ROOT, "fetch", "--no-tags", "origin", BRANCH], check=False)
        if fetch.returncode != 0:
            crit("GIT_FETCH_FAILED", "could not refresh dev branch metadata")
            return 2
        if run(["git", "-C", ROOT, "cat-file", "-e", f"{BASELINE}^{{commit}}"], check=False).returncode != 0:
            crit("BASELINE_MISSING", BASELINE)
            return 2
        run(["git", "-C", ROOT, "worktree", "add", "--detach", source, BASELINE])
        shutil.copy2(ROOT / "infra/.env", source / "infra/.env")
        os.chmod(source / "infra/.env", 0o600)
        pass_("IMMUTABLE_SOURCE_AVAILABLE")

        local_head = output(["git", "-C", ROOT, "rev-parse", "HEAD"])
        local_branch = output(["git", "-C", ROOT, "branch", "--show-current"])
        dirty = output(["git", "-C", ROOT, "status", "--porcelain"])
        print(f"local_branch={local_branch or 'DETACHED'}")
        print(f"local_head={local_head}")
        print(f"local_dirty_entries={len([x for x in dirty.splitlines() if x.strip()])}")
        if local_head != BASELINE:
            warn("LOCAL_HEAD_DIFFERS_FROM_CERTIFIED_BASELINE", f"local={local_head[:12]} certified={BASELINE[:12]}")
        if dirty.strip():
            warn("LOCAL_CHECKOUT_DIRTY", "operational scripts must not depend on this working tree")

        for rel in ("docker-compose.yml", "docker-compose.v3.yml", "scripts/v3-compose.sh"):
            local = ROOT / rel
            pinned = source / rel
            if not local.is_file():
                crit("LOCAL_OPERATIONAL_FILE_MISSING", rel)
            elif sha256_file(local) != sha256_file(pinned):
                crit("LOCAL_COMPOSE_DRIFT", rel)
            else:
                pass_(f"LOCAL_MATCH_{rel.replace('/', '_').replace('.', '_')}")

        print("\n===== 2. COMPOSE SERVICE INVENTORY — NAMES ONLY =====")
        cmd = [
            "bash", source / "scripts/v3-compose.sh",
            "--profile", "v3-orchestration", "--profile", "v3-execution",
            "config", "--services",
        ]
        cp = run(cmd, check=False)
        if cp.returncode != 0:
            crit("IMMUTABLE_COMPOSE_RESOLUTION_FAILED", "pinned compose could not resolve service names")
            immutable_services: set[str] = set()
        else:
            immutable_services = {x.strip() for x in cp.stdout.splitlines() if x.strip()}
            required_services = {
                "desifaces-db", "desifaces-redis", "svc-core", "svc-pricing",
                "svc-face", "svc-face-worker", "svc-audio", "svc-audio-worker",
                "svc-fusion", "svc-fusion-worker", "svc-fusion-extension",
                "svc-fusion-extension-stitch-worker", "svc-director", "svc-director-worker",
            }
            missing = sorted(required_services - immutable_services)
            if missing:
                crit("IMMUTABLE_COMPOSE_SERVICES_MISSING", ",".join(missing))
            else:
                pass_("IMMUTABLE_COMPOSE_SERVICE_SET")
            print("immutable_service_count=" + str(len(immutable_services)))

        local_cmd = [
            "bash", ROOT / "scripts/v3-compose.sh",
            "--profile", "v3-orchestration", "--profile", "v3-execution",
            "config", "--services",
        ]
        cp_local = run(local_cmd, check=False)
        if cp_local.returncode != 0:
            crit("LOCAL_COMPOSE_RESOLUTION_FAILED", "local V3 compose cannot resolve service inventory")
        else:
            local_services = {x.strip() for x in cp_local.stdout.splitlines() if x.strip()}
            if "svc-director-worker" not in local_services:
                crit("LOCAL_DIRECTOR_WORKER_SERVICE_MISSING", "local compose does not define svc-director-worker")
            elif immutable_services and local_services != immutable_services:
                crit("LOCAL_IMMUTABLE_SERVICE_SET_DRIFT", "local and certified service inventories differ")
            else:
                pass_("LOCAL_COMPOSE_SERVICE_SET")

        print("\n===== 3. REQUIRED MULTI-PERSON RUNTIME INVENTORY =====")
        required_containers = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-core", "df-v3-svc-pricing",
            "df-v3-svc-face", "df-v3-svc-face-worker", "df-v3-svc-audio", "df-v3-svc-audio-worker",
            "df-v3-svc-fusion", "df-v3-svc-fusion-worker", "df-v3-svc-fusion-extension",
            "df-v3-svc-fusion-extension-stitch-worker", "df-v3-svc-director", "df-v3-svc-director-worker",
            "df-v3-web",
        ]
        inventory = {c: runtime_inventory(c, True) for c in required_containers}

        print("\n===== 4. IMAGE PARITY =====")
        dapi = inventory.get("df-v3-svc-director")
        dworker = inventory.get("df-v3-svc-director-worker")
        if dapi and dworker:
            if dapi["image_id"] != dworker["image_id"]:
                crit("DIRECTOR_API_WORKER_IMAGE_DRIFT", "Director API and worker are not running the same image")
            else:
                pass_("DIRECTOR_API_WORKER_IMAGE_PARITY")

        print("\n===== 5. V3 NETWORK MEMBERSHIP + DNS/TCP =====")
        network_required = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-pricing", "df-v3-svc-face",
            "df-v3-svc-audio", "df-v3-svc-fusion", "df-v3-svc-fusion-extension",
            "df-v3-svc-director", "df-v3-svc-director-worker", "df-v3-web",
        ]
        for c in network_required:
            if not exists(c):
                continue
            networks = inspect_field(c, "{{json .NetworkSettings.Networks}}")
            if NETWORK not in networks:
                crit("NETWORK_MEMBERSHIP_MISSING", f"{c} not attached to {NETWORK}")
        if not any(code == "NETWORK_MEMBERSHIP_MISSING" for code, _ in CRITICAL):
            pass_("V3_NETWORK_MEMBERSHIP")

        if exists("df-v3-svc-director") and inspect_field("df-v3-svc-director", "{{.State.Status}}") == "running":
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
            cp = run(["docker", "exec", "-i", "df-v3-svc-director", "python", "-"], input_text=probe, check=False)
            if cp.returncode == 0 and "PASS" in cp.stdout:
                pass_("DIRECTOR_NETWORK_DNS_TCP")
            else:
                crit("DIRECTOR_NETWORK_DNS_TCP_FAILED", "one or more internal dependencies are not resolvable/reachable")
        else:
            crit("DIRECTOR_NETWORK_PROBE_SKIPPED", "Director API is not running")

        print("\n===== 6. DIRECTOR QUEUE HEALTH =====")
        if exists("desifaces-v3-db") and inspect_field("desifaces-v3-db", "{{.State.Status}}") == "running":
            try:
                counts = psql_query("select state||':'||count(*) from public.v3_director_runs group by state order by state;")
                eligible = int(psql_query("select count(*) from public.v3_director_runs where state='queued' and available_at<=now() and attempt_count<max_attempts;") or "0")
                stale = int(psql_query("select count(*) from public.v3_director_runs where state='running' and lease_expires_at is not null and lease_expires_at<now();") or "0")
                latest = psql_query("select coalesce(state,'')||'|'||coalesce(attempt_count::text,'0')||'|'||coalesce(max_attempts::text,'0') from public.v3_director_runs order by created_at desc limit 1;")
                print("queue_state_counts=" + (counts.replace("\n", ",") if counts else "none"))
                print(f"eligible_queued={eligible}")
                print(f"stale_running={stale}")
                print(f"latest_state_attempts={latest or 'none'}")
                worker_running = bool(dworker and dworker["status"] == "running")
                if eligible > 0 and not worker_running:
                    crit("DIRECTOR_QUEUE_WITHOUT_CONSUMER", f"eligible_queued={eligible} while Director worker is not running")
                elif eligible > 0:
                    warn("DIRECTOR_QUEUE_PENDING", f"eligible_queued={eligible}")
                else:
                    pass_("DIRECTOR_QUEUE_NO_ORPHANED_ELIGIBLE_WORK")
                if stale > 0 and not worker_running:
                    crit("DIRECTOR_STALE_LEASE_WITHOUT_RECOVERY_WORKER", f"stale_running={stale}")
                elif stale > 0:
                    warn("DIRECTOR_STALE_LEASES_PRESENT", f"stale_running={stale}")
                else:
                    pass_("DIRECTOR_QUEUE_NO_STALE_RUNNING")
            except Exception:
                crit("DIRECTOR_QUEUE_AUDIT_FAILED", "could not read queue health")
        else:
            crit("DIRECTOR_QUEUE_AUDIT_SKIPPED", "database is not running")

        print("\n===== 7. RUNTIME SOURCE HASH ALIGNMENT =====")
        hash_map = {
            "df-v3-svc-director": [
                ("services/svc-director/app/app/main.py", "/app/app/main.py"),
                ("services/svc-director/app/app/audio_execution_runtime.py", "/app/app/audio_execution_runtime.py"),
                ("services/svc-director/app/app/face_execution_runtime.py", "/app/app/face_execution_runtime.py"),
                ("services/svc-director/app/app/fusion_input_performance.py", "/app/app/fusion_input_performance.py"),
                ("services/svc-director/app/app/studio_aspect_routes.py", "/app/app/studio_aspect_routes.py"),
            ],
            "df-v3-svc-face": [
                ("services/svc-face/app/app/api/routes/face_media.py", "/app/app/api/routes/face_media.py"),
            ],
            "df-v3-svc-audio": [
                ("services/svc-audio/app/app/api/routes/v3_audio_output.py", "/app/app/api/routes/v3_audio_output.py"),
            ],
            "df-v3-svc-fusion": [
                ("services/svc-fusion/app/app/domain/enums.py", "/app/app/domain/enums.py"),
            ],
        }
        for container, mappings in hash_map.items():
            if not exists(container) or inspect_field(container, "{{.State.Status}}") != "running":
                crit("RUNTIME_HASH_UNAVAILABLE", container)
                continue
            for rel, inside in mappings:
                actual = container_sha(container, inside)
                expected = sha256_file(source / rel)
                code = f"RUNTIME_SOURCE_{container}_{Path(rel).name}".replace("-", "_").replace(".", "_")
                if actual == expected:
                    pass_(code)
                else:
                    crit("RUNTIME_SOURCE_DRIFT", f"{container}:{rel}")

        print("\n===== 8. REQUIRED API CONTRACTS =====")
        route_checks = [
            ("df-v3-svc-face", "/api/face/assets/{media_asset_id}/read-url"),
            ("df-v3-svc-audio", "/api/audio/jobs/{job_id}/canonical-output"),
            ("df-v3-svc-audio", "/api/audio/assets/{media_id}/read-url"),
            ("df-v3-svc-director", "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio"),
        ]
        for container, route in route_checks:
            if not exists(container) or inspect_field(container, "{{.State.Status}}") != "running":
                crit("ROUTE_CHECK_UNAVAILABLE", f"{container}:{route}")
                continue
            code = "from app.main import app; import sys; sys.exit(0 if " + repr(route) + " in {getattr(r,'path','') for r in app.routes} else 7)"
            cp = run(["docker", "exec", container, "python", "-c", code], check=False)
            if cp.returncode == 0:
                pass_("ROUTE_" + route.replace("/", "_").replace("{", "").replace("}", "").replace("-", "_").upper())
            else:
                crit("REQUIRED_ROUTE_MISSING", f"{container}:{route}")

        if all(x in (source / "services/svc-fusion/app/app/domain/enums.py").read_text() for x in ('"16:9"', '"9:16"', '"1:1"')):
            pass_("FUSION_ASPECT_RATIO_SET")
        else:
            crit("FUSION_ASPECT_RATIO_SET_INCOMPLETE", "expected 9:16,16:9,1:1")

        print("\n===== 9. PERSISTENT DEV WEB =====")
        web = inventory.get("df-v3-web")
        if web and web["status"] == "running":
            port = inspect_field("df-v3-web", "{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostIp}}:{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostPort}}")
            print(f"web_binding={port or 'unknown'}")
            if port != "127.0.0.1:13000":
                crit("DEV_WEB_BINDING_DRIFT", f"expected 127.0.0.1:13000 got {port or 'unknown'}")
            else:
                pass_("DEV_WEB_LOOPBACK_BINDING")
            cp = run(["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:13000/auth/login"], check=False)
            if cp.returncode == 0 and "desifaces" in cp.stdout.lower():
                pass_("DEV_WEB_HTTP")
            else:
                crit("DEV_WEB_HTTP_FAILED", "127.0.0.1:13000/auth/login not healthy")
            label = inspect_field("df-v3-web", '{{index .Config.Labels "desifaces.web_sha"}}')
            if label and label != WEB_SHA:
                crit("DEV_WEB_REVISION_DRIFT", f"label={label[:12]} expected={WEB_SHA[:12]}")
            elif label == WEB_SHA:
                pass_("DEV_WEB_REVISION_LABEL")
            else:
                warn("DEV_WEB_REVISION_UNLABELED", "web runtime has no revision label")

        print("\n===== 10. WORKER RESILIENCE + RELEASE HYGIENE =====")
        worker_src = (source / "services/svc-director/app/app/worker.py").read_text()
        # Current worker acquires DB connections outside the per-run exception block.
        # There is no outer transient DNS/connection retry marker or supervisor loop.
        resilient_markers = (
            "director_worker_transient_retry",
            "Temporary failure in name resolution",
            "socket.gaierror",
            "ConnectionError",
        )
        if not any(marker in worker_src for marker in resilient_markers):
            crit(
                "DIRECTOR_WORKER_TRANSIENT_RESILIENCE_MISSING",
                "worker can terminate on transient DB/DNS acquisition failure outside per-run exception handling",
            )
        else:
            pass_("DIRECTOR_WORKER_TRANSIENT_RESILIENCE")

        compose_text = (source / "docker-compose.yml").read_text()
        sensitive_default = re.compile(
            r"\$\{[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|TOKEN|BEARER)[A-Z0-9_]*:-([^}]*)\}"
        )
        if any(m.group(1).strip() for m in sensitive_default.finditer(compose_text)):
            crit("TRACKED_SECRET_DEFAULT_PRESENT", "tracked compose contains a non-empty sensitive configuration default")
        else:
            pass_("NO_TRACKED_SENSITIVE_COMPOSE_DEFAULTS")

        tracked = output(["git", "-C", source, "ls-files"]).splitlines()
        if "infra/.env" in tracked:
            crit("ENV_FILE_TRACKED", "infra/.env must not be committed")
        else:
            pass_("ENV_FILE_NOT_TRACKED")

        suspect_files: set[str] = set()
        token_patterns = [
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
            re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}"),
        ]
        for rel in tracked:
            p = source / rel
            if not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            if any(rx.search(text) for rx in token_patterns):
                suspect_files.add(rel)
        if suspect_files:
            crit("TRACKED_TOKEN_LIKE_MATERIAL", "files=" + ",".join(sorted(suspect_files)))
        else:
            pass_("NO_TRACKED_TOKEN_LIKE_MATERIAL")

        cert_path = source / "scripts/certify-multiperson-dev-20260909.py"
        if cert_path.is_file() and 'compose("config", "svc-face"' in cert_path.read_text():
            crit("CERTIFICATION_SECRET_OUTPUT_RISK", "dev certification emits resolved compose instead of capturing service-only output")
        else:
            pass_("CERTIFICATION_OUTPUT_SECRET_SAFE")

        if local_head != BASELINE:
            crit("OPERATIONAL_SCRIPTS_NOT_IMMUTABLE_BY_DEFAULT", "recent recovery depended on local checkout rather than pinned worktree")

        print("\n============================================================")
        print(" V3 DEV INTEGRITY AUDIT SUMMARY")
        print("============================================================")
        print(f"PASS_COUNT={len(PASSES)}")
        print(f"WARNING_COUNT={len(WARNING)}")
        print(f"CRITICAL_COUNT={len(CRITICAL)}")
        print("RUNTIME_MUTATIONS=NONE")
        print("PRODUCTION_TOUCH=NONE")
        if WARNING:
            print("WARNINGS=")
            for code, message in WARNING:
                print(f"  - {code}: {message}")
        if CRITICAL:
            print("CRITICALS=")
            for code, message in CRITICAL:
                print(f"  - {code}: {message}")
            print("OVERALL=BLOCKED")
            print("NEXT=FIX_ALL_CRITICALS_AS_ONE_NON_PROD_HARDENING_BUNDLE")
            return 2
        if WARNING:
            print("OVERALL=PASS_WITH_WARNINGS")
            return 1
        print("OVERALL=PASS")
        return 0
    finally:
        if source.exists():
            run(["git", "-C", ROOT, "worktree", "remove", "--force", source], check=False)
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
