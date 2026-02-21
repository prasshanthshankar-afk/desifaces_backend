import argparse
import os
import sys

import bpy


def _parse_args():
    argv = sys.argv
    if "--" not in argv:
        return None
    idx = argv.index("--")
    argv = argv[idx + 1 :]

    p = argparse.ArgumentParser()
    p.add_argument("--template", required=True)
    p.add_argument("--saree_texture", required=True)
    p.add_argument("--out", required=True)
    return p.parse_args(argv)


def _set_first_image_texture(img_path: str) -> bool:
    img = bpy.data.images.load(img_path, check_existing=True)

    # Try named node first (recommended you set in template):
    preferred_names = {"SareeTexture", "Saree_Image", "SareeTex"}
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.name in preferred_names:
                node.image = img
                return True

    # Fallback: replace first TEX_IMAGE node found
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE":
                node.image = img
                return True

    return False


def main():
    args = _parse_args()
    if not args:
        print("Missing args after --")
        return 2

    bpy.ops.wm.open_mainfile(filepath=args.template)

    ok = _set_first_image_texture(args.saree_texture)
    if not ok:
        print("No image texture node found to replace. Update template with a TEX_IMAGE node.")
        return 3

    # Render output
    out_path = args.out
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    scene = bpy.context.scene
    scene.render.filepath = out_path
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"

    bpy.ops.render.render(write_still=True)
    print(f"Rendered: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())