#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

ROOT = Path(os.environ.get("ROOT", "/home/azureuser/workspace/desifaces"))
RELEASE_REF = "release/v3-production-backend-closeout-20260904"
PROMOTION_PATH = "scripts/promote-certify-multiperson-aspect-face-readurl-prod-20260909.sh"
BACKEND_REPO = "prasshanthshankar-afk/desifaces_backend"
WEB_REPO = "prasshanthshankar-afk/desifaces_web"
BACKEND_COMMIT = "6aaef39c524ff4d5472c432936046a73fe4bbb1f"
WEB_COMMIT = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"

BACKEND_BLOBS = {
    "services/svc-director/app/app/face_execution_runtime.py": "59462638a9464b3800ae4330c9983a58a87a4992",
    "services/svc-director/app/app/fusion_input_performance.py": "531327d60d1b7f4cddeabd614e3d5887b7297acb",
    "services/svc-director/app/app/studio_aspect_routes.py": "4841800302c78b43e20964ad68a974df444b5bae",
    "services/svc-director/app/app/studio_routes_runtime.py": "44da7f2299d241becd42a4a280efef217cdffb5e",
    "services/svc-face/app/app/api/routes/face_media.py": "59115de9687becd2cf9ab835130b439166d4e1f0",
    "services/svc-face/app/app/api/__init__.py": "870d249af1e54b95d633ad036d703bd43165dc4d",
}
WEB_BLOBS = {
    "lib/multiperson-aspect.ts": "8d4dddfe5bea0f4456a89f29e253c3ff1065fa03",
    "lib/client.ts": "cd2f27766fb85e2325ba83104c61968924b1d76b",
    "components/MultiPersonAspectControls.tsx": "3838ce47bed39538681a070f011e57a3f5a01b92",
    "app/app/multi-person/multi-person-aspect.css": "f0e5437685cda96bca341ce021a9d0cecb632d1e",
    "app/app/multi-person/layout.tsx": "2699b085b75a68ed850169c560722d6ad5809fc9",
}


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "desifaces-prod-launcher"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def find_web_source() -> Path:
    candidates = [
        Path("/home/azureuser/workspace/desifaces-web/web"),
        Path("/home/azureuser/workspace/desifaces-web-review/web"),
        Path("/home/azureuser/workspace/desifaces_web/web"),
    ]
    for candidate in candidates:
        if (candidate / "Dockerfile").is_file() and (candidate / "components/MultiPersonDirector.tsx").is_file():
            return candidate
    raise SystemExit("FAIL: active production web source not found")


def write_blob(repo: str, sha: str, destination: Path) -> None:
    url = f"https://api.github.com/repos/{repo}/git/blobs/{sha}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "desifaces-prod-launcher"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    if doc.get("encoding") != "base64" or not doc.get("content"):
        raise RuntimeError(f"invalid GitHub blob response for {repo}@{sha}")
    data = base64.b64decode(doc["content"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + f".hotfix.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, destination)


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"FAIL: production package missing: {ROOT}")
    web_src = find_web_source()

    print("SOURCE_TRANSPORT=IMMUTABLE_GITHUB_BLOBS")
    print(f"PRODUCTION_PACKAGE={ROOT}")
    print(f"WEB_SOURCE={web_src}")
    print("LAUNCHER_MODE=PYTHON_SYNTAX_GATED_NO_GIT")

    promotion_url = (
        f"https://raw.githubusercontent.com/{BACKEND_REPO}/{RELEASE_REF}/{PROMOTION_PATH}"
    )
    promotion = fetch_text(promotion_url)
    promotion = promotion.replace(
        'BACKEND_COMMIT="f459ae128d0bf5e2b0d6b63dfaf476df11c4731b"',
        f'BACKEND_COMMIT="{BACKEND_COMMIT}"',
    )
    promotion = promotion.replace(
        'WEB_COMMIT="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"',
        f'WEB_COMMIT="{WEB_COMMIT}"',
    )

    start_marker = 'echo\necho "===== 1. SYNC IMMUTABLE SOURCE ====="'
    end_marker = 'echo\necho "===== 2. STATIC CONTRACT GATES ====="'
    start = promotion.find(start_marker)
    end = promotion.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        raise SystemExit("FAIL: promotion source-sync markers not found")

    with tempfile.TemporaryDirectory(prefix="desifaces-multiperson-promote-") as td:
        td_path = Path(td)
        sync_py = td_path / "sync_blobs.py"
        sync_py.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parent)) + ")\n"
        )

        manifest = td_path / "manifest.json"
        manifest.write_text(json.dumps({
            "root": str(ROOT),
            "web": str(web_src),
            "backend_repo": BACKEND_REPO,
            "web_repo": WEB_REPO,
            "backend_blobs": BACKEND_BLOBS,
            "web_blobs": WEB_BLOBS,
        }))

        helper = td_path / "blob_sync_runtime.py"
        helper.write_text(
            "import base64,json,os,sys,urllib.request\n"
            "from pathlib import Path\n"
            "m=json.loads(Path(sys.argv[1]).read_text())\n"
            "def one(repo,sha,dest):\n"
            "  req=urllib.request.Request(f'https://api.github.com/repos/{repo}/git/blobs/{sha}',headers={'Accept':'application/vnd.github+json','User-Agent':'desifaces-prod-launcher'})\n"
            "  with urllib.request.urlopen(req,timeout=30) as r: doc=json.loads(r.read().decode())\n"
            "  if doc.get('encoding')!='base64' or not doc.get('content'): raise SystemExit(f'invalid blob {repo}@{sha}')\n"
            "  p=Path(dest); p.parent.mkdir(parents=True,exist_ok=True); t=p.with_name(p.name+'.hotfix')\n"
            "  t.write_bytes(base64.b64decode(doc['content'])); os.replace(t,p)\n"
            "for rel,sha in m['backend_blobs'].items(): one(m['backend_repo'],sha,str(Path(m['root'])/rel))\n"
            "for rel,sha in m['web_blobs'].items(): one(m['web_repo'],sha,str(Path(m['web'])/rel))\n"
            "print('SOURCE_SYNC=PASS')\n"
        )

        replacement = (
            'echo\n'
            'echo "===== 1. SYNC IMMUTABLE SOURCE ====="\n'
            f'python3 "{helper}" "{manifest}"\n'
        )
        patched = promotion[:start] + replacement + promotion[end:]

        for forbidden in (
            "raw.githubusercontent.com/$BACKEND_REPO/$BACKEND_COMMIT/$f",
            "raw.githubusercontent.com/$WEB_REPO/$WEB_COMMIT/web/$f",
        ):
            if forbidden in patched:
                raise SystemExit(f"FAIL: raw application-source loop remained: {forbidden}")

        ready = td_path / "promotion.ready.sh"
        ready.write_text(patched)

        subprocess.run(["bash", "-n", str(ready)], check=True)
        print("LAUNCHER_SYNTAX_GATE=PASS")
        print(f"SOURCE_COMMIT={BACKEND_COMMIT}")
        print(f"WEB_COMMIT={WEB_COMMIT}")
        subprocess.run(["bash", str(ready)], check=True)


if __name__ == "__main__":
    main()
