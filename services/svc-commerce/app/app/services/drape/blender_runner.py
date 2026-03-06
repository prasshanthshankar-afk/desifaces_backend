# services/svc-commerce/app/app/services/drape/blender_runner.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = (os.getenv(name) or "").strip()
    if not v:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _normalize_engine(engine: str) -> str:
    e = (engine or "").strip().upper()
    if not e or e == "AUTO":
        return "AUTO"
    alias = {
        "EEVEE": "BLENDER_EEVEE_NEXT",
        "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT",
        "EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
        "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
        "WORKBENCH": "BLENDER_WORKBENCH",
        "BLENDER_WORKBENCH": "BLENDER_WORKBENCH",
        "CYCLES": "CYCLES",
    }
    return alias.get(e, e)


def _read_tail(path: str, max_bytes: int = 8000) -> str:
    try:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _read_json(path: str) -> Dict[str, Any]:
    try:
        if not path or not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) if f else {}
    except Exception:
        return {}


@dataclass
class BlenderRunnerConfig:
    blender_bin: str = "blender"
    script_path: str = ""  # if empty, default to packaged script path
    timeout_s: int = 240

    overlay_res: int = 1024
    render_engine: str = "BLENDER_EEVEE_NEXT"
    replace_all_textures: bool = False

    allow_cycles: bool = False

    require_transparent_overlay: bool = False
    min_output_bytes: int = 1024

    inject_mesa_env: bool = True
    mesa_env_libgl_always_software: str = "1"
    mesa_env_gl_version_override: str = "4.5"
    mesa_env_glsl_version_override: str = "450"

    @staticmethod
    def from_env() -> "BlenderRunnerConfig":
        engine = (
            _env("COMMERCE_SAREE_BLENDER_ENGINE", "")
            or _env("DF_SAREE_BLENDER_ENGINE", "")
            or _env("DF_SAREE_RENDER_ENGINE", "")
            or "BLENDER_EEVEE_NEXT"
        )
        return BlenderRunnerConfig(
            blender_bin=_env("DF_BLENDER_BIN", "blender"),
            script_path=_env("DF_SAREE_BLENDER_SCRIPT", ""),
            timeout_s=_env_int("DF_BLENDER_TIMEOUT_S", 240),
            overlay_res=_env_int("DF_SAREE_OVERLAY_RES", 1024),
            render_engine=_normalize_engine(engine),
            replace_all_textures=_env_bool("DF_SAREE_REPLACE_ALL_TEXTURES", False),
            allow_cycles=_env_bool("DF_SAREE_ALLOW_CYCLES", False),
            require_transparent_overlay=_env_bool("DF_SAREE_REQUIRE_TRANSPARENT_OVERLAY", False),
            inject_mesa_env=_env_bool("DF_SAREE_INJECT_MESA_ENV", True),
            mesa_env_libgl_always_software=_env("LIBGL_ALWAYS_SOFTWARE", "1") or "1",
            mesa_env_gl_version_override=_env("MESA_GL_VERSION_OVERRIDE", "4.5") or "4.5",
            mesa_env_glsl_version_override=_env("MESA_GLSL_VERSION_OVERRIDE", "450") or "450",
            min_output_bytes=_env_int("DF_SAREE_MIN_OUTPUT_BYTES", 1024),
        )


class BlenderRunner:
    def __init__(self, config: Optional[BlenderRunnerConfig] = None) -> None:
        self.cfg = config or BlenderRunnerConfig.from_env()

    def _resolve_blender(self) -> Optional[str]:
        if os.path.isabs(self.cfg.blender_bin) and os.path.exists(self.cfg.blender_bin):
            return self.cfg.blender_bin
        return shutil.which(self.cfg.blender_bin)

    def _resolve_script_path(self) -> str:
        if self.cfg.script_path and os.path.exists(self.cfg.script_path):
            return self.cfg.script_path
        return os.path.join(os.path.dirname(__file__), "blender_scripts", "saree_render.py")

    def render_saree_overlay(
        self,
        *,
        template_blend: str,
        saree_texture_path: str,
        out_png_path: str,
        overlay_res: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        render_engine: Optional[str] = None,
        replace_all_textures: Optional[bool] = None,
        meta_json_path: Optional[str] = None,
        stdout_path: Optional[str] = None,
        stderr_path: Optional[str] = None,
        fail_if_output_missing: bool = False,
        material_override: bool = True,
        only_saree_objects: bool = True,
        variant_idx: Optional[int] = None,
        variant_seed: Optional[str] = None,
        alpha_mask_path: Optional[str] = None,
        alpha_mask_invert: bool = False,
        pallu_mask_path: Optional[str] = None,
        pallu_mask_invert: bool = False,
        eevee_samples: Optional[int] = None,
    ) -> bool:
        blender = self._resolve_blender()
        if not blender:
            raise RuntimeError("Blender binary not found in PATH (install blender or set DF_BLENDER_BIN)")

        script_path = self._resolve_script_path()
        if not os.path.exists(script_path):
            raise RuntimeError(f"missing blender script: {script_path}")

        if not out_png_path.lower().endswith(".png"):
            raise RuntimeError(f"out_png_path must be a .png: {out_png_path}")

        if not os.path.exists(template_blend):
            raise RuntimeError(f"missing template blend: {template_blend}")
        if not os.path.exists(saree_texture_path):
            raise RuntimeError(f"missing saree texture path: {saree_texture_path}")
        if alpha_mask_path and (not os.path.exists(alpha_mask_path)):
            raise RuntimeError(f"alpha_mask_path not found: {alpha_mask_path}")
        if pallu_mask_path and (not os.path.exists(pallu_mask_path)):
            raise RuntimeError(f"pallu_mask_path not found: {pallu_mask_path}")

        os.makedirs(os.path.dirname(out_png_path) or ".", exist_ok=True)

        if not meta_json_path:
            meta_json_path = out_png_path + ".meta.json"
        if not stdout_path:
            stdout_path = out_png_path + ".stdout.txt"
        if not stderr_path:
            stderr_path = out_png_path + ".stderr.txt"

        os.makedirs(os.path.dirname(meta_json_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(stdout_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(stderr_path) or ".", exist_ok=True)

        res = int(overlay_res or self.cfg.overlay_res or 1024)
        res = max(256, min(4096, res))

        requested_engine = _normalize_engine(render_engine or self.cfg.render_engine or "AUTO")
        if requested_engine in ("AUTO", "BLENDER_EEVEE"):
            engine = "BLENDER_EEVEE_NEXT"
        elif requested_engine == "CYCLES":
            engine = "CYCLES" if bool(self.cfg.allow_cycles) else "BLENDER_EEVEE_NEXT"
        else:
            engine = requested_engine

        repl_all = bool(self.cfg.replace_all_textures if replace_all_textures is None else replace_all_textures)

        if variant_idx is not None and not variant_seed:
            s = f"{os.path.basename(template_blend)}|{os.path.basename(saree_texture_path)}|{int(variant_idx)}"
            variant_seed = hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

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
            "--res",
            str(res),
            "--engine",
            engine,
            "--meta_json",
            meta_json_path,
            "--material_override",
            "1" if material_override else "0",
            "--only_saree_objects",
            "1" if only_saree_objects else "0",
        ]

        # NEW: allow non-square output matching human image
        if width is not None and height is not None:
            cmd.extend(["--width", str(int(width)), "--height", str(int(height))])

        if eevee_samples is not None:
            cmd.extend(["--eevee_samples", str(int(eevee_samples))])

        if repl_all:
            cmd.append("--replace_all")
        if fail_if_output_missing:
            cmd.append("--fail_if_output_missing")
        if variant_idx is not None:
            cmd.extend(["--variant_idx", str(int(variant_idx))])
        if variant_seed:
            cmd.extend(["--variant_seed", str(variant_seed)])
        if alpha_mask_path:
            cmd.extend(["--alpha_mask", str(alpha_mask_path)])
            if alpha_mask_invert:
                cmd.append("--alpha_mask_invert")

        # NEW: pallu mask passthrough
        if pallu_mask_path:
            cmd.extend(["--pallu_mask", str(pallu_mask_path)])
            if pallu_mask_invert:
                cmd.append("--pallu_mask_invert")

        logger.info(
            "BlenderRunner requested_engine=%s resolved_engine=%s cmd=%s",
            requested_engine,
            engine,
            " ".join(cmd),
        )

        env = dict(os.environ)
        if self.cfg.inject_mesa_env:
            env.setdefault("LIBGL_ALWAYS_SOFTWARE", self.cfg.mesa_env_libgl_always_software or "1")
            env.setdefault("MESA_GL_VERSION_OVERRIDE", self.cfg.mesa_env_gl_version_override or "4.5")
            env.setdefault("MESA_GLSL_VERSION_OVERRIDE", self.cfg.mesa_env_glsl_version_override or "450")
            env.setdefault("EGL_PLATFORM", os.getenv("EGL_PLATFORM", "surfaceless"))
            env.setdefault("MESA_LOADER_DRIVER_OVERRIDE", os.getenv("MESA_LOADER_DRIVER_OVERRIDE", "llvmpipe"))
            env.setdefault("GALLIUM_DRIVER", os.getenv("GALLIUM_DRIVER", "llvmpipe"))

        try:
            with open(stdout_path, "w", encoding="utf-8") as out_f, open(stderr_path, "w", encoding="utf-8") as err_f:
                p = subprocess.run(
                    cmd,
                    stdout=out_f,
                    stderr=err_f,
                    timeout=int(self.cfg.timeout_s),
                    check=False,
                    text=True,
                    env=env,
                )
            rc = int(p.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("blender timeout after %ss out=%s", int(self.cfg.timeout_s), out_png_path)
            return False
        except Exception as e:
            logger.exception("blender run failed: %s", e)
            return False

        tail = (_read_tail(stderr_path) + "\n" + _read_tail(stdout_path)).strip()
        meta = _read_json(meta_json_path)
        meta_ok = bool(isinstance(meta, dict) and meta.get("ok") is True)

        ok_output = (
            os.path.exists(out_png_path)
            and os.path.getsize(out_png_path) > int(self.cfg.min_output_bytes or 1024)
        )

        if rc != 0 and not (ok_output and meta_ok):
            logger.warning(
                "blender failed rc=%s out=%s meta_ok=%s meta_error=%s tail=%s",
                rc,
                out_png_path,
                meta_ok,
                meta.get("error") if isinstance(meta, dict) else None,
                tail[-6000:] if tail else "(no tail)",
            )
            return False

        if rc != 0 and (ok_output and meta_ok):
            logger.warning(
                "blender rc=%s but output+meta ok; treating as success. out=%s tail=%s",
                rc,
                out_png_path,
                tail[-2000:] if tail else "(no tail)",
            )

        if not ok_output:
            logger.warning(
                "blender produced no/empty output: %s meta_ok=%s meta_error=%s tail=%s",
                out_png_path,
                meta_ok,
                meta.get("error") if isinstance(meta, dict) else None,
                tail[-6000:] if tail else "(no tail)",
            )
            return False

        if isinstance(meta, dict) and meta.get("error"):
            logger.warning("blender meta_error=%s out=%s", meta.get("error"), out_png_path)

        return True

    # legacy wrappers unchanged
    def render_saree_overlay_legacy(
        self,
        *,
        blend_path: str,
        garment_image_path: str,
        output_dir: str,
        meta_json_path: str,
        variant_idx: Optional[int] = None,
        variant_seed: Optional[str] = None,
        alpha_mask_path: Optional[str] = None,
        alpha_mask_invert: bool = False,
        **_ignored: Any,
    ) -> bool:
        out_png_path = os.path.join(output_dir, "overlay.png")
        stdout_path = os.path.join(output_dir, "overlay.stdout.txt")
        stderr_path = os.path.join(output_dir, "overlay.stderr.txt")
        return self.render_saree_overlay(
            template_blend=blend_path,
            saree_texture_path=garment_image_path,
            out_png_path=out_png_path,
            alpha_mask_path=alpha_mask_path,
            alpha_mask_invert=alpha_mask_invert,
            meta_json_path=meta_json_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            fail_if_output_missing=True,
            material_override=True,
            only_saree_objects=True,
            variant_idx=variant_idx,
            variant_seed=variant_seed,
        )

    render_overlay = render_saree_overlay