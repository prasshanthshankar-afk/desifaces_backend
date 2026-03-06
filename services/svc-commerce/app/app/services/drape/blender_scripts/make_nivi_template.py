# services/svc-commerce/app/app/services/drape/blender_scripts/make_nivi_template.py
from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Optional

import bpy


def parse_args() -> Optional[argparse.Namespace]:
    argv = sys.argv
    if "--" not in argv:
        return None
    argv = argv[argv.index("--") + 1 :]

    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--preview", required=True)
    p.add_argument("--res", type=int, default=1024)
    p.add_argument("--preview_res", type=int, default=512)
    p.add_argument(
        "--engine",
        default="BLENDER_EEVEE_NEXT",
        help="Preferred engine for template defaults (Blender 4.x: BLENDER_EEVEE_NEXT).",
    )
    p.add_argument(
        "--preview_engine",
        default="AUTO",
        help="Engine used for preview render (AUTO keeps chosen template engine).",
    )
    p.add_argument("--qc_black_thresh", type=int, default=10, help="Fail build if preview RGB max <= this value.")
    return p.parse_args(argv)


def _allowed_engines(scene) -> set[str]:
    try:
        items = scene.render.bl_rna.properties["engine"].enum_items
        return {it.identifier for it in items}
    except Exception:
        return set()


def _choose_engine(scene, requested: str) -> str:
    req = (requested or "").strip().upper()
    if not req or req == "AUTO":
        req = str(scene.render.engine)

    alias = {
        "EEVEE": "BLENDER_EEVEE_NEXT",
        "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT",
        "EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
        "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
        "WORKBENCH": "BLENDER_WORKBENCH",
        "BLENDER_WORKBENCH": "BLENDER_WORKBENCH",
        "CYCLES": "CYCLES",
    }
    desired = alias.get(req, req)

    allowed = _allowed_engines(scene)
    if allowed and desired not in allowed:
        # Prefer Eevee Next; silently map CYCLES->EEVEE_NEXT when unavailable
        if desired == "CYCLES" and "BLENDER_EEVEE_NEXT" in allowed:
            return "BLENDER_EEVEE_NEXT"
        for cand in ("BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"):
            if cand in allowed:
                print(f"[df] Engine '{requested}' not supported; using '{cand}'. Allowed={sorted(allowed)}")
                return cand
        return sorted(allowed)[0] if allowed else desired

    return desired


def _ensure_scene_defaults(scene, *, res: int, engine: str) -> None:
    scene.render.resolution_x = int(res)
    scene.render.resolution_y = int(res)
    scene.render.resolution_percentage = 100

    scene.render.engine = _choose_engine(scene, engine)

    # Template default should support alpha overlays (background transparent)
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"

    try:
        scene.render.film_transparent = True
    except Exception:
        pass

    # Avoid Filmic surprises
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception:
        pass


def _ensure_collection(scene, name: str) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
    # Ensure it's linked into the scene
    try:
        if col.name not in [c.name for c in scene.collection.children]:
            scene.collection.children.link(col)
    except Exception:
        pass
    return col


def _link_object_to_collection(obj: bpy.types.Object, col: bpy.types.Collection) -> None:
    # Ensure object is linked to the collection
    try:
        if col not in list(getattr(obj, "users_collection", []) or []):
            col.objects.link(obj)
    except Exception:
        pass
    # Ensure it is not hidden
    try:
        obj.hide_render = False
    except Exception:
        pass
    try:
        obj.hide_set(False)
    except Exception:
        pass
    try:
        if hasattr(obj, "hide_viewport"):
            obj.hide_viewport = False
    except Exception:
        pass


def make_material_with_image_node(name: str = "DF_SareeMat"):
    """
    OPAQUE emission overlay material (lighting independent, alpha-safe):
      - Texture color -> Emission -> Output
    This avoids blank-transparent overlays when the texture alpha is 0 or missing.
    """
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    for n in list(nodes):
        nodes.remove(n)

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (520, 0)

    emission = nodes.new("ShaderNodeEmission")
    emission.location = (220, 0)
    emission.inputs["Strength"].default_value = 1.0

    tex = nodes.new("ShaderNodeTexImage")
    tex.name = "SareeTexture"
    tex.label = "SareeTexture"
    tex.location = (-140, 0)

    try:
        tex.interpolation = "Smart"
    except Exception:
        pass
    try:
        tex.extension = "REPEAT"
    except Exception:
        pass

    links.new(tex.outputs.get("Color"), emission.inputs.get("Color"))
    links.new(emission.outputs.get("Emission"), out.inputs.get("Surface"))

    # Eevee hints
    try:
        mat.blend_method = "OPAQUE"
    except Exception:
        pass
    try:
        mat.shadow_method = "NONE"
    except Exception:
        pass
    # Ensure plane is visible from both sides
    try:
        mat.use_backface_culling = False
    except Exception:
        pass

    return mat


def add_ortho_camera(*, scene, target_obj, res: int) -> bpy.types.Object:
    bpy.ops.object.camera_add(location=(0.0, -3.0, 0.9))
    cam = bpy.context.active_object
    cam.name = "DF_NiviCamera"
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = 3.8
    cam.data.clip_start = 0.01
    cam.data.clip_end = 100.0
    scene.camera = cam

    c = cam.constraints.new(type="TRACK_TO")
    c.target = target_obj
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"

    _ensure_scene_defaults(scene, res=res, engine="AUTO")
    return cam


def add_lights(scene) -> None:
    bpy.ops.object.light_add(type="AREA", location=(0.0, -2.2, 2.2))
    key = bpy.context.active_object
    key.name = "DF_KeyLight"
    key.data.energy = 1200
    key.data.size = 2.0

    bpy.ops.object.light_add(type="AREA", location=(1.2, -1.8, 1.4))
    fill = bpy.context.active_object
    fill.name = "DF_FillLight"
    fill.data.energy = 600
    fill.data.size = 1.2

    bpy.ops.object.light_add(type="POINT", location=(-1.5, -1.0, 1.6))
    rim = bpy.context.active_object
    rim.name = "DF_RimLight"
    rim.data.energy = 250


def add_saree_meshes(scene, col: bpy.types.Collection, mat):
    # Skirt plane (faces +Y after rotation)
    bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0.0, 0.0, 0.05))
    skirt = bpy.context.active_object
    skirt.name = "DF_SAREE_SKIRT"
    skirt["df_garment_kind"] = "saree"  # <- deterministic selection
    skirt.scale[0] = 0.55
    skirt.scale[1] = 0.95
    skirt.rotation_euler[0] = math.radians(90.0)
    skirt.data.materials.append(mat)
    _link_object_to_collection(skirt, col)

    mod = skirt.modifiers.new(name="WavePleats", type="WAVE")
    mod.height = 0.03
    mod.width = 0.35
    mod.speed = 0.0

    # Pallu plane
    bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0.0, 0.0, 0.65))
    pallu = bpy.context.active_object
    pallu.name = "DF_SAREE_PALLU"
    pallu["df_garment_kind"] = "saree"  # <- deterministic selection
    pallu.scale[0] = 0.95
    pallu.scale[1] = 0.22
    pallu.rotation_euler[0] = math.radians(90.0)
    pallu.rotation_euler[2] = 0.55
    pallu.location[0] = 0.10
    pallu.location[2] = 0.72
    pallu.data.materials.append(mat)
    _link_object_to_collection(pallu, col)

    mod2 = pallu.modifiers.new(name="WavePallu", type="WAVE")
    mod2.height = 0.02
    mod2.width = 0.6
    mod2.speed = 0.0

    # Ensure both are linked to scene collection too (belt-and-suspenders)
    try:
        if skirt.name not in scene.objects:
            scene.collection.objects.link(skirt)
    except Exception:
        pass
    try:
        if pallu.name not in scene.objects:
            scene.collection.objects.link(pallu)
    except Exception:
        pass

    return skirt, pallu


def make_preview_texture(size=512):
    img = bpy.data.images.new("PreviewFabric", width=size, height=size, alpha=True, float_buffer=False)
    pixels = [0.0] * (size * size * 4)

    border = max(6, size // 16)
    stripe_w = max(8, size // 24)

    for y in range(size):
        for x in range(size):
            is_border = x < border or x >= size - border or y < border or y >= size - border
            if is_border:
                r, g, b, a = 0.90, 0.72, 0.12, 1.0  # gold border
            else:
                stripe = ((x // stripe_w) % 2) == 0
                if stripe:
                    r, g, b, a = 0.55, 0.05, 0.45, 1.0
                else:
                    r, g, b, a = 0.12, 0.12, 0.12, 1.0
            idx = (y * size + x) * 4
            pixels[idx + 0] = r
            pixels[idx + 1] = g
            pixels[idx + 2] = b
            pixels[idx + 3] = a

    img.pixels = pixels
    img.pack()
    return img


def assign_texture_to_node(img):
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.name == "SareeTexture":
                node.image = img
                return True
    return False


def set_world_gray(scene, bg=0.10):
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("World")
        scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg_node = nt.nodes.get("Background")
    if bg_node:
        bg_node.inputs[0].default_value = (bg, bg, bg, 1.0)
        try:
            bg_node.inputs[1].default_value = 1.0
        except Exception:
            pass


def render_preview(path: str, *, scene, res: int, preview_engine: str):
    scene.render.resolution_x = int(res)
    scene.render.resolution_y = int(res)
    scene.render.resolution_percentage = 100

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"

    try:
        scene.render.film_transparent = False
    except Exception:
        pass

    if (preview_engine or "").strip().upper() != "AUTO":
        scene.render.engine = _choose_engine(scene, preview_engine)

    set_world_gray(scene, 0.10)

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def _qc_preview_not_black(path: str, *, thresh: int) -> None:
    try:
        img = bpy.data.images.load(path, check_existing=False)
        if not img or not hasattr(img, "pixels"):
            raise RuntimeError("preview_load_failed")
        px = list(img.pixels)
        if len(px) < 4:
            raise RuntimeError("preview_pixels_empty")

        mx = 0.0
        for i in range(0, len(px), 4):
            mx = max(mx, px[i], px[i + 1], px[i + 2])
            if mx >= (thresh / 255.0):
                break
        if mx < (thresh / 255.0):
            raise RuntimeError(f"preview_is_black rgb_max={mx:.4f} thresh={thresh}")
    except Exception as e:
        raise RuntimeError(f"DF_TEMPLATE_QC_FAILED: {e}") from e


def main():
    args = parse_args()
    if not args:
        print("Missing args after --")
        return 2

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    _ensure_scene_defaults(scene, res=args.res, engine=args.engine)

    # Dedicated collection for deterministic selection/rendering
    saree_col = _ensure_collection(scene, "DF_SAREE")

    # Target for camera
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0.0, 0.0, 0.55))
    target = bpy.context.active_object
    target.name = "DF_Target"
    _link_object_to_collection(target, saree_col)

    add_lights(scene)
    mat = make_material_with_image_node()
    add_saree_meshes(scene, saree_col, mat)
    add_ortho_camera(scene=scene, target_obj=target, res=args.res)

    preview_img = make_preview_texture(size=512)
    ok = assign_texture_to_node(preview_img)
    if not ok:
        print("Failed to assign preview texture to SareeTexture node", file=sys.stderr)
        return 4

    # Save blend
    bpy.ops.wm.save_as_mainfile(filepath=args.out)
    print(f"Saved template blend: {args.out}")

    # Render preview + QC
    render_preview(args.preview, scene=scene, res=args.preview_res, preview_engine=args.preview_engine)
    print(f"Rendered preview: {args.preview}")

    if not os.path.exists(args.preview) or os.path.getsize(args.preview) < 2048:
        print(f"Preview missing/too small: {args.preview}", file=sys.stderr)
        return 5

    try:
        _qc_preview_not_black(args.preview, thresh=int(args.qc_black_thresh))
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 6

    return 0


if __name__ == "__main__":
    raise SystemExit(main())