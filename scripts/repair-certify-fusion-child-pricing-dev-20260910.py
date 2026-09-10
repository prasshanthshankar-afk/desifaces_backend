#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

EXPECTED_HOST = "desifaces-dev"
BACKEND_ROOT = Path("/home/azureuser/workspace/desifaces-v3")
WEB_ROOT = Path("/home/azureuser/workspace/desifaces-web")
BACKEND_REF = "10492d1c29da53c5150cb2f7b161fb57460cfa4d"
WEB_REF = "535f9fa25da0f4f08650a5c58fdf26c6759d22f9"
NETWORK = "df-v3-net"
DIRECTOR_API = "df-v3-svc-director"
DIRECTOR_WORKER = "df-v3-svc-director-worker"
WEB = "df-v3-web"
WEB_PORT = 13000
DB = "desifaces-v3-db"

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
    "df-v3-svc-fusion-extension",
    "df-v3-svc-fusion-extension-stitch-worker",
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


def psql(sql: str) -> str:
    cp = run([
        "docker", "exec", "-e", f"DF_READONLY_SQL={sql}", DB,
        "sh", "-lc",
        'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"',
    ], capture=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError("read-only DB certification query failed")
    return cp.stdout.strip()


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
    print(" desifaces DEV — FUSION CHILD PRICING CONTRACT REPAIR")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("production_touch=FORBIDDEN")
    print(f"backend_ref={BACKEND_REF}")
    print(f"web_ref={WEB_REF}")
    print("runtime_scope=Director API + Director worker + dev web")
    print("fusion_restart=FORBIDDEN")
    print("face_audio_restart=FORBIDDEN")
    print("fusion_extension_restart=FORBIDDEN")
    print("db_redis_restart=FORBIDDEN")
    print("provider_generation=NONE")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")
    for root in (BACKEND_ROOT, WEB_ROOT):
        if not (root / ".git").exists():
            raise SystemExit(f"FAIL: Git checkout missing: {root}")
    if not (BACKEND_ROOT / "infra/.env").is_file():
        raise SystemExit("FAIL: dev V3 env file missing")
    for name in (*NON_TARGETS, DIRECTOR_API, DIRECTOR_WORKER, WEB):
        if not exists(name):
            raise SystemExit(f"FAIL: required dev container missing: {name}")

    before = {name: snapshot(name) for name in NON_TARGETS}
    director_before = {
        DIRECTOR_API: snapshot(DIRECTOR_API),
        DIRECTOR_WORKER: snapshot(DIRECTOR_WORKER),
    }
    director_restart = {
        DIRECTOR_API: field(DIRECTOR_API, "{{.HostConfig.RestartPolicy.Name}}") or "unless-stopped",
        DIRECTOR_WORKER: field(DIRECTOR_WORKER, "{{.HostConfig.RestartPolicy.Name}}") or "unless-stopped",
    }
    old_web_image = field(WEB, "{{.Config.Image}}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    api_rollback = f"{DIRECTOR_API}-rollback-child-pricing-{stamp}"
    worker_rollback = f"{DIRECTOR_WORKER}-rollback-child-pricing-{stamp}"
    web_rollback = f"{WEB}-rollback-child-pricing-{stamp}"
    web_candidate = f"{WEB}-candidate-child-pricing-{stamp}"

    tmp = Path(tempfile.mkdtemp(prefix="desifaces-fusion-child-pricing-dev-"))
    backend = tmp / "backend"
    webrepo = tmp / "webrepo"
    director_swapped = False
    director_started = False
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
        if director_started:
            for name in (DIRECTOR_API, DIRECTOR_WORKER):
                if exists(name):
                    run(["docker", "rm", "-f", name], check=False)
        if director_swapped:
            for rollback_name, live_name in ((api_rollback, DIRECTOR_API), (worker_rollback, DIRECTOR_WORKER)):
                if exists(rollback_name):
                    run(["docker", "rename", rollback_name, live_name], check=False)
                    run(["docker", "update", f"--restart={director_restart[live_name]}", live_name], check=False)
                    run(["docker", "start", live_name], check=False)
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
        print("IMMUTABLE_SOURCE=PASS")

        print("\n===== 2. PROVE FAILED SCENE BILLING SAFETY =====", flush=True)
        latest_stage = psql("""
select stage_run_id::text
from public.v3_studio_stage_runs
where stage_type='fusion' and state='failed'
order by updated_at desc limit 1;
""").strip()
        if not latest_stage:
            raise RuntimeError("latest failed Fusion stage not found")
        print(f"failed_stage={latest_stage}")
        counts = psql(f"""
with c as (
  select payload_json,meta_json,status
  from public.studio_jobs
  where studio_type='fusion'
    and (
      payload_json #>> '{{provider_options,billing_context,billing_parent_job_id}}' = '{latest_stage}'
      or payload_json #>> '{{provider_options,billing_context,parent_longform_job_id}}' = '{latest_stage}'
      or payload_json #>> '{{tags,billing_context,billing_parent_job_id}}' = '{latest_stage}'
      or payload_json #>> '{{tags,billing_context,parent_longform_job_id}}' = '{latest_stage}'
    )
), p as (
  select status,
         coalesce(payload_json->'pricing',meta_json->'pricing','{{}}'::jsonb) pricing
  from c
)
select count(*)::text,
       count(*) filter (where lower(coalesce(pricing->>'state',''))='suppressed')::text,
       count(*) filter (where coalesce((pricing->>'enabled')::boolean,false)=false)::text,
       count(*) filter (where coalesce(pricing->>'quote_id','')='')::text,
       count(*) filter (where lower(coalesce(pricing->>'billing_mode',pricing->>'pricing_mode','')) in ('internal','internal_child'))::text,
       count(*) filter (where lower(status) in ('queued','processing','running','pending'))::text,
       count(*) filter (where lower(status) in ('succeeded','completed','ready'))::text,
       count(*) filter (where lower(status) in ('failed','blocked','canceled','cancelled'))::text
from p;
""")
        parts = counts.split("|") if counts else []
        if len(parts) != 8:
            raise RuntimeError("could not classify existing child jobs")
        total, suppressed, disabled, no_quote, internal_mode, active, succeeded, failed = map(int, parts)
        print(f"orphan_child_jobs={total}")
        print(f"orphan_child_active={active}")
        print(f"orphan_child_succeeded={succeeded}")
        print(f"orphan_child_failed={failed}")
        if total and not (suppressed == disabled == no_quote == internal_mode == total):
            raise RuntimeError("existing child job billing is not uniformly suppressed; stop before retry")
        print("EXISTING_CHILD_BILLING_SUPPRESSED=PASS")

        print("\n===== 3. STATIC CROSS-SERVICE CONTRACT =====", flush=True)
        runtime_text = (backend / "services/svc-director/app/app/fusion_execution_runtime.py").read_text()
        orphan_text = (backend / "services/svc-director/app/app/fusion_execution_orphan_recovery.py").read_text()
        fusion_route = (backend / "services/svc-fusion/app/app/api/routes/fusion_jobs.py").read_text()
        fusion_orch = (backend / "services/svc-fusion/app/app/services/fusion_orchestrator.py").read_text()
        for marker in ('{"internal", "internal_child"}', 'enabled is not True', 'state == "suppressed"'):
            if marker not in runtime_text:
                raise RuntimeError(f"Director semantic suppression contract missing: {marker}")
        for marker in ("lost_create_response_recovered", "fusion_existing_internal_child_still_running"):
            if marker not in orphan_text:
                raise RuntimeError(f"orphan reconciliation contract missing: {marker}")
        if '"billing_mode": "internal_child"' not in fusion_orch:
            raise RuntimeError("Fusion persisted internal-child pricing representation missing")
        if "_stamp_internal_child_pricing_suppression" not in fusion_route:
            raise RuntimeError("Fusion route suppression bridge missing")
        print("DIRECTOR_FUSION_CHILD_PRICING_CONTRACT=PASS")
        print("ORPHAN_RECONCILIATION_CONTRACT=PASS")

        print("\n===== 4. BUILD + PROVE DIRECTOR IMAGE =====", flush=True)
        compose("build", "svc-director")
        probe = r'''
from app.fusion_execution_runtime import _is_internal_child_pricing_contract as ok

def p(mode, **extra):
    pricing={
      'enabled':False,'state':'suppressed','suppressed':True,
      'pricing_suppressed':True,'billing_mode':mode,'quote_id':None,
    }
    pricing.update(extra)
    return {'pricing':pricing}
assert ok(p('internal'))
assert ok(p('internal_child'))
assert not ok(p('wallet', enabled=True, suppressed=False, pricing_suppressed=False))
assert not ok(p('internal_child', quote_id='must-fail'))
assert not ok(p('internal_child', state='reserved'))
print('DIRECTOR_CHILD_PRICING_SEMANTIC_PROOF=PASS')
'''
        compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "svc-director", input_text=probe)
        print("DIRECTOR_IMAGE_BUILD=PASS")

        print("\n===== 5. BUILD + PROVE WEB FEEDBACK =====", flush=True)
        web_image = f"desifaces-web-dev:{WEB_REF[:13]}"
        run(["docker", "build", "-t", web_image, webrepo / "web"])
        run([
            "docker", "run", "--rm", "--entrypoint", "sh", web_image, "-lc",
            "grep -R -q 'Scene generation needs attention' /app/.next && "
            "grep -R -q 'Scene generation accepted' /app/.next && "
            "grep -R -q 'desifaces:multiperson-fusion-dispatch' /app/.next",
        ])
        print("WEB_INLINE_DISPATCH_FEEDBACK=PASS")

        print("\n===== 6. ISOLATED WEB CANDIDATE =====", flush=True)
        candidate_port = free_port()
        run_web(web_candidate, candidate_port, "no", web_image)
        candidate_started = True
        wait_http(f"http://127.0.0.1:{candidate_port}/auth/login", "web-candidate")
        print("WEB_CANDIDATE=PASS")

        print("\n===== 7. CUT OVER DIRECTOR API + WORKER =====", flush=True)
        for live, rollback_name in ((DIRECTOR_API, api_rollback), (DIRECTOR_WORKER, worker_rollback)):
            run(["docker", "stop", live])
            run(["docker", "rename", live, rollback_name])
            run(["docker", "update", "--restart=no", rollback_name])
        director_swapped = True
        compose("up", "-d", "--no-deps", "svc-director", "svc-director-worker")
        director_started = True
        wait_http("http://127.0.0.1:18011/api/health", "director")
        if field(DIRECTOR_WORKER, "{{.State.Status}}") != "running":
            raise RuntimeError("Director worker did not remain running")
        if field(DIRECTOR_API, "{{.Image}}") != field(DIRECTOR_WORKER, "{{.Image}}"):
            raise RuntimeError("Director API/worker image parity failed")
        runtime_probe = r'''
from app.fusion_execution_runtime import _is_internal_child_pricing_contract as ok
assert ok({'pricing':{'enabled':False,'state':'suppressed','pricing_suppressed':True,'billing_mode':'internal_child','quote_id':None}})
print('DIRECTOR_RUNTIME_CHILD_PRICING_CONTRACT=PASS')
'''
        run(["docker", "exec", "-i", DIRECTOR_API, "python", "-"], input_text=runtime_probe)
        print("DIRECTOR_API_WORKER_CUTOVER=PASS")

        print("\n===== 8. CUT OVER DEV WEB =====", flush=True)
        run(["docker", "stop", WEB])
        run(["docker", "rename", WEB, web_rollback])
        run(["docker", "update", "--restart=no", web_rollback])
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
            raise RuntimeError("web revision label mismatch")
        print("DEV_WEB_CUTOVER=PASS")

        print("\n===== 9. NON-TARGET RUNTIME INVARIANTS =====", flush=True)
        for name, value in before.items():
            if snapshot(name) != value:
                raise RuntimeError(f"non-target runtime changed: {name}")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("FUSION_RUNTIME_UNCHANGED=PASS")
        print("FACE_AUDIO_EXTENSION_UNCHANGED=PASS")
        print("DB_REDIS_UNCHANGED=PASS")

        # Finalize rollback slots only after every gate passes.
        if exists(web_candidate):
            run(["docker", "rm", "-f", web_candidate], check=False)
        candidate_started = False
        for name in (api_rollback, worker_rollback, web_rollback):
            if exists(name):
                run(["docker", "rm", "-f", name], check=False)
        director_swapped = False
        web_swapped = False
        success = True

        print("\n============================================================")
        print(" DEV FUSION CHILD PRICING CONTRACT REPAIR PASS")
        print("============================================================")
        print("EXISTING_CHILD_BILLING_SUPPRESSED=PASS")
        print("DIRECTOR_FUSION_CHILD_PRICING_CONTRACT=PASS")
        print("ORPHAN_RECONCILIATION_CONTRACT=PASS")
        print("DIRECTOR_CHILD_PRICING_SEMANTIC_PROOF=PASS")
        print("DIRECTOR_RUNTIME_CHILD_PRICING_CONTRACT=PASS")
        print("WEB_INLINE_DISPATCH_FEEDBACK=PASS")
        print("WEB_CANDIDATE=PASS")
        print("DIRECTOR_API_WORKER_CUTOVER=PASS")
        print("DEV_WEB_CUTOVER=PASS")
        print("NON_TARGET_RUNTIME_UNCHANGED=PASS")
        print("PRODUCTION_TOUCH=NONE")
        print(f"failed_stage={latest_stage}")
        print(f"existing_orphan_children={total}")
        print(f"active_orphan_children={active}")
        print(f"old_web_image={old_web_image}")
        print(f"new_web_image={web_image}")
        if active:
            print("NEXT=REFRESH_FAILED_SCENE_CHECK_PRICE; ORPHAN_GUARD_WILL_BLOCK_DUPLICATE_RENDER_UNTIL_EXISTING_CHILDREN_SETTLE")
        else:
            print("NEXT=REFRESH_FAILED_SCENE_CHECK_PRICE_THEN_CONFIRM_CREATE")
        return 0
    except Exception as exc:
        print(f"DEV_CHILD_PRICING_REPAIR_FAIL={type(exc).__name__}:{exc}", flush=True)
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
