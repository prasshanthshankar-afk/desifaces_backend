from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class BlenderRunnerConfig:
    blender_bin: str = "blender"
    script_path: str = ""  # default resolved below
    timeout_s: int = 180

    @staticmethod
    def from_env() -> "BlenderRunnerConfig":
        return BlenderRunnerConfig(
            blender_bin=_env("DF_BLENDER_BIN", "blender"),
            script_path=_env("DF_SAREE_BLENDER_SCRIPT", ""),
            timeout_s=int(float(_env("DF_BLENDER_TIMEOUT_S", "180"))),
        )


class BlenderRunner:
    """
    Runs Blender headless to render a saree overlay PNG (RGBA) from a template .blend
    using a Python script inside blender_scripts/.

    Required: your .blend template should have at least one Image Texture node; the script
    will try to replace it with the saree texture.
    """

    def __init__(self, config: Optional[BlenderRunnerConfig] = None) -> None:
        self.cfg = config or BlenderRunnerConfig.from_env()

    def render_saree_overlay(
        self,
        *,
        template_blend: str,
        saree_texture_path: str,
        out_png_path: str,
    ) -> bool:
        blender = self._resolve_blender()
        if not blender:
            raise RuntimeError("Blender binary not found")

        script_path = self.cfg.script_path
        if not script_path:
            # default: sibling blender_scripts/saree_render.py
            script_path = os.path.join(os.path.dirname(__file__), "blender_scripts", "saree_render.py")

        if not os.path.exists(script_path):
            raise RuntimeError(f"missing blender script: {script_path}")
        if not os.path.exists(template_blend):
            raise RuntimeError(f"missing template blend: {template_blend}")
        if not os.path.exists(saree_texture_path):
            raise RuntimeError(f"missing saree texture path: {saree_texture_path}")

        os.makedirs(os.path.dirname(out_png_path) or ".", exist_ok=True)

        cmd = [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            script_path,
            "--",
            "--template",
            template_blend,
            "--saree_texture",
            saree_texture_path,
            "--out",
            out_png_path,
        ]

        logger.info("BlenderRunner cmd=%s", " ".join(cmd))
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.cfg.timeout_s,
            check=False,
            text=True,
        )
        if p.returncode != 0:
            logger.warning("blender failed rc=%s output=%s", p.returncode, p.stdout[-4000:])
            return False

        ok = os.path.exists(out_png_path) and os.path.getsize(out_png_path) > 1024
        if not ok:
            logger.warning("blender produced no/empty output: %s", out_png_path)
        return ok

    def _resolve_blender(self) -> Optional[str]:
        if os.path.isabs(self.cfg.blender_bin) and os.path.exists(self.cfg.blender_bin):
            return self.cfg.blender_bin
        return shutil.which(self.cfg.blender_bin)