from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import struct
import sys
import time
import traceback
import zlib
from typing import Any, Dict, List, Optional, Tuple

import bpy  # type: ignore

logger = logging.getLogger(__name__)


# -------------------------
# args
# -------------------------


def _parse_args() -> Optional[argparse.Namespace]:
    argv = sys.argv
    if "--" not in argv:
        return None
    argv = argv[argv.index("--") + 1 :]

    p = argparse.ArgumentParser()
    p.add_argument("--template", required=True)
    p.add_argument("--saree_texture", required=True)
    p.add_argument("--out", required=True)

    # Primary mask (usually saree_alpha.png)
    p.add_argument("--alpha_mask", default="")
    p.add_argument("--alpha_mask_invert", action="store_true")

    # Optional pallu mask (usually pallu_alpha.png). If not provided, we auto-discover next to alpha_mask.
    p.add_argument("--pallu_mask", default="")
    p.add_argument("--pallu_mask_invert", action="store_true")

    p.add_argument("--res", type=int, default=1024)
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)

    p.add_argument(
        "--engine",
        default="BLENDER_EEVEE_NEXT",
        help="Render engine: BLENDER_EEVEE_NEXT (default). Aliases accepted: BLENDER_EEVEE, EEVEE, EEVEE_NEXT, AUTO.",
    )

    p.add_argument("--replace_all", action="store_true")  # compat
    p.add_argument("--material_override", type=int, default=1)
    p.add_argument("--only_saree_objects", type=int, default=1)

    p.add_argument("--variant_idx", type=int, default=-1)
    p.add_argument("--variant_seed", default="")

    p.add_argument("--meta_json", default="")
    p.add_argument("--fail_if_output_missing", action="store_true")

    p.add_argument("--eevee_samples", type=int, default=16)

    return p.parse_args(argv)


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    try:
        v = (os.getenv(name) or "").strip()
        return float(v) if v else default
    except Exception:
        return default


def _env_csv(name: str, default: str = "") -> List[str]:
    s = (os.getenv(name) or default).strip()
    if not s:
        return []
    return [p.strip().lower() for p in s.split(",") if p.strip()]


# -------------------------
# render + scene config
# -------------------------


def _allowed_render_engines(scene: Any) -> List[str]:
    try:
        items = scene.render.bl_rna.properties["engine"].enum_items
        return sorted({it.identifier for it in items})
    except Exception:
        return []


def _choose_engine(scene: Any, requested: str) -> str:
    req = (requested or "").strip()
    up = req.upper() if req else ""
    if up in ("", "AUTO"):
        desired = str(scene.render.engine)
    else:
        alias = {
            "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT",
            "EEVEE": "BLENDER_EEVEE_NEXT",
            "EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
            "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
            "CYCLES": "CYCLES",
            "BLENDER_WORKBENCH": "BLENDER_WORKBENCH",
            "WORKBENCH": "BLENDER_WORKBENCH",
        }
        desired = alias.get(up, req)

    allowed = set(_allowed_render_engines(scene))
    if not allowed:
        return desired
    if desired in allowed:
        return desired
    if desired.upper() == "CYCLES" and "BLENDER_EEVEE_NEXT" in allowed:
        return "BLENDER_EEVEE_NEXT"
    if "BLENDER_EEVEE_NEXT" in allowed:
        return "BLENDER_EEVEE_NEXT"
    return sorted(allowed)[0]


def _disable_compositor_and_sequencer(scene: Any) -> None:
    try:
        scene.use_nodes = False
    except Exception:
        pass
    try:
        scene.render.use_compositing = False
    except Exception:
        pass
    try:
        scene.render.use_sequencer = False
    except Exception:
        pass


def _configure_render_output(scene: Any, *, out_path: str, width: int, height: int) -> None:
    scene.render.filepath = out_path
    scene.render.resolution_x = int(width)
    scene.render.resolution_y = int(height)
    scene.render.resolution_percentage = 100

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    try:
        scene.render.image_settings.compression = 15
    except Exception:
        pass

    # alpha handling
    try:
        if hasattr(scene.render.image_settings, "alpha_mode"):
            scene.render.image_settings.alpha_mode = "STRAIGHT"
    except Exception:
        pass
    try:
        scene.render.film_transparent = True
    except Exception:
        pass

    # keep colors stable
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception:
        pass

    # ensure world doesn't "accidentally" show
    try:
        if scene.world:
            scene.world.use_nodes = False
            if hasattr(scene.world, "color"):
                scene.world.color = (0.0, 0.0, 0.0)
    except Exception:
        pass


def _ensure_camera(scene: Any) -> None:
    """
    Force stable top-down orthographic camera.
    """
    cam_obj = None
    for obj in scene.objects:
        if obj.type == "CAMERA":
            cam_obj = obj
            break

    if cam_obj is None:
        cam_data = bpy.data.cameras.new("DF_CAMERA")
        cam_obj = bpy.data.objects.new("DF_CAMERA", cam_data)
        scene.collection.objects.link(cam_obj)

    scene.camera = cam_obj

    cam_obj.location = (0.0, 0.0, 5.0)
    cam_obj.rotation_euler = (0.0, 0.0, 0.0)

    try:
        cam_obj.data.type = "ORTHO"
    except Exception:
        pass

    try:
        cam_obj.data.ortho_scale = 2.2
    except Exception:
        pass


def _ensure_camera_ortho(scene: Any) -> None:
    cam = scene.camera
    if not cam or cam.type != "CAMERA":
        return
    try:
        cam.data.type = "ORTHO"
    except Exception:
        pass
    try:
        if float(getattr(cam.data, "ortho_scale", 0.0) or 0.0) <= 0.0:
            cam.data.ortho_scale = 4.2
    except Exception:
        pass


def _ensure_light(scene: Any) -> None:
    if any(o.type == "LIGHT" for o in scene.objects):
        return
    light_data = bpy.data.lights.new(name="DF_KEY", type="SUN")
    light_data.energy = 3.0
    light_obj = bpy.data.objects.new(name="DF_KEY", object_data=light_data)
    scene.collection.objects.link(light_obj)
    light_obj.location = (2.0, -2.0, 4.0)


def _unexclude_all_layer_collections(view_layer: Any) -> None:
    def _walk(lc: Any) -> None:
        try:
            lc.exclude = False
        except Exception:
            pass
        try:
            lc.hide_viewport = False
        except Exception:
            pass
        try:
            lc.collection.hide_viewport = False
        except Exception:
            pass
        try:
            lc.collection.hide_render = False
        except Exception:
            pass
        for ch in getattr(lc, "children", []) or []:
            _walk(ch)

    try:
        _walk(view_layer.layer_collection)
    except Exception:
        pass


def _unhide_all_objects(scene: Any) -> None:
    for obj in scene.objects:
        try:
            obj.hide_render = False
        except Exception:
            pass
        try:
            obj.hide_set(False)
        except Exception:
            pass


# -------------------------
# target selection
# -------------------------


def _poly_count(obj: Any) -> int:
    try:
        return int(len(obj.data.polygons))
    except Exception:
        return 0


def _dims(obj: Any) -> Tuple[float, float, float]:
    try:
        d = getattr(obj, "dimensions", None)
        if d is None:
            return (0.0, 0.0, 0.0)
        return (float(d[0]), float(d[1]), float(d[2]))
    except Exception:
        return (0.0, 0.0, 0.0)


def _safe_lower(x: Any) -> str:
    try:
        return str(x or "").strip().lower()
    except Exception:
        return ""


def _is_probably_body_mesh(name: str) -> bool:
    s = _safe_lower(name)
    return any(
        k in s
        for k in (
            "body",
            "skin",
            "head",
            "face",
            "hair",
            "eyebrow",
            "lashes",
            "teeth",
            "tongue",
            "avatar",
            "mannequin",
            "armature",
            "rig",
        )
    )


def _is_saree_named(name: str) -> bool:
    s = _safe_lower(name)
    return any(
        k in s
        for k in (
            "df_saree",
            "saree",
            "sari",
            "saari",
            "pallu",
            "pallav",
            "pleat",
            "drape",
            "endpiece",
            "cloth",
        )
    )


def _is_pallu_named(name: str) -> bool:
    s = _safe_lower(name)
    return any(k in s for k in ("pallu", "pallav", "endpiece", "palloo", "pallavum"))


def _obj_collections_lower(obj: Any) -> List[str]:
    out: List[str] = []
    try:
        for c in getattr(obj, "users_collection", []) or []:
            out.append(_safe_lower(getattr(c, "name", "")))
    except Exception:
        pass
    return out


def _score_mesh_candidate(
    obj: Any,
    *,
    name_hints: List[str],
    col_hints: List[str],
    exclude_hints: List[str],
) -> Tuple[float, Dict[str, Any]]:
    n = _safe_lower(getattr(obj, "name", ""))
    cols = _obj_collections_lower(obj)

    for bad in exclude_hints:
        if bad and (bad in n or any(bad in c for c in cols)):
            return -1e9, {"excluded_by": bad, "collections": cols}

    if _is_probably_body_mesh(n):
        return -1e9, {"excluded_by": "body_like", "collections": cols}

    score = 0.0
    reasons: List[str] = []

    # df_garment_kind (best)
    try:
        gk = _safe_lower(obj.get("df_garment_kind", ""))
        if gk in ("saree", "sari", "saari", "pallu"):
            score += 250.0
            reasons.append(f"prop:df_garment_kind={gk}")
    except Exception:
        pass

    # name hints
    for h in name_hints:
        if h and h in n:
            score += 100.0
            reasons.append(f"name:{h}")

    # collection hints
    for h in col_hints:
        if h and any(h in c for c in cols):
            score += 70.0
            reasons.append(f"col:{h}")

    # geometry heuristics
    pc = _poly_count(obj)
    dx, dy, dz = _dims(obj)
    score += min(90.0, (pc ** 0.5) * 1.2)
    score += min(60.0, max(dx, dy) * 8.0)
    reasons.append(f"poly:{pc}")
    reasons.append(f"dims:{dx:.3f},{dy:.3f},{dz:.3f}")

    # visibility
    try:
        if bool(getattr(obj, "hide_render", False)):
            score -= 80.0
            reasons.append("hide_render")
    except Exception:
        pass

    return score, {"reasons": reasons, "collections": cols, "poly": pc, "dims": [dx, dy, dz]}


def _find_pallu_meshes(meshes: List[Any]) -> List[Any]:
    out: List[Any] = []
    for o in meshes:
        if o.type != "MESH":
            continue
        n = getattr(o, "name", "") or ""
        if _is_probably_body_mesh(n):
            continue
        if _is_pallu_named(n):
            out.append(o)
    out = sorted(out, key=lambda o: _safe_lower(getattr(o, "name", "")))
    return out


def _find_saree_meshes(scene: Any) -> Tuple[List[Any], Dict[str, Any]]:
    meshes = [o for o in scene.objects if o.type == "MESH"]
    info: Dict[str, Any] = {
        "scene_candidates": len(meshes),
        "strategy": None,
        "matched": 0,
        "picked_names": [],
        "pallu_matched": 0,
        "pallu_names": [],
        "hints": {
            "name_hints": _env_csv("DF_SAREE_MESH_NAME_HINTS", "df_saree,saree,sari,saari,pallu,pleat,drape,cloth,endpiece"),
            "collection_hints": _env_csv("DF_SAREE_MESH_COLLECTION_HINTS", "saree,garment,cloth,drape"),
            "exclude_hints": _env_csv("DF_SAREE_MESH_EXCLUDE_HINTS", "body,skin,face,head,hair,rig,armature,camera,light"),
        },
        "candidates": [
            {
                "name": getattr(o, "name", ""),
                "poly": _poly_count(o),
                "dims": list(_dims(o)),
                "hide_render": bool(getattr(o, "hide_render", False)),
                "collections": _obj_collections_lower(o),
                "df_garment_kind": _safe_lower(getattr(o, "get", lambda *_: "")("df_garment_kind", "")),
            }
            for o in meshes[:200]
        ],
        "top_scored": [],
    }
    if not meshes:
        info["strategy"] = "none"
        return [], info

    # tagged by df_garment_kind
    tagged: List[Any] = []
    for o in meshes:
        try:
            if _safe_lower(o.get("df_garment_kind", "")) == "saree":
                tagged.append(o)
        except Exception:
            pass

    pallu = _find_pallu_meshes(meshes)
    info["pallu_matched"] = len(pallu)
    info["pallu_names"] = [getattr(o, "name", "") for o in pallu]

    if tagged:
        def _rank(o: Any) -> Tuple[int, str]:
            n = _safe_lower(getattr(o, "name", ""))
            if "skirt" in n:
                return (0, n)
            if "pallu" in n or "pallav" in n or "endpiece" in n:
                return (1, n)
            if "plane" in n:
                return (2, n)
            return (3, n)

        base = sorted(tagged, key=_rank)
        info["strategy"] = "df_garment_kind"
    else:
        exact = [o for o in meshes if _safe_lower(getattr(o, "name", "")) == "df_saree_plane"]
        if exact:
            base = exact
            info["strategy"] = "exact_df_saree_plane"
        else:
            named = [
                o
                for o in meshes
                if _is_saree_named(getattr(o, "name", "")) and not _is_probably_body_mesh(getattr(o, "name", ""))
            ]
            if named:
                base = sorted(named, key=lambda o: _safe_lower(getattr(o, "name", "")))
                info["strategy"] = "name_keywords"
            else:
                # ✅ score-based fallback (template-agnostic)
                nh = info["hints"]["name_hints"]
                ch = info["hints"]["collection_hints"]
                ex = info["hints"]["exclude_hints"]

                scored: List[Tuple[float, Any, Dict[str, Any]]] = []
                for o in meshes:
                    sc, why = _score_mesh_candidate(o, name_hints=nh, col_hints=ch, exclude_hints=ex)
                    scored.append((sc, o, why))

                scored.sort(key=lambda t: t[0], reverse=True)
                info["top_scored"] = [
                    {"name": getattr(o, "name", ""), "score": float(sc), **why}
                    for sc, o, why in scored[: min(25, len(scored))]
                ]

                # pick top N with positive score
                max_pick = _clamp_int(os.getenv("DF_SAREE_MAX_MESHES", "3"), default=3, lo=1, hi=12)
                base = [o for sc, o, _why in scored if sc > 0.0][:max_pick]

                if base:
                    info["strategy"] = "score_fallback"
                else:
                    info["strategy"] = "no_match_fail_fast"
                    return [], info

    # UNION IN pallu meshes even if base strategy returned only DF_SAREE_PLANE
    seen = set()
    merged: List[Any] = []
    for o in (base + pallu):
        nm = getattr(o, "name", "") or ""
        if nm in seen:
            continue
        seen.add(nm)
        merged.append(o)

    info["matched"] = len(merged)
    info["picked_names"] = [getattr(o, "name", "") for o in merged]
    return merged, info


def _hide_everything_except(scene: Any, target_meshes: List[Any]) -> None:
    keep = set(target_meshes)
    for obj in scene.objects:
        if obj.type in ("CAMERA", "LIGHT"):
            try:
                obj.hide_render = False
                obj.hide_set(False)
            except Exception:
                pass
            continue
        try:
            obj.hide_render = (obj not in keep)
        except Exception:
            pass
        try:
            # keep viewport visible; render hides are enough
            obj.hide_set(False)
        except Exception:
            pass
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


def _auto_fit_ortho_camera(scene: Any, targets: List[Any], *, pad: float) -> Optional[Dict[str, Any]]:
    cam = scene.camera
    if not cam or cam.type != "CAMERA":
        return None
    try:
        cam.data.type = "ORTHO"
    except Exception:
        pass

    max_xy = 0.0
    for o in targets:
        dx, dy, _dz = _dims(o)
        max_xy = max(max_xy, float(dx), float(dy))

    if max_xy <= 0.0:
        return None

    before = float(getattr(cam.data, "ortho_scale", 0.0) or 0.0)
    desired = float(max_xy) * float(pad)

    try:
        cam.data.ortho_scale = desired
        after = float(getattr(cam.data, "ortho_scale", 0.0) or 0.0)
    except Exception:
        return None

    return {"before": before, "after": after, "max_xy": max_xy, "pad": float(pad)}


def _bump_ortho_scale(scene: Any, factor: float) -> Optional[float]:
    cam = scene.camera
    if not cam or cam.type != "CAMERA":
        return None
    try:
        cam.data.type = "ORTHO"
    except Exception:
        pass
    try:
        s = float(getattr(cam.data, "ortho_scale", 0.0) or 0.0)
        if s <= 0.0:
            s = 4.2
        cam.data.ortho_scale = float(s * float(factor))
        return float(cam.data.ortho_scale)
    except Exception:
        return None


# -------------------------
# evaluated mesh bake + probe
# -------------------------


def _force_modifiers_on(obj: Any) -> Dict[str, Any]:
    mods = []
    try:
        for m in getattr(obj, "modifiers", []) or []:
            row = {"name": getattr(m, "name", ""), "type": getattr(m, "type", "")}
            try:
                row["show_viewport_before"] = bool(getattr(m, "show_viewport", True))
                m.show_viewport = True
            except Exception:
                pass
            try:
                row["show_render_before"] = bool(getattr(m, "show_render", True))
                m.show_render = True
            except Exception:
                pass
            mods.append(row)
    except Exception:
        pass
    return {"object": getattr(obj, "name", ""), "modifiers": mods}


def _bake_evaluated_mesh(scene: Any, obj: Any, *, baked_name: str = "DF_SAREE_BAKED") -> Any:
    deps = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(deps)

    try:
        mesh_eval = eval_obj.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    except TypeError:
        mesh_eval = eval_obj.to_mesh()

    if mesh_eval is None:
        raise RuntimeError(f"BAKE_EVAL_MESH_NONE: {getattr(obj, 'name', '')}")

    try:
        baked_mesh = mesh_eval.copy()
    except Exception as e:
        baked_mesh = bpy.data.meshes.new(f"{baked_name}_MESH")
        verts = [v.co[:] for v in mesh_eval.vertices]
        edges = [e.vertices[:] for e in mesh_eval.edges]
        faces = [p.vertices[:] for p in mesh_eval.polygons]
        baked_mesh.from_pydata(verts, edges, faces)
        baked_mesh.update()
        logger.warning("mesh_eval.copy() failed; used from_pydata fallback: %s", e)

    baked_mesh.name = f"{baked_name}_MESH"

    baked_obj = bpy.data.objects.new(baked_name, baked_mesh)
    try:
        baked_obj.matrix_world = obj.matrix_world.copy()
    except Exception:
        pass

    try:
        scene.collection.objects.link(baked_obj)
    except Exception:
        bpy.context.collection.objects.link(baked_obj)

    try:
        eval_obj.to_mesh_clear()
    except Exception:
        pass

    try:
        baked_obj.hide_render = False
    except Exception:
        pass

    return baked_obj


def _mesh_axis_probe(obj: Any, *, sample: int = 2500) -> Dict[str, Any]:
    try:
        verts = getattr(getattr(obj, "data", None), "vertices", None)
        if not verts:
            return {"ok": False, "reason": "no_vertices"}

        n = len(verts)
        step = max(1, n // max(1, sample))
        xmin = ymin = zmin = 1e18
        xmax = ymax = zmax = -1e18

        cnt = 0
        for i in range(0, n, step):
            v = verts[i].co
            x = float(v.x)
            y = float(v.y)
            z = float(v.z)
            xmin, xmax = min(xmin, x), max(xmax, x)
            ymin, ymax = min(ymin, y), max(ymax, y)
            zmin, zmax = min(zmin, z), max(zmax, z)
            cnt += 1
            if cnt >= sample:
                break

        xr = float(xmax - xmin)
        yr = float(ymax - ymin)
        zr = float(zmax - zmin)
        ranges = sorted([xr, yr, zr])
        min_r = ranges[0]
        max_r = ranges[2]

        flat = False
        if max_r > 0.05 and (min_r < 5e-5 or (min_r / max_r) < 1e-4):
            flat = True

        return {
            "ok": True,
            "n": n,
            "sampled": cnt,
            "xmin": xmin,
            "xmax": xmax,
            "ymin": ymin,
            "ymax": ymax,
            "zmin": zmin,
            "zmax": zmax,
            "xrange": xr,
            "yrange": yr,
            "zrange": zr,
            "min_range": min_r,
            "max_range": max_r,
            "flat_like": flat,
        }
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# -------------------------
# images + material
# -------------------------


def _load_image(img_path: str, *, non_color: bool = False):
    img = bpy.data.images.load(img_path, check_existing=True)
    try:
        img.reload()
    except Exception:
        pass
    try:
        if hasattr(img, "alpha_mode"):
            img.alpha_mode = "STRAIGHT"
    except Exception:
        pass
    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    return img


def _configure_material_for_eevee_alpha(mat: bpy.types.Material) -> None:
    try:
        mat.blend_method = "BLEND"
    except Exception:
        pass
    try:
        mat.shadow_method = "NONE"
    except Exception:
        pass
    try:
        mat.use_backface_culling = True
    except Exception:
        pass


def _make_overlay_material(
    *,
    saree_img,
    mask_img=None,
    mask_invert: bool = False,
    mask_img2=None,
    mask2_invert: bool = False,
    pallu_gain: float = 1.0,
    variant_idx: int,
    seed_hex: str,
) -> bpy.types.Material:
    """
    Emission overlay + alpha masks.

    CRITICAL FIX:
      - Fabric texture mapping is randomized for variant diversity.
      - Masks must stay aligned to mesh UV, so we DO NOT random-map masks.
    """
    name = f"DF_Saree_Overlay_Mat_v{int(variant_idx)}_{'m' if mask_img or mask_img2 else 'nom'}"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
    mat.use_nodes = True
    _configure_material_for_eevee_alpha(mat)

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    for n in list(nodes):
        nodes.remove(n)

    r = random.Random(int(seed_hex, 16) if seed_hex else int(variant_idx))

    uv_scale = 2.2 + r.random() * 1.0
    u_off = 0.03 + r.random() * 0.28
    v_off = 0.03 + r.random() * 0.28
    rot_z = (r.random() - 0.5) * 0.35

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (980, 0)

    emission = nodes.new("ShaderNodeEmission")
    emission.location = (650, 60)
    emission.inputs["Strength"].default_value = 1.0

    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (650, -140)

    mix = nodes.new("ShaderNodeMixShader")
    mix.location = (820, 10)

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (0, 30)

    # Random mapping ONLY for saree fabric texture
    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (220, 30)
    try:
        mapping.inputs["Location"].default_value = (float(u_off), float(v_off), 0.0)
        mapping.inputs["Rotation"].default_value = (0.0, 0.0, float(rot_z))
        mapping.inputs["Scale"].default_value = (float(uv_scale), float(uv_scale), 1.0)
    except Exception:
        pass

    tex = nodes.new("ShaderNodeTexImage")
    tex.location = (450, 60)
    tex.image = saree_img
    try:
        tex.extension = "REPEAT"
    except Exception:
        pass
    try:
        tex.interpolation = "Linear"
    except Exception:
        pass

    # Connect mapping to saree texture (variant diversity)
    try:
        links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
    except Exception:
        links.new(texcoord.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    links.new(tex.outputs["Color"], emission.inputs["Color"])

    def _mask_alpha_socket(mask_node: Any) -> Optional[Any]:
        try:
            return mask_node.outputs.get("Alpha")  # type: ignore[attr-defined]
        except Exception:
            return None

    def _invert_socket(sock: Any, *, xloc: int, yloc: int) -> Any:
        one = nodes.new("ShaderNodeValue")
        one.location = (xloc, yloc - 70)
        one.outputs[0].default_value = 1.0
        inv = nodes.new("ShaderNodeMath")
        inv.location = (xloc + 170, yloc)
        inv.operation = "SUBTRACT"
        links.new(one.outputs[0], inv.inputs[0])
        links.new(sock, inv.inputs[1])
        return inv.outputs[0]

    def _gain_socket(sock: Any, gain: float, *, xloc: int, yloc: int) -> Any:
        g = float(max(0.0, gain))
        if abs(g - 1.0) < 1e-6:
            return sock
        mul = nodes.new("ShaderNodeMath")
        mul.location = (xloc + 170, yloc - 40)
        mul.operation = "MULTIPLY"
        mul.use_clamp = True
        mul.inputs[1].default_value = g
        links.new(sock, mul.inputs[0])
        return mul.outputs[0]

    def _threshold_socket(sock: Any, thr: float, feather: float, *, xloc: int, yloc: int) -> Any:
        thr = float(thr)
        feather = float(max(1e-6, feather))
        if thr <= 0.0:
            return sock
        mr = nodes.new("ShaderNodeMapRange")
        mr.location = (xloc, yloc)
        mr.clamp = True
        mr.inputs[1].default_value = thr
        mr.inputs[2].default_value = thr + feather
        mr.inputs[3].default_value = 0.0
        mr.inputs[4].default_value = 1.0
        links.new(sock, mr.inputs[0])
        return mr.outputs[0]

    alpha1 = None
    if mask_img is not None:
        mask_node_1 = nodes.new("ShaderNodeTexImage")
        mask_node_1.location = (450, -170)
        mask_node_1.image = mask_img
        try:
            mask_node_1.extension = "CLIP"
        except Exception:
            pass
        try:
            mask_node_1.interpolation = "Linear"
        except Exception:
            pass

        # masks stay raw UV
        try:
            links.new(texcoord.outputs["UV"], mask_node_1.inputs["Vector"])
        except Exception:
            links.new(texcoord.outputs["Generated"], mask_node_1.inputs["Vector"])

        a = _mask_alpha_socket(mask_node_1)
        if a is None:
            sep = nodes.new("ShaderNodeSeparateRGB")
            sep.location = (650, -170)
            links.new(mask_node_1.outputs["Color"], sep.inputs["Image"])
            a = sep.outputs["R"]

        alpha1 = _invert_socket(a, xloc=650, yloc=-150) if mask_invert else a

    alpha2 = None
    if mask_img2 is not None:
        mask_node_2 = nodes.new("ShaderNodeTexImage")
        mask_node_2.location = (450, -340)
        mask_node_2.image = mask_img2
        try:
            mask_node_2.extension = "CLIP"
        except Exception:
            pass
        try:
            mask_node_2.interpolation = "Linear"
        except Exception:
            pass

        # masks stay raw UV
        try:
            links.new(texcoord.outputs["UV"], mask_node_2.inputs["Vector"])
        except Exception:
            links.new(texcoord.outputs["Generated"], mask_node_2.inputs["Vector"])

        a2 = _mask_alpha_socket(mask_node_2)
        if a2 is None:
            sep2 = nodes.new("ShaderNodeSeparateRGB")
            sep2.location = (650, -340)
            links.new(mask_node_2.outputs["Color"], sep2.inputs["Image"])
            a2 = sep2.outputs["R"]

        a2 = _invert_socket(a2, xloc=650, yloc=-320) if mask2_invert else a2
        a2 = _gain_socket(a2, pallu_gain, xloc=650, yloc=-320)
        alpha2 = a2

    fac_out = None
    if alpha1 is not None and alpha2 is not None:
        mx = nodes.new("ShaderNodeMath")
        mx.location = (820, -230)
        mx.operation = "MAXIMUM"
        mx.use_clamp = True
        links.new(alpha1, mx.inputs[0])
        links.new(alpha2, mx.inputs[1])
        fac_out = mx.outputs[0]
    elif alpha1 is not None:
        fac_out = alpha1
    elif alpha2 is not None:
        fac_out = alpha2

    mask_thr = _env_float("COMMERCE_SAREE_MASK_THRESHOLD", 0.0)
    mask_feather = _env_float("COMMERCE_SAREE_MASK_FEATHER", 0.06)
    if fac_out is not None:
        fac_out = _threshold_socket(fac_out, mask_thr, mask_feather, xloc=930, yloc=-120)

    # Mix: Fac=0 => Transparent, Fac=1 => Emission
    links.new(transparent.outputs["BSDF"], mix.inputs[1])
    links.new(emission.outputs["Emission"], mix.inputs[2])

    if fac_out is not None:
        links.new(fac_out, mix.inputs["Fac"])
    else:
        mix.inputs["Fac"].default_value = 1.0  # no masks => always visible

    links.new(mix.outputs["Shader"], out.inputs["Surface"])

    try:
        mat["df_variant_idx"] = int(variant_idx)
        mat["df_u_off"] = float(u_off)
        mat["df_v_off"] = float(v_off)
        mat["df_uv_scale"] = float(uv_scale)
        mat["df_rot_z"] = float(rot_z)
        mat["df_has_mask1"] = bool(mask_img is not None)
        mat["df_has_mask2"] = bool(mask_img2 is not None)
        mat["df_mask_invert"] = bool(mask_invert)
        mat["df_mask2_invert"] = bool(mask2_invert)
        mat["df_pallu_gain"] = float(pallu_gain)
        mat["df_mask_thr"] = float(mask_thr)
        mat["df_mask_feather"] = float(mask_feather)
    except Exception:
        pass

    return mat


def _assign_material_to_meshes(meshes: List[Any], mat: bpy.types.Material, *, override: bool) -> int:
    assigned = 0
    for obj in meshes:
        if getattr(obj, "type", None) != "MESH":
            continue
        try:
            if obj.data and hasattr(obj.data, "materials"):
                if override:
                    obj.data.materials.clear()
                if len(obj.data.materials) == 0:
                    obj.data.materials.append(mat)
                else:
                    obj.data.materials[0] = mat
                assigned += 1
        except Exception:
            continue
    return assigned


# -------------------------
# PNG probe (pure python)
# -------------------------


def _png_probe_rgba8(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not path or not os.path.exists(path):
            return None

        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None

            width = height = None
            bit_depth = color_type = None
            idat = bytearray()

            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                length, ctype = struct.unpack(">I4s", hdr)
                chunk = f.read(length)
                f.read(4)
                if ctype == b"IHDR":
                    width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk)
                elif ctype == b"IDAT":
                    idat.extend(chunk)
                elif ctype == b"IEND":
                    break

        if width is None or height is None or bit_depth != 8 or color_type != 6:
            return None

        raw = zlib.decompress(bytes(idat))
        bpp = 4
        stride = int(width) * bpp
        expected_min = int(height) * (1 + stride)
        if len(raw) < expected_min:
            return None

        def paeth(a: int, b: int, c: int) -> int:
            p = a + b - c
            pa = abs(p - a)
            pb = abs(p - b)
            pc = abs(p - c)
            if pa <= pb and pa <= pc:
                return a
            if pb <= pc:
                return b
            return c

        amin = 255
        amax = 0
        prev = bytearray(stride)
        pos = 0

        nontransparent = 0
        blackish = 0

        sample_stride = 8 * 4

        for _y in range(int(height)):
            ftype = raw[pos]
            pos += 1
            row = bytearray(raw[pos : pos + stride])
            pos += stride

            if ftype == 1:
                for i in range(bpp, stride):
                    row[i] = (row[i] + row[i - bpp]) & 0xFF
            elif ftype == 2:
                for i in range(stride):
                    row[i] = (row[i] + prev[i]) & 0xFF
            elif ftype == 3:
                for i in range(stride):
                    left = row[i - bpp] if i >= bpp else 0
                    up = prev[i]
                    row[i] = (row[i] + ((left + up) // 2)) & 0xFF
            elif ftype == 4:
                for i in range(stride):
                    left = row[i - bpp] if i >= bpp else 0
                    up = prev[i]
                    up_left = prev[i - bpp] if i >= bpp else 0
                    row[i] = (row[i] + paeth(left, up, up_left)) & 0xFF
            elif ftype != 0:
                return None

            for i in range(3, stride, 4):
                a = row[i]
                if a < amin:
                    amin = a
                if a > amax:
                    amax = a

            for i in range(0, stride, sample_stride):
                r = row[i + 0]
                g = row[i + 1]
                b = row[i + 2]
                a = row[i + 3]
                if a > 20:
                    nontransparent += 1
                    if r <= 10 and g <= 10 and b <= 10:
                        blackish += 1

            prev = row

        black_frac = float(blackish) / float(max(1, nontransparent))
        return {
            "amin": float(amin) / 255.0,
            "amax": float(amax) / 255.0,
            "nontransparent_pixels": int(nontransparent),
            "black_frac": float(black_frac),
            "mostly_black": bool(nontransparent >= 200 and black_frac >= 0.92),
        }
    except Exception:
        return None


# -------------------------
# misc helpers
# -------------------------


def _infer_variant_idx_from_out(out_path: str) -> int:
    s = out_path.replace(os.sep, "/")
    m = re.search(r"-([0-9]{1,2})[_/]", s)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return 0


def _stable_seed(template: str, texture: str, variant_idx: int, seed_str: str) -> str:
    base = seed_str.strip()
    if not base:
        base = f"{os.path.basename(template)}|{os.path.basename(texture)}|{variant_idx}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _write_meta(path: str, meta: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def _cleanup_unused_images() -> None:
    try:
        for img in list(bpy.data.images):
            if img.users == 0 and img.name not in ("Render Result", "Viewer Node"):
                bpy.data.images.remove(img)
    except Exception:
        pass


def _render_write_still(scene: Any, out_path: str) -> None:
    scene.render.filepath = out_path
    bpy.ops.render.render(write_still=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise RuntimeError(f"RENDER_OUTPUT_MISSING_OR_EMPTY: {out_path}")


def _autodiscover_pallu_mask(alpha_mask_path: str) -> str:
    if not alpha_mask_path:
        return ""
    d = os.path.dirname(alpha_mask_path)
    cand = os.path.join(d, "pallu_alpha.png")
    if os.path.exists(cand):
        return cand
    cand2 = os.path.join(d, "pallav_alpha.png")
    if os.path.exists(cand2):
        return cand2
    return ""


# -------------------------
# main
# -------------------------


def main() -> int:
    args = _parse_args()
    if not args:
        print("Missing args after --", file=sys.stderr)
        return 2

    if not os.path.exists(args.template):
        print(f"Template not found: {args.template}", file=sys.stderr)
        return 3
    if not os.path.exists(args.saree_texture):
        print(f"Saree texture not found: {args.saree_texture}", file=sys.stderr)
        return 3

    if args.alpha_mask and (not os.path.exists(args.alpha_mask)):
        print(f"Alpha mask not found: {args.alpha_mask}", file=sys.stderr)
        return 3

    if args.pallu_mask and (not os.path.exists(args.pallu_mask)):
        print(f"Pallu mask not found: {args.pallu_mask}", file=sys.stderr)
        return 3

    t0 = time.time()

    out_path = args.out
    if os.path.dirname(out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

    w = int(args.width or 0)
    h = int(args.height or 0)
    if w <= 0 or h <= 0:
        r = int(args.res or 1024)
        w = r
        h = r

    v_idx = int(args.variant_idx)
    if v_idx < 0:
        v_idx = _infer_variant_idx_from_out(out_path)
    seed_hex = _stable_seed(args.template, args.saree_texture, v_idx, args.variant_seed)

    # pallu mask: explicit arg > auto-discover next to alpha_mask
    pallu_mask_path = (args.pallu_mask or "").strip()
    if not pallu_mask_path and (args.alpha_mask or "").strip():
        pallu_mask_path = _autodiscover_pallu_mask(args.alpha_mask)

    require_pallu = _env_bool("COMMERCE_SAREE_REQUIRE_PALLU", default=False)
    pallu_gain = _env_float("COMMERCE_SAREE_PALLU_GAIN", 1.15)

    meta: Dict[str, Any] = {
        "ok": False,
        "out": out_path,
        "template": args.template,
        "saree_texture": args.saree_texture,
        "alpha_mask": args.alpha_mask or "",
        "alpha_mask_invert": bool(args.alpha_mask_invert),
        "pallu_mask": pallu_mask_path or "",
        "pallu_mask_invert": bool(args.pallu_mask_invert),
        "require_pallu": bool(require_pallu),
        "pallu_gain": float(pallu_gain),
        "mask_threshold": float(_env_float("COMMERCE_SAREE_MASK_THRESHOLD", 0.0)),
        "mask_feather": float(_env_float("COMMERCE_SAREE_MASK_FEATHER", 0.06)),
        "width": int(w),
        "height": int(h),
        "requested_engine": str(args.engine),
        "allowed_engines": None,
        "chosen_engine": None,
        "only_saree_objects": bool(int(args.only_saree_objects or 0) == 1),
        "material_override": bool(int(args.material_override or 0) == 1),
        "assigned_mesh_count": 0,
        "target_find": None,
        "target_names": [],
        "pallu_mesh_names": [],
        "baked_target_names": [],
        "baked_pallu_names": [],
        "forced_modifiers": [],
        "baked_axis_probe": None,
        "camera_autofit": None,
        "camera_ortho_scale_attempts": [],
        "png_probe": None,
        "render_attempts": [],
        "variant": {"variant_idx": v_idx, "seed": seed_hex},
        "warnings": [],
        "elapsed_s": None,
    }

    try:
        bpy.ops.wm.open_mainfile(filepath=args.template)
        scene = bpy.context.scene

        _disable_compositor_and_sequencer(scene)
        _unexclude_all_layer_collections(bpy.context.view_layer)
        _unhide_all_objects(scene)

        meta["allowed_engines"] = _allowed_render_engines(scene)
        chosen_engine = _choose_engine(scene, args.engine)
        scene.render.engine = chosen_engine
        meta["chosen_engine"] = chosen_engine

        try:
            if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
                scene.eevee.taa_render_samples = int(args.eevee_samples)
        except Exception:
            pass

        _configure_render_output(scene, out_path=out_path, width=w, height=h)
        _ensure_camera(scene)
        _ensure_camera_ortho(scene)
        _ensure_light(scene)

        # 1) pick target meshes (includes pallu union)
        targets, find_info = _find_saree_meshes(scene)
        meta["target_find"] = find_info
        meta["target_names"] = [getattr(o, "name", "") for o in targets]
        meta["pallu_mesh_names"] = [n for n in meta["target_names"] if _is_pallu_named(n)]
        if not targets:
            raise RuntimeError(f"NO_SAREE_MESHES_FOUND: {find_info}")

        # pallu requirement: require we have a pallu mask (explicit or auto-discovered)
        if require_pallu and not pallu_mask_path:
            raise RuntimeError(
                "SAREE_PALLU_REQUIRED_BUT_NO_MASK: set --pallu_mask or ensure pallu_alpha.png exists next to --alpha_mask "
                f"(alpha_mask={args.alpha_mask!r})"
            )

        # 2) force modifiers ON
        for t in targets:
            meta["forced_modifiers"].append(_force_modifiers_on(t))
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        # 3) bake evaluated meshes and render baked
        baked_targets: List[Any] = []
        baked_pallu: List[Any] = []
        for t in targets:
            baked = _bake_evaluated_mesh(scene, t, baked_name=f"DF_SAREE_BAKED_{t.name}")
            baked_targets.append(baked)
            if _is_pallu_named(getattr(t, "name", "")):
                baked_pallu.append(baked)
            try:
                t.hide_render = True
            except Exception:
                pass

        targets = baked_targets
        meta["baked_target_names"] = [getattr(o, "name", "") for o in targets]
        meta["baked_pallu_names"] = [getattr(o, "name", "") for o in baked_pallu]

        # 4) overlay mode allows flat plane
        probe_obj = None
        if targets:
            probe_obj = sorted(targets, key=_poly_count, reverse=True)[0]
        probe_axis = _mesh_axis_probe(probe_obj) if probe_obj else {"ok": False, "reason": "no_baked_targets"}
        meta["baked_axis_probe"] = probe_axis
        if probe_axis.get("ok") and bool(probe_axis.get("flat_like")):
            if _env_bool("DF_SAREE_RENDER_REQUIRE_NONFLAT", False):
                raise RuntimeError(f"SAREE_TEMPLATE_NOT_DRAPED (flat evaluated mesh): baked_axis_probe={probe_axis}")
            meta["warnings"].append({"code": "SAREE_TEMPLATE_FLAT_ALLOWED", "baked_axis_probe": probe_axis})

        # 5) hide everything else from render
        if meta["only_saree_objects"]:
            _hide_everything_except(scene, targets)
            pad = _env_float("DF_SAREE_CAMERA_PAD", 1.25)
            meta["camera_autofit"] = _auto_fit_ortho_camera(scene, targets, pad=pad)

        # 6) material (supports combined masks)
        saree_img = _load_image(args.saree_texture, non_color=False)
        mask_img = _load_image(args.alpha_mask, non_color=True) if args.alpha_mask else None
        mask2_img = _load_image(pallu_mask_path, non_color=True) if pallu_mask_path else None

        def _render_and_probe(tag: str) -> Optional[Dict[str, Any]]:
            _render_write_still(scene, out_path)
            probe = _png_probe_rgba8(out_path)
            meta["png_probe"] = probe
            meta["render_attempts"].append({"tag": tag, "png_probe": probe})
            return probe

        # First try WITH masks (if provided)
        mat = _make_overlay_material(
            saree_img=saree_img,
            mask_img=mask_img,
            mask_invert=bool(args.alpha_mask_invert),
            mask_img2=mask2_img,
            mask2_invert=bool(args.pallu_mask_invert),
            pallu_gain=float(pallu_gain),
            variant_idx=v_idx,
            seed_hex=seed_hex,
        )

        assigned = _assign_material_to_meshes(targets, mat, override=bool(int(args.material_override or 0) == 1))
        meta["assigned_mesh_count"] = int(assigned)
        if assigned <= 0:
            raise RuntimeError(f"NO_MESHES_ASSIGNED: targets={meta['baked_target_names']} info={find_info}")

        probe = _render_and_probe("with_masks" if (mask_img or mask2_img) else "no_masks_initial")

        # QC: require some non-zero alpha + avoid mostly-black overlays
        if meta["only_saree_objects"]:
            if probe is None:
                print("[df] WARN: png_probe failed; cannot QC", file=sys.stderr)
            else:
                amax = float(probe.get("amax", 0.0))
                if amax <= 0.01:
                    # 🔥 CRITICAL FIX: if masks blank the overlay, retry WITHOUT masks
                    if mask_img is not None or mask2_img is not None:
                        meta["warnings"].append(
                            {
                                "code": "MASKS_BLANKED_OVERLAY_RETRYING_WITHOUT_MASKS",
                                "probe": probe,
                                "alpha_mask": bool(mask_img is not None),
                                "pallu_mask": bool(mask2_img is not None),
                            }
                        )
                        mat2 = _make_overlay_material(
                            saree_img=saree_img,
                            mask_img=None,
                            mask_invert=False,
                            mask_img2=None,
                            mask2_invert=False,
                            pallu_gain=float(pallu_gain),
                            variant_idx=v_idx,
                            seed_hex=seed_hex,
                        )
                        _assign_material_to_meshes(targets, mat2, override=True)
                        probe = _render_and_probe("retry_without_masks")
                        if probe is None or float(probe.get("amax", 0.0)) <= 0.01:
                            raise RuntimeError(f"BLANK_TRANSPARENT_OVERLAY: png_probe={probe}")
                    else:
                        raise RuntimeError(f"BLANK_TRANSPARENT_OVERLAY: png_probe={probe}")

                if probe is not None and bool(probe.get("mostly_black")):
                    raise RuntimeError(f"OVERLAY_MOSTLY_BLACK: png_probe={probe} targets={meta['baked_target_names']}")

                # If background not transparent enough, attempt camera bump to include transparent border
                if probe is not None and float(probe.get("amin", 1.0)) > 0.05:
                    for factor in (1.15, 1.35, 1.60, 1.90, 2.30):
                        new_scale = _bump_ortho_scale(scene, factor)
                        meta["camera_ortho_scale_attempts"].append({"factor": factor, "ortho_scale": new_scale})
                        probe2 = _render_and_probe(f"camera_bump_{factor}")
                        if probe2 is None:
                            continue
                        if float(probe2.get("amax", 0.0)) <= 0.01:
                            raise RuntimeError("BLANK_TRANSPARENT_OVERLAY_AFTER_CAMERA_BUMP")
                        if bool(probe2.get("mostly_black")):
                            raise RuntimeError(f"OVERLAY_MOSTLY_BLACK_AFTER_CAMERA_BUMP: png_probe={probe2}")
                        if float(probe2.get("amin", 1.0)) <= 0.05:
                            break

                    if meta["png_probe"] is not None and float(meta["png_probe"].get("amin", 1.0)) > 0.05:
                        raise RuntimeError(f"OVERLAY_NOT_TRANSPARENT_BG: png_probe={meta['png_probe']}")

        meta["ok"] = True

    except Exception as e:
        meta["ok"] = False
        meta["error"] = f"{type(e).__name__}: {e}"
        print(f"[df] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

        _write_meta(args.meta_json, meta)
        if bool(args.fail_if_output_missing):
            meta["elapsed_s"] = round(time.time() - t0, 3)
            _write_meta(args.meta_json, meta)
            return 5

    finally:
        meta["elapsed_s"] = round(time.time() - t0, 3)
        _write_meta(args.meta_json, meta)
        _cleanup_unused_images()

    print(
        f"Rendered: {out_path} | {w}x{h} | engine={meta.get('chosen_engine')} "
        f"variant={v_idx} seed={seed_hex} assigned_meshes={meta.get('assigned_mesh_count')} "
        f"pallu_mask={meta.get('pallu_mask')} pallu_meshes={len(meta.get('baked_pallu_names') or [])} "
        f"baked_probe={meta.get('baked_axis_probe')} png_probe={meta.get('png_probe')} ok={meta.get('ok')}"
    )
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())