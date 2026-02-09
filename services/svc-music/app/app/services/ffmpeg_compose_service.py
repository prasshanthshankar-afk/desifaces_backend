from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple


@dataclass(frozen=True)
class ComposeResult:
    outputs: Dict[str, str]          # aspect -> local mp4 path
    preview_path: Optional[str]      # local mp4 path


class FFmpegComposeService:
    """
    Self-reliant local video compositor using ffmpeg.

    Inputs are LOCAL FILE PATHS (downloaded by orchestrator).
    """

    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg = ffmpeg_bin

    def _run(self, cmd: List[str]) -> None:
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(
                "ffmpeg_failed:"
                + "\nCMD: " + " ".join(shlex.quote(c) for c in cmd)
                + "\nSTDERR:\n" + (p.stderr[-2500:] if p.stderr else "")
            )

    def _dims_for_aspect(self, aspect: str) -> Tuple[int, int]:
        if aspect == "9:16":
            return (1080, 1920)
        if aspect == "16:9":
            return (1920, 1080)
        if aspect == "1:1":
            return (1080, 1080)
        return (1080, 1920)

    def _escape_sub_path(self, p: str) -> str:
        # subtitles filter is picky: escape backslashes/colons/quotes
        s = p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        return s

    def compose_one(
        self,
        *,
        out_path: str,
        aspect: str,
        performer_a_mp4: str,
        audio_master_path: str,
        performer_b_mp4: Optional[str] = None,
        captions_srt: Optional[str] = None,
    ) -> None:
        W, H = self._dims_for_aspect(aspect)
        has_b = bool(performer_b_mp4)

        # Inputs: A, (B?), audio(last)
        cmd = [self.ffmpeg, "-y", "-i", performer_a_mp4]
        if has_b:
            cmd += ["-i", performer_b_mp4]
        cmd += ["-i", audio_master_path]

        # Build video
        fc: List[str] = []
        if has_b:
            half_w = W // 2
            fc.append(
                f"[0:v]scale={half_w}:{H}:force_original_aspect_ratio=increase,crop={half_w}:{H}[va]"
            )
            fc.append(
                f"[1:v]scale={half_w}:{H}:force_original_aspect_ratio=increase,crop={half_w}:{H}[vb]"
            )
            fc.append("[va][vb]hstack=inputs=2[v0]")
        else:
            fc.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[v0]")

        video_out = "[v0]"
        if captions_srt:
            srt = self._escape_sub_path(captions_srt)
            # Requires ffmpeg built with libass; if not available, skip captions_srt in orchestrator.
            fc.append(f"{video_out}subtitles='{srt}'[v]")
            video_out = "[v]"

        filter_complex = ";".join(fc)

        # audio stream index is last input
        audio_idx = 2 if has_b else 1

        cmd += [
            "-filter_complex", filter_complex,
            "-map", video_out,
            "-map", f"{audio_idx}:a",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            out_path,
        ]
        self._run(cmd)

    def clip_preview(
        self,
        *,
        in_path: str,
        out_path: str,
        start_s: float,
        duration_s: float,
    ) -> None:
        cmd = [
            self.ffmpeg, "-y",
            "-ss", str(float(start_s)),
            "-t", str(float(duration_s)),
            "-i", in_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            out_path,
        ]
        self._run(cmd)

    def compose(
        self,
        *,
        out_dir: str,
        performer_a_mp4: str,
        audio_master_path: str,
        exports: List[str],
        performer_b_mp4: Optional[str] = None,
        captions_srt: Optional[str] = None,
        chorus_preview: bool = True,
        chorus_start_s: float = 30.0,
        chorus_duration_s: float = 12.0,
    ) -> ComposeResult:
        os.makedirs(out_dir, exist_ok=True)

        outputs: Dict[str, str] = {}
        for aspect in exports:
            out_path = os.path.join(out_dir, f"final_{aspect.replace(':','x')}.mp4")
            self.compose_one(
                out_path=out_path,
                aspect=aspect,
                performer_a_mp4=performer_a_mp4,
                performer_b_mp4=performer_b_mp4,
                audio_master_path=audio_master_path,
                captions_srt=captions_srt,
            )
            outputs[aspect] = out_path

        preview_path = None
        if chorus_preview:
            base = outputs.get("9:16") or next(iter(outputs.values()), None)
            if base:
                preview_path = os.path.join(out_dir, "preview_chorus.mp4")
                self.clip_preview(
                    in_path=base,
                    out_path=preview_path,
                    start_s=chorus_start_s,
                    duration_s=chorus_duration_s,
                )

        return ComposeResult(outputs=outputs, preview_path=preview_path)