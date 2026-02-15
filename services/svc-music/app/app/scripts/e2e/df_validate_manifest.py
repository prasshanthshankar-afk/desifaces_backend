import json
import sys
from typing import Any, List, Tuple, Optional

def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _find_clip_manifest(obj: Any) -> List[Tuple[str, Any]]:
    hits: List[Tuple[str, Any]] = []

    def walk(x: Any, path: str) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                p2 = f"{path}.{k}"
                if k == "clip_manifest":
                    hits.append((p2, v))
                walk(v, p2)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")

    walk(obj, "$")
    return hits

def _looks_like_manifest(m: Any) -> bool:
    if not isinstance(m, dict):
        return False
    # be lenient: accept if it has any of these common keys
    keys = set(m.keys())
    return bool(keys.intersection({"clips", "exports", "timeline", "version", "meta"}))

def main() -> int:
    if len(sys.argv) not in (3, 4):
        print("Usage:")
        print("  df_validate_manifest.py <json_file> <expect_manifest:true|false>")
        print("  df_validate_manifest.py <status_json> <project_json> <expect_manifest:true|false>")
        return 2

    if len(sys.argv) == 3:
        paths = [sys.argv[1]]
        expect = sys.argv[2].strip().lower()
    else:
        paths = [sys.argv[1], sys.argv[2]]
        expect = sys.argv[3].strip().lower()

    expect_manifest = expect == "true"

    found_path: Optional[str] = None
    found_manifest: Any = None

    for p in paths:
        try:
            j = _load(p)
        except Exception as e:
            print(f"FAIL: could not read JSON: {p} ({e})")
            return 2

        hits = _find_clip_manifest(j)
        if hits:
            # pick first plausible manifest
            for hp, hv in hits:
                if _looks_like_manifest(hv):
                    found_path = f"{p}:{hp}"
                    found_manifest = hv
                    break
            if found_manifest is None:
                found_path = f"{p}:{hits[0][0]}"
                found_manifest = hits[0][1]
            break

    if expect_manifest:
        if found_manifest is None:
            print("FAIL: clip_manifest not found in provided JSON file(s).")
            print("Checked:", ", ".join(paths))
            return 1
        if not _looks_like_manifest(found_manifest):
            print(f"FAIL: clip_manifest found but does not look valid at {found_path}")
            t = type(found_manifest).__name__
            print(f"type={t}")
            return 1

        # Extra: if clips exists, it should be a list
        if isinstance(found_manifest, dict) and "clips" in found_manifest:
            if not isinstance(found_manifest["clips"], list):
                print(f"FAIL: clip_manifest.clips is not a list at {found_path}")
                return 1

        print(f"OK: clip_manifest found at {found_path}")
        return 0

    # expect_manifest == false
    if found_manifest is not None:
        print(f"FAIL: clip_manifest was found but expect_manifest=false (found at {found_path})")
        return 1

    print("OK: clip_manifest not present (as expected).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())