from __future__ import annotations

import argparse
import sys
from typing import Optional

import bpy  # type: ignore


def _parse_args() -> Optional[argparse.Namespace]:
    argv = sys.argv
    if "--" not in argv:
        return None
    argv = argv[argv.index("--") + 1 :]
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="Output .blend path")
    return p.parse_args(argv)


def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    try:
        for _ in range(2):
            bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except Exception:
        pass


def _ensure_camera_and_light() -> None:
    bpy.ops.object.camera_add(location=(0.0, -4.0, 1.5), rotation=(1.15, 0.0, 0.0))
    cam = bpy.context.active_object
    cam.name = "DF_CAMERA"
    bpy.context.scene.camera = cam
    try:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = 3.8
    except Exception:
        pass

    bpy.ops.object.light_add(type="SUN", location=(2.0, -2.0, 4.0))
    sun = bpy.context.active_object
    sun.name = "DF_KEY"
    try:
        sun.data.energy = 3.0
    except Exception:
        pass

    sc = bpy.context.scene
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    try:
        sc.render.film_transparent = True
    except Exception:
        pass


def _tag_saree(obj: bpy.types.Object) -> None:
    obj["df_garment_kind"] = "saree"
    try:
        obj.hide_render = False
    except Exception:
        pass


def _apply_displace(obj: bpy.types.Object, *, strength: float, scale: float) -> None:
    tex = bpy.data.textures.new(f"{obj.name}_FOLD_TEX", type="CLOUDS")
    tex.noise_scale = float(scale)

    disp = obj.modifiers.new("DF_FOLDS", type="DISPLACE")
    disp.texture = tex
    disp.strength = float(strength)
    disp.mid_level = 0.0

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.modifier_apply(modifier="DF_FOLDS")
    except Exception:
        pass


def _apply_simple_deform(obj: bpy.types.Object, *, method: str, axis: str, angle: float) -> None:
    m = obj.modifiers.new(f"DF_{method}_{axis}", type="SIMPLE_DEFORM")
    m.deform_method = method
    m.deform_axis = axis
    m.angle = float(angle)

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.modifier_apply(modifier=m.name)
    except Exception:
        pass


def _uv_smart_project(obj: bpy.types.Object) -> None:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
    except Exception:
        pass
    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


def _make_body_proxy() -> bpy.types.Object:
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.35, depth=1.65, location=(0.0, 0.0, 0.9))
    body = bpy.context.active_object
    body.name = "DF_BODY_PROXY"
    try:
        body.hide_render = True
        body.hide_set(True)
    except Exception:
        pass
    return body


def _make_saree_skirt() -> bpy.types.Object:
    # A wrap-like cylinder (open top/bottom) → saree skirt silhouette
    bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=0.55, depth=1.10, location=(0.0, 0.0, 0.55))
    skirt = bpy.context.active_object
    skirt.name = "DF_SAREE_SKIRT"
    _tag_saree(skirt)

    # Add folds + subtle twist for pleat feel
    _apply_displace(skirt, strength=0.06, scale=0.85)
    _apply_simple_deform(skirt, method="TWIST", axis="Z", angle=0.18)

    # Slight bend so it doesn’t read like a perfect tube
    _apply_simple_deform(skirt, method="BEND", axis="X", angle=0.22)

    _uv_smart_project(skirt)
    return skirt


def _make_saree_pallu() -> bpy.types.Object:
    # A pallu sheet, angled like shoulder-to-hip drape
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=(0.25, 0.05, 1.25))
    pallu = bpy.context.active_object
    pallu.name = "DF_SAREE_PALLU"
    _tag_saree(pallu)

    # Make it long + narrow
    pallu.scale = (0.55, 1.40, 1.0)

    # Subdivide for folds
    bpy.context.view_layer.objects.active = pallu
    pallu.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=60)
    bpy.ops.object.mode_set(mode="OBJECT")

    # Angle it like pallu across torso
    pallu.rotation_euler = (0.35, 0.15, -0.55)

    _apply_displace(pallu, strength=0.05, scale=0.9)
    _apply_simple_deform(pallu, method="BEND", axis="Y", angle=0.35)
    _uv_smart_project(pallu)
    return pallu


def main() -> int:
    args = _parse_args()
    if not args:
        print("Missing args after --", file=sys.stderr)
        return 2

    _clear_scene()
    _ensure_camera_and_light()

    _make_body_proxy()
    _make_saree_skirt()
    _make_saree_pallu()

    bpy.ops.wm.save_as_mainfile(filepath=args.out)
    print(f"[df] Wrote template: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())