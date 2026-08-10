from __future__ import annotations

import os
from pathlib import Path

from rembg import new_session


def main() -> None:
    model_name = os.getenv("REMBG_MODEL", "u2net").strip() or "u2net"
    model_home = os.getenv("U2NET_HOME", str(Path.home() / ".u2net"))

    Path(model_home).mkdir(parents=True, exist_ok=True)

    print(f"[prewarm_rembg] U2NET_HOME={model_home}")
    print(f"[prewarm_rembg] model={model_name}")

    # This forces rembg to download/load the model into U2NET_HOME.
    _ = new_session(model_name)

    expected = Path(model_home) / f"{model_name}.onnx"
    if expected.exists():
        print(f"[prewarm_rembg] ready: {expected}")
    else:
        print(f"[prewarm_rembg] model loaded, but expected file not found exactly at: {expected}")


if __name__ == "__main__":
    main()