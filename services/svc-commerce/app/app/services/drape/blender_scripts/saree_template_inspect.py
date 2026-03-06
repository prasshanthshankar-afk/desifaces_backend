from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple

# Blender-only imports
import bpy  # type: ignore
import bmesh  # type: ignore


def _as_bool_env(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _safe_write_json(path: str, obj: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except Exception:
        # last resort: print to stdout (manual runs)
        try:
            print(json.dumps(obj, indent=2))
        except Exception:
            pass


def _iter_mesh_objects() -> List[Any]:
    out = []
    for obj in bpy.data.objects:
        try:
            if obj is None:
                continue
            if obj.type != "MESH":
                continue
            if getattr(obj, "hide_render", False):
                continue
            out.append(obj)
        except Exception:
            continue
    return out


def _score_candidate(obj: Any) -> int:
    name = (obj.name or "").lower()
    score = 0

    # canonical best pick
    if name == "df_saree_plane":
        score += 10_000

    for kw, w in [
        ("df_saree", 500),
        ("saree", 300),
        ("sari", 250),
        ("cloth", 120),
        ("garment", 80),
        ("drape", 60),
        ("overlay", 40),
        ("plane", 20),
    ]:
        if kw in name:
            score += w

    try:
        bb = obj.bound_box
        xs = [p[0] for p in bb]
        ys = [p[1] for p in bb]
        xr = max(xs) - min(xs)
        yr = max(ys) - min(ys)
        area = max(0.0, xr * yr)
        if area > 0.01:
            score += int(min(1000, area * 100))
    except Exception:
        pass

    try:
        ms = len(getattr(obj, "material_slots", []) or [])
        if ms > 0:
            score += 50 + min(200, ms * 10)
    except Exception:
        pass

    return score


def _pick_mesh() -> Tuple[Optional[Any], Dict[str, Any]]:
    mesh_objs = _iter_mesh_objects()
    info: Dict[str, Any] = {"mesh_count": len(mesh_objs), "strategy": None, "picked": None, "top_candidates": []}

    if not mesh_objs:
        info["strategy"] = "none"
        return None, info

    for obj in mesh_objs:
        if (obj.name or "") == "DF_SAREE_PLANE":
            info["strategy"] = "exact_df_saree_plane"
            info["picked"] = obj.name
            return obj, info

    scored = []
    for obj in mesh_objs:
        scored.append((_score_candidate(obj), obj))
    scored.sort(key=lambda t: t[0], reverse=True)

    info["strategy"] = "scored"
    info["picked"] = scored[0][1].name if scored else None
    info["top_candidates"] = [{"name": o.name, "score": int(s)} for (s, o) in scored[:8]]
    return (scored[0][1] if scored else None), info


def _axis_probe_for_object(obj: Any, *, use_evaluated: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "use_evaluated": bool(use_evaluated)}
    bm = None
    mesh_eval = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()

        if use_evaluated:
            obj_eval = obj.evaluated_get(depsgraph)
            mesh_eval = obj_eval.to_mesh()
            if mesh_eval is None:
                out["error"] = "to_mesh_returned_none"
                return out
            bm = bmesh.new()
            bm.from_mesh(mesh_eval)
            verts = bm.verts
        else:
            mesh = obj.data
            bm = bmesh.new()
            bm.from_mesh(mesh)
            verts = bm.verts

        if not verts:
            out["error"] = "no_verts"
            return out

        xs = [v.co.x for v in verts]
        ys = [v.co.y for v in verts]
        zs = [v.co.z for v in verts]

        xmin, xmax = float(min(xs)), float(max(xs))
        ymin, ymax = float(min(ys)), float(max(ys))
        zmin, zmax = float(min(zs)), float(max(zs))

        xr = float(xmax - xmin)
        yr = float(ymax - ymin)
        zr = float(zmax - zmin)

        out.update(
            {
                "ok": True,
                "n": int(len(verts)),
                "xrange": xr,
                "yrange": yr,
                "zrange": zr,
                "min_range": float(min(xr, yr, zr)),
                "max_range": float(max(xr, yr, zr)),
                "bounds": {"x": [xmin, xmax], "y": [ymin, ymax], "z": [zmin, zmax]},
            }
        )

        eps = 1e-6
        out["flat_like"] = bool(zr <= eps)
        return out

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    finally:
        try:
            if bm is not None:
                bm.free()
        except Exception:
            pass
        try:
            if use_evaluated and mesh_eval is not None:
                # clear evaluated mesh to prevent leaks
                obj_eval = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                obj_eval.to_mesh_clear()
        except Exception:
            pass


def _split_argv(argv: List[str]) -> Tuple[List[str], List[str]]:
    """
    Return (after_double_dash, full_minus_prog).
    Blender frequently includes '--' in sys.argv for python scripts, but not always
    depending on invocation. We'll try both.
    """
    full_minus_prog = argv[1:] if len(argv) > 1 else []
    if "--" in argv:
        i = argv.index("--")
        return argv[i + 1 :], full_minus_prog
    return [], full_minus_prog


def _manual_find_flag(argv: List[str], flag: str) -> Optional[str]:
    """
    Scan argv for '--flag value' (both in after '--' and in full argv).
    """
    try:
        for i in range(len(argv) - 1):
            if argv[i] == flag:
                return argv[i + 1]
    except Exception:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="saree_template_inspect", add_help=True)
    # NOTE: not required here — we enforce after we try all extraction paths
    parser.add_argument("--template", required=False, help="Path to .blend template")
    parser.add_argument("--out", required=False, help="Output JSON path")

    after_dd, full_minus_prog = _split_argv(sys.argv)

    # Try parse both ways; Blender argv can be inconsistent across invocations.
    args = None
    unknown: List[str] = []
    for candidate in (after_dd, full_minus_prog):
        try:
            a, u = parser.parse_known_args(candidate)
            if a.template or a.out:
                args = a
                unknown = u
                break
        except Exception:
            continue

    # If parse didn't yield anything useful, parse full_minus_prog anyway
    if args is None:
        args, unknown = parser.parse_known_args(full_minus_prog)

    template_path = (getattr(args, "template", None) or "").strip() if args else ""
    out_path = (getattr(args, "out", None) or "").strip() if args else ""

    # Manual scan across BOTH argv variants (handles weird '--' behavior)
    if not template_path:
        template_path = (
            _manual_find_flag(after_dd, "--template")
            or _manual_find_flag(full_minus_prog, "--template")
            or ""
        )
    if not out_path:
        out_path = (
            _manual_find_flag(after_dd, "--out")
            or _manual_find_flag(full_minus_prog, "--out")
            or ""
        )

    # Fallbacks:
    # - template can be current opened blend
    if not template_path:
        try:
            template_path = bpy.data.filepath or ""
        except Exception:
            template_path = ""

    # - out defaults for manual runs (provider should still pass --out)
    if not out_path:
        out_path = os.getenv("DF_SAREE_TEMPLATE_INSPECT_OUT", "/tmp/saree_template_inspect.json")

    report: Dict[str, Any] = {
        "ok": False,
        "template": template_path,
        "picked_mesh": None,
        "pick_info": {},
        "modifier_count": 0,
        "material_slot_count": 0,
        "baked_axis_probe": {},
        "base_axis_probe": {},
        "warnings": [],
        "errors": [],
        "argv": {
            "sys_argv": list(sys.argv),
            "after_double_dash": after_dd,
            "full_minus_prog": full_minus_prog,
            "unknown": unknown,
        },
    }

    try:
        if not template_path or not os.path.exists(template_path):
            report["errors"].append(f"TEMPLATE_MISSING_OR_EMPTY: {template_path!r}")
            _safe_write_json(out_path, report)
            return 2

        # Often already open via -b, but safe to open
        try:
            bpy.ops.wm.open_mainfile(filepath=template_path)
        except Exception:
            report["warnings"].append("open_mainfile_failed_or_not_needed")

        picked, pick_info = _pick_mesh()
        report["pick_info"] = pick_info

        if picked is None:
            report["errors"].append("NO_MESH_OBJECTS_FOUND")
            _safe_write_json(out_path, report)
            return 3

        report["picked_mesh"] = picked.name

        try:
            report["modifier_count"] = int(len(getattr(picked, "modifiers", []) or []))
        except Exception:
            report["modifier_count"] = 0

        try:
            report["material_slot_count"] = int(len(getattr(picked, "material_slots", []) or []))
        except Exception:
            report["material_slot_count"] = 0

        base_probe = _axis_probe_for_object(picked, use_evaluated=False)
        eval_probe = _axis_probe_for_object(picked, use_evaluated=True)
        report["base_axis_probe"] = base_probe
        report["baked_axis_probe"] = eval_probe if eval_probe.get("ok") else base_probe

        # Contract logic: overlay templates may be flat. Nonflat is optional.
        require_nonflat = _as_bool_env("DF_SAREE_TEMPLATE_REQUIRE_NONFLAT", False)

        probe_ok = bool(report["baked_axis_probe"].get("ok"))
        flat_like = bool(report["baked_axis_probe"].get("flat_like"))

        if require_nonflat and flat_like:
            report["errors"].append("TEMPLATE_REJECTED_NONFLAT_REQUIRED")
            report["ok"] = False
        else:
            report["ok"] = probe_ok

        _safe_write_json(out_path, report)
        return 0 if report["ok"] else 4

    except Exception:
        report["errors"].append("UNCAUGHT_EXCEPTION")
        report["errors"].append(traceback.format_exc())
        _safe_write_json(out_path, report)
        return 5


if __name__ == "__main__":
    sys.exit(main())