# MVP responsibilities:
# - Load/construct body mesh (generic or SMPL)
# - Create blouse mesh (tight garment) and apply texture
# - Create saree cloth plane, set cloth sim, anchors/pins, collision
# - Apply drape preset (nivi/gujarati/bengali)
# - Render: beauty, alpha, depth, normals, garment mask

import sys

def _parse_cli(argv):
    args = argv[1:]
    for idx, token in enumerate(args):
        if token == "--":
            args = args[idx + 1 :]
            break

    parsed = {}
    i = 0
    argc = len(args)
    while i < argc:
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue

        key = token[2:]
        if not key:
            i += 1
            continue

        if "=" in key:
            name, value = key.split("=", 1)
            parsed[name] = value
            i += 1
            continue

        if i + 1 < argc and not args[i + 1].startswith("--"):
            parsed[key] = args[i + 1]
            i += 2
            continue

        parsed[key] = "1"
        i += 1

    return parsed

def main(argv):
    options = _parse_cli(argv)

    preset = options.get("preset", "nivi")
    if preset not in {"nivi", "gujarati", "bengali"}:
        raise ValueError(f"unsupported preset: {preset}")

    result = {
        "body_mesh": options.get("body_mesh", ""),
        "blouse_mesh": options.get("blouse_mesh", ""),
        "saree_texture": options.get("saree_texture", ""),
        "output_path": options.get("output_path", ""),
        "preset": preset,
        "frame_start": int(options.get("frame_start", 1)),
        "frame_end": int(options.get("frame_end", 120)),
        "resolution_x": int(options.get("resolution_x", 1024)),
        "resolution_y": int(options.get("resolution_y", 1024)),
        "seed": int(options.get("seed", 0)),
    }
    return result

if __name__ == "__main__":
    main(sys.argv)
