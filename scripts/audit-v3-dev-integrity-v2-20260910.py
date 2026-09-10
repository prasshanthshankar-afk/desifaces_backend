#!/usr/bin/env python3
from __future__ import annotations

import hashlib
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
APP_BASELINE = "89e30d3fab49db0231548c1b72dc3720ad032e87"
WEB_SHA = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
NETWORK = "df-v3-net"

critical: list[tuple[str, str]] = []
warnings: list[tuple[str, str]] = []
passes: list[str] = []


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


def pass_(code):
    passes.append(code)
    print(f"PASS  {code}")


def crit(code, message):
    critical.append((code, message))
    print(f"CRIT  {code}: {message}")


def warn(code, message):
    warnings.append((code, message))
    print(f"WARN  {code}: {message}")


def exists(name):
    return subprocess.run(
        ["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def field(name, template):
    cp = run(["docker", "inspect", name, "--format", template], check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def sha_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def runtime_sha(container, inside):
    if not exists(container) or field(container, "{{.State.Status}}") != "running":
        return None
    cp = run(["docker", "exec", container, "sha256sum", inside], check=False)
    return cp.stdout.split()[0] if cp.returncode == 0 and cp.stdout.strip() else None


def psql(sql):
    cp = run([
        "docker", "exec", "desifaces-v3-db", "sh", "-lc",
        'psql -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
        "--", sql,
    ], check=False)
    if cp.returncode != 0:
        raise RuntimeError("database query failed")
    return cp.stdout.strip()


def inventory(name, required=True):
    if not exists(name):
        (crit if required else warn)("CONTAINER_MISSING", name)
        return None
    data = {
        "status": field(name, "{{.State.Status}}"),
        "health": field(name, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"),
        "exit": field(name, "{{.State.ExitCode}}"),
        "restarts": field(name, "{{.RestartCount}}"),
        "restart_policy": field(name, "{{.HostConfig.RestartPolicy.Name}}"),
        "image_id": field(name, "{{.Image}}"),
        "image_ref": field(name, "{{.Config.Image}}"),
    }
    print(
        f"container={name} status={data['status']} health={data['health']} "
        f"exit={data['exit']} restarts={data['restarts']} "
        f"restart_policy={data['restart_policy']} image={data['image_ref']}"
    )
    if required and data["status"] != "running":
        crit("REQUIRED_CONTAINER_NOT_RUNNING", f"{name} status={data['status']} exit={data['exit']}")
    if required and data["restart_policy"] not in {"always", "unless-stopped"}:
        crit("RESTART_POLICY_WEAK", f"{name} restart_policy={data['restart_policy'] or 'none'}")
    return data


def main():
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces V3 DEV — READ-ONLY INTEGRITY AUDIT v2")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("runtime_mutations=NONE")
    print("container_builds=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")
    print("secret_values_output=FORBIDDEN")
    print(f"application_baseline={APP_BASELINE}")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    if not (ROOT / ".git").exists():
        raise SystemExit(f"FAIL: dev Git checkout missing: {ROOT}")
    if not (ROOT / "infra/.env").is_file():
        raise SystemExit(f"FAIL: V3 env file missing: {ROOT / 'infra/.env'}")

    temp = Path(tempfile.mkdtemp(prefix="df-v3-readonly-audit-"))
    src = temp / "source"
    try:
        print("\n===== 1. CERTIFIED SOURCE vs LOCAL OPERATIONAL CHECKOUT =====")
        if run(["git", "-C", ROOT, "fetch", "--no-tags", "origin", BRANCH], check=False).returncode != 0:
            crit("GIT_METADATA_REFRESH_FAILED", "cannot refresh branch metadata")
            return 2
        if run(["git", "-C", ROOT, "cat-file", "-e", f"{APP_BASELINE}^{{commit}}"], check=False).returncode != 0:
            crit("CERTIFIED_BASELINE_UNAVAILABLE", APP_BASELINE)
            return 2
        run(["git", "-C", ROOT, "worktree", "add", "--detach", src, APP_BASELINE])
        shutil.copy2(ROOT / "infra/.env", src / "infra/.env")
        os.chmod(src / "infra/.env", 0o600)
        pass_("CERTIFIED_SOURCE_MATERIALIZED")

        local_head = out(["git", "-C", ROOT, "rev-parse", "HEAD"])
        local_branch = out(["git", "-C", ROOT, "branch", "--show-current"])
        dirty = out(["git", "-C", ROOT, "status", "--porcelain"])
        print(f"local_branch={local_branch or 'DETACHED'}")
        print(f"local_head={local_head}")
        print(f"local_dirty_entries={sum(1 for x in dirty.splitlines() if x.strip())}")
        if local_head != APP_BASELINE:
            warn("LOCAL_HEAD_NOT_APPLICATION_BASELINE", f"local={local_head[:12]} baseline={APP_BASELINE[:12]}")
        if dirty.strip():
            warn("LOCAL_CHECKOUT_DIRTY", "operational deployment must not consume mutable working-tree content")

        for rel in ("docker-compose.yml", "docker-compose.v3.yml", "scripts/v3-compose.sh"):
            lp, bp = ROOT / rel, src / rel
            if not lp.is_file():
                crit("LOCAL_OPERATIONAL_FILE_MISSING", rel)
            elif sha_file(lp) != sha_file(bp):
                crit("LOCAL_OPERATIONAL_FILE_DRIFT", rel)
            else:
                pass_("LOCAL_MATCH_" + rel.replace("/", "_").replace(".", "_").upper())

        print("\n===== 2. COMPOSE SERVICE INVENTORY — SERVICE NAMES ONLY =====")
        immutable_cmd = [
            "bash", src / "scripts/v3-compose.sh", "--profile", "v3-orchestration",
            "--profile", "v3-execution", "config", "--services",
        ]
        cp = run(immutable_cmd, check=False)
        immutable_services = {x.strip() for x in cp.stdout.splitlines() if x.strip()} if cp.returncode == 0 else set()
        required_services = {
            "desifaces-db", "desifaces-redis", "svc-core", "svc-pricing",
            "svc-face", "svc-face-worker", "svc-audio", "svc-audio-worker",
            "svc-fusion", "svc-fusion-worker", "svc-fusion-extension",
            "svc-fusion-extension-stitch-worker", "svc-director", "svc-director-worker",
        }
        if not immutable_services:
            crit("CERTIFIED_COMPOSE_RESOLUTION_FAILED", "could not resolve pinned service inventory")
        else:
            missing = sorted(required_services - immutable_services)
            if missing:
                crit("CERTIFIED_COMPOSE_SERVICE_GAP", ",".join(missing))
            else:
                pass_("CERTIFIED_COMPOSE_SERVICE_SET")
            print(f"certified_service_count={len(immutable_services)}")

        local_cmd = [
            "bash", ROOT / "scripts/v3-compose.sh", "--profile", "v3-orchestration",
            "--profile", "v3-execution", "config", "--services",
        ]
        lp = run(local_cmd, check=False)
        if lp.returncode != 0:
            crit("LOCAL_COMPOSE_RESOLUTION_FAILED", "local V3 compose cannot resolve service names")
        else:
            local_services = {x.strip() for x in lp.stdout.splitlines() if x.strip()}
            if "svc-director-worker" not in local_services:
                crit("LOCAL_DIRECTOR_WORKER_SERVICE_MISSING", "local compose cannot address Director worker")
            elif immutable_services and local_services != immutable_services:
                crit("LOCAL_CERTIFIED_SERVICE_SET_DRIFT", "local and certified compose service sets differ")
            else:
                pass_("LOCAL_COMPOSE_SERVICE_SET")

        recovery = src / "scripts/recover-certify-director-worker-dev-20260910.sh"
        if recovery.is_file() and 'bash "$ROOT/scripts/v3-compose.sh"' in recovery.read_text(errors="ignore"):
            crit(
                "RECOVERY_SCRIPT_MUTABLE_COMPOSE_DEPENDENCY",
                "Director recovery executes compose from the mutable local checkout instead of a pinned source tree",
            )
        else:
            pass_("RECOVERY_SCRIPT_IMMUTABLE_COMPOSE")

        print("\n===== 3. REQUIRED MULTI-PERSON RUNTIME INVENTORY =====")
        required_containers = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-core", "df-v3-svc-pricing",
            "df-v3-svc-face", "df-v3-svc-face-worker", "df-v3-svc-audio", "df-v3-svc-audio-worker",
            "df-v3-svc-fusion", "df-v3-svc-fusion-worker", "df-v3-svc-fusion-extension",
            "df-v3-svc-fusion-extension-stitch-worker", "df-v3-svc-director", "df-v3-svc-director-worker",
            "df-v3-web",
        ]
        inv = {name: inventory(name) for name in required_containers}

        print("\n===== 4. API / WORKER IMAGE PARITY =====")
        da, dw = inv.get("df-v3-svc-director"), inv.get("df-v3-svc-director-worker")
        if da and dw:
            if da["image_id"] != dw["image_id"]:
                crit("DIRECTOR_API_WORKER_IMAGE_DRIFT", "Director API and worker image IDs differ")
            else:
                pass_("DIRECTOR_API_WORKER_IMAGE_PARITY")

        print("\n===== 5. NETWORK MEMBERSHIP + LIVE DNS/TCP =====")
        network_members = [
            "desifaces-v3-db", "desifaces-v3-redis", "df-v3-svc-pricing", "df-v3-svc-face",
            "df-v3-svc-audio", "df-v3-svc-fusion", "df-v3-svc-fusion-extension",
            "df-v3-svc-director", "df-v3-svc-director-worker", "df-v3-web",
        ]
        network_bad = False
        for name in network_members:
            if not exists(name):
                continue
            networks = field(name, "{{json .NetworkSettings.Networks}}")
            if NETWORK not in networks:
                network_bad = True
                crit("V3_NETWORK_MEMBERSHIP_GAP", f"{name} not attached to {NETWORK}")
        if not network_bad:
            pass_("V3_NETWORK_MEMBERSHIP")

        if da and da["status"] == "running":
            probe = r'''
import socket
for h,p in [('desifaces-db',5432),('desifaces-redis',6379),('svc-pricing',8009),('svc-face',8003),('svc-audio',8004),('svc-fusion',8002),('svc-fusion-extension',8006)]:
    socket.gethostbyname(h)
    with socket.create_connection((h,p),timeout=3): pass
print('OK')
'''
            cp = run(["docker", "exec", "-i", "df-v3-svc-director", "python", "-"], input_text=probe, check=False)
            if cp.returncode == 0 and "OK" in cp.stdout:
                pass_("DIRECTOR_LIVE_DNS_TCP")
            else:
                crit("DIRECTOR_LIVE_DNS_TCP_FAILED", "one or more internal dependencies are not reachable")
        else:
            crit("DIRECTOR_LIVE_NETWORK_UNTESTABLE", "Director API is not running")

        print("\n===== 6. DIRECTOR DURABLE QUEUE HEALTH =====")
        try:
            counts = psql("select state||':'||count(*) from public.v3_director_runs group by state order by state;")
            eligible = int(psql("select count(*) from public.v3_director_runs where state='queued' and available_at<=now() and attempt_count<max_attempts;") or "0")
            stale = int(psql("select count(*) from public.v3_director_runs where state='running' and lease_expires_at is not null and lease_expires_at<now();") or "0")
            latest = psql("select coalesce(state,'')||'|'||coalesce(attempt_count::text,'0')||'/'||coalesce(max_attempts::text,'0') from public.v3_director_runs order by created_at desc limit 1;")
            print("queue_state_counts=" + (counts.replace("\n", ",") if counts else "none"))
            print(f"eligible_queued={eligible}")
            print(f"stale_running={stale}")
            print(f"latest_state_attempts={latest or 'none'}")
            worker_running = bool(dw and dw["status"] == "running")
            if eligible and not worker_running:
                crit("DIRECTOR_QUEUE_WITHOUT_CONSUMER", f"eligible_queued={eligible}; worker not running")
            elif eligible:
                warn("DIRECTOR_QUEUE_PENDING", f"eligible_queued={eligible}")
            else:
                pass_("DIRECTOR_NO_ORPHANED_ELIGIBLE_QUEUE")
            if stale and not worker_running:
                crit("DIRECTOR_STALE_LEASE_WITHOUT_CONSUMER", f"stale_running={stale}")
            elif stale:
                warn("DIRECTOR_STALE_LEASES", f"stale_running={stale}")
            else:
                pass_("DIRECTOR_NO_STALE_RUNNING_LEASES")
        except Exception:
            crit("DIRECTOR_QUEUE_HEALTH_UNREADABLE", "could not query v3_director_runs")

        print("\n===== 7. CERTIFIED SOURCE vs RUNNING API HASHES =====")
        checks = {
            "df-v3-svc-director": [
                ("services/svc-director/app/app/main.py", "/app/app/main.py"),
                ("services/svc-director/app/app/audio_execution_runtime.py", "/app/app/audio_execution_runtime.py"),
                ("services/svc-director/app/app/face_execution_runtime.py", "/app/app/face_execution_runtime.py"),
                ("services/svc-director/app/app/fusion_input_performance.py", "/app/app/fusion_input_performance.py"),
                ("services/svc-director/app/app/studio_aspect_routes.py", "/app/app/studio_aspect_routes.py"),
            ],
            "df-v3-svc-face": [("services/svc-face/app/app/api/routes/face_media.py", "/app/app/api/routes/face_media.py")],
            "df-v3-svc-audio": [("services/svc-audio/app/app/api/routes/v3_audio_output.py", "/app/app/api/routes/v3_audio_output.py")],
            "df-v3-svc-fusion": [("services/svc-fusion/app/app/domain/enums.py", "/app/app/domain/enums.py")],
        }
        for container, files in checks.items():
            for rel, inside in files:
                expected = sha_file(src / rel)
                actual = runtime_sha(container, inside)
                if actual == expected:
                    pass_("SOURCE_HASH_" + container.replace("-", "_") + "_" + Path(rel).name.replace(".", "_"))
                else:
                    crit("RUNNING_SOURCE_DRIFT", f"{container}:{rel}")

        print("\n===== 8. REQUIRED API ROUTES + ASPECT CONTRACT =====")
        routes = [
            ("df-v3-svc-face", "/api/face/assets/{media_asset_id}/read-url"),
            ("df-v3-svc-audio", "/api/audio/jobs/{job_id}/canonical-output"),
            ("df-v3-svc-audio", "/api/audio/assets/{media_id}/read-url"),
            ("df-v3-svc-director", "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio"),
        ]
        for container, route in routes:
            if not exists(container) or field(container, "{{.State.Status}}") != "running":
                crit("ROUTE_UNTESTABLE", f"{container}:{route}")
                continue
            py = "from app.main import app; import sys; sys.exit(0 if " + repr(route) + " in {getattr(r,'path','') for r in app.routes} else 9)"
            if run(["docker", "exec", container, "python", "-c", py], check=False).returncode == 0:
                pass_("ROUTE_PRESENT_" + container.replace("-", "_") + "_" + str(routes.index((container, route))))
            else:
                crit("REQUIRED_ROUTE_MISSING", f"{container}:{route}")
        fusion_enum = (src / "services/svc-fusion/app/app/domain/enums.py").read_text()
        if all(v in fusion_enum for v in ('"9:16"', '"16:9"', '"1:1"')):
            pass_("FUSION_ASPECT_9_16_16_9_1_1")
        else:
            crit("FUSION_ASPECT_CONTRACT_INCOMPLETE", "expected 9:16,16:9,1:1")

        print("\n===== 9. DEV WEB RUNTIME =====")
        web = inv.get("df-v3-web")
        if web and web["status"] == "running":
            cp = run(["docker", "port", "df-v3-web", "3000/tcp"], check=False)
            bindings = [x.strip() for x in cp.stdout.splitlines() if x.strip()]
            print("web_bindings=" + (",".join(bindings) if bindings else "none"))
            if bindings == ["127.0.0.1:13000"]:
                pass_("DEV_WEB_LOOPBACK_13000")
            else:
                crit("DEV_WEB_BINDING_DRIFT", "expected only 127.0.0.1:13000")
            http = run(["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:13000/auth/login"], check=False)
            if http.returncode == 0 and "desifaces" in http.stdout.lower():
                pass_("DEV_WEB_HTTP")
            else:
                crit("DEV_WEB_HTTP_FAILED", "login page not healthy on 127.0.0.1:13000")
            label = field("df-v3-web", '{{index .Config.Labels "desifaces.web_sha"}}')
            if label == WEB_SHA:
                pass_("DEV_WEB_REVISION_LABEL")
            elif label:
                crit("DEV_WEB_REVISION_DRIFT", f"runtime={label[:12]} expected={WEB_SHA[:12]}")
            else:
                warn("DEV_WEB_REVISION_UNLABELED", "revision label missing")

        print("\n===== 10. RESILIENCE + SECURITY / RELEASE HYGIENE =====")
        worker_src = (src / "services/svc-director/app/app/worker.py").read_text()
        # The current process has no outer retry boundary for DB/DNS acquisition.
        if not any(m in worker_src for m in ("director_worker_transient_retry", "socket.gaierror", "Temporary failure in name resolution")):
            crit(
                "DIRECTOR_WORKER_TRANSIENT_RESILIENCE_MISSING",
                "transient DB/DNS acquisition failure can terminate the orchestration process",
            )
        else:
            pass_("DIRECTOR_WORKER_TRANSIENT_RESILIENCE")

        base_compose = (src / "docker-compose.yml").read_text()
        sensitive_default = re.compile(r"\$\{[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|TOKEN|BEARER)[A-Z0-9_]*:-([^}]*)\}")
        if any(m.group(1).strip() for m in sensitive_default.finditer(base_compose)):
            crit("TRACKED_SENSITIVE_DEFAULT", "tracked compose contains a non-empty sensitive configuration default")
        else:
            pass_("NO_TRACKED_SENSITIVE_DEFAULTS")

        tracked = out(["git", "-C", src, "ls-files"]).splitlines()
        if "infra/.env" in tracked:
            crit("ENV_TRACKED", "infra/.env is tracked")
        else:
            pass_("ENV_NOT_TRACKED")

        suspect_files = set()
        regexes = [re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}")]
        for rel in tracked:
            p = src / rel
            if not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            if any(rx.search(text) for rx in regexes):
                suspect_files.add(rel)
        if suspect_files:
            crit("TRACKED_TOKEN_LIKE_CONTENT", "files=" + ",".join(sorted(suspect_files)))
        else:
            pass_("NO_TRACKED_TOKEN_LIKE_CONTENT")

        cert = src / "scripts/certify-multiperson-dev-20260909.py"
        if cert.is_file() and 'compose("config", "svc-face"' in cert.read_text(errors="ignore"):
            crit(
                "CERTIFICATION_OUTPUT_SECRET_RISK",
                "existing dev certification can emit fully resolved compose environment to terminal logs",
            )
        else:
            pass_("CERTIFICATION_OUTPUT_REDACTED")

        print("\n============================================================")
        print(" V3 DEV INTEGRITY AUDIT SUMMARY")
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
            print("NEXT=ONE_NON_PROD_HARDENING_BUNDLE_FOR_ALL_CRITICALS")
            return 2
        if warnings:
            print("OVERALL=PASS_WITH_WARNINGS")
            return 1
        print("OVERALL=PASS")
        return 0
    finally:
        if src.exists():
            run(["git", "-C", ROOT, "worktree", "remove", "--force", src], check=False)
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
