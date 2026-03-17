from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageStat

try:
    import torch
    from transformers import CLIPModel, CLIPProcessor
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    CLIPModel = None  # type: ignore
    CLIPProcessor = None  # type: ignore


_FAMILY_ALIASES: Dict[str, str] = {
    "hoodie": "hoodie",
    "blazer": "blazer",
    "jeans": "jeans",
    "dress": "dress",
    "dresses": "dress",
    "kurta": "kurta",
    "kurta_pyjama": "kurta",
    "salwar": "salwar_suit",
    "salwar suit": "salwar_suit",
    "salwar_suit": "salwar_suit",
    "shalwar kameez": "salwar_suit",
    "lehenga": "lehenga",
    "lehenga_set": "lehenga",
    "sherwani": "sherwani",
}

_FAMILY_TEXT: Dict[str, str] = {
    "hoodie": "hoodie sweatshirt",
    "blazer": "blazer jacket",
    "jeans": "jeans denim pants",
    "dress": "dress gown",
    "kurta": "kurta tunic",
    "salwar_suit": "salwar suit shalwar kameez",
    "lehenga": "lehenga choli",
    "sherwani": "sherwani coat",
}

_ALLOWED_FAMILIES: Tuple[str, ...] = (
    "hoodie",
    "blazer",
    "jeans",
    "dress",
    "kurta",
    "salwar_suit",
    "lehenga",
    "sherwani",
)


@dataclass
class QCConfig:
    min_width: int = 384
    min_height: int = 384
    min_entropy: float = 2.5
    min_brightness_stddev: float = 12.0

    reject_if_target_matches_garment_hash_le: int = 6
    reject_if_target_matches_model_hash_le: int = 6
    reject_if_target_matches_garment_hist_ge: float = 0.985
    reject_if_target_matches_model_hist_ge: float = 0.985

    require_clip_classifier: bool = True
    clip_model_id: str = "openai/clip-vit-base-patch32"
    clip_min_expected_wearing_score: float = 0.22
    clip_min_expected_margin_over_product: float = 0.06
    clip_min_expected_margin_over_other_family: float = 0.04

    allow_without_clip: bool = False

    @classmethod
    def from_env(cls) -> "QCConfig":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except Exception:
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(float(os.getenv(name, str(default))))
            except Exception:
                return default

        def _b(name: str, default: bool) -> bool:
            v = os.getenv(name, "1" if default else "0").strip().lower()
            return v in {"1", "true", "yes", "y", "on"}

        return cls(
            min_width=_i("DF_NONSAREE_QC_MIN_WIDTH", 384),
            min_height=_i("DF_NONSAREE_QC_MIN_HEIGHT", 384),
            min_entropy=_f("DF_NONSAREE_QC_MIN_ENTROPY", 2.5),
            min_brightness_stddev=_f("DF_NONSAREE_QC_MIN_BRIGHTNESS_STDDEV", 12.0),
            reject_if_target_matches_garment_hash_le=_i("DF_NONSAREE_QC_REJECT_GARMENT_HASH_LE", 6),
            reject_if_target_matches_model_hash_le=_i("DF_NONSAREE_QC_REJECT_MODEL_HASH_LE", 6),
            reject_if_target_matches_garment_hist_ge=_f("DF_NONSAREE_QC_REJECT_GARMENT_HIST_GE", 0.985),
            reject_if_target_matches_model_hist_ge=_f("DF_NONSAREE_QC_REJECT_MODEL_HIST_GE", 0.985),
            require_clip_classifier=_b("DF_NONSAREE_QC_REQUIRE_CLIP", True),
            clip_model_id=(os.getenv("DF_NONSAREE_QC_CLIP_MODEL_ID") or "openai/clip-vit-base-patch32").strip(),
            clip_min_expected_wearing_score=_f("DF_NONSAREE_QC_CLIP_MIN_WEARING_SCORE", 0.22),
            clip_min_expected_margin_over_product=_f("DF_NONSAREE_QC_CLIP_MIN_MARGIN_PRODUCT", 0.06),
            clip_min_expected_margin_over_other_family=_f("DF_NONSAREE_QC_CLIP_MIN_MARGIN_OTHER", 0.04),
            allow_without_clip=_b("DF_NONSAREE_QC_ALLOW_WITHOUT_CLIP", False),
        )


@dataclass
class QCDecision:
    accepted: bool
    status: str  # accepted|rejected|review
    expected_family: str
    predicted_family: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    clip: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "expected_family": self.expected_family,
            "predicted_family": self.predicted_family,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "clip": dict(self.clip),
            "context": dict(self.context),
        }


def _canon_family(x: str) -> str:
    s = str(x or "").strip().lower().replace("-", "_")
    return _FAMILY_ALIASES.get(s, s)


def _image_entropy(img: Image.Image) -> float:
    hist = img.convert("L").histogram()
    total = float(sum(hist) or 1.0)
    entropy = 0.0
    for h in hist:
        if h <= 0:
            continue
        p = h / total
        entropy -= p * math.log2(p)
    return float(entropy)


def _avg_hash(img: Image.Image, size: int = 16) -> int:
    im = img.convert("L").resize((size, size))
    pixels = list(im.getdata())
    avg = sum(pixels) / float(len(pixels) or 1)
    bits = 0
    for i, px in enumerate(pixels):
        if px >= avg:
            bits |= 1 << i
    return bits


def _hamming_distance(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def _hist_cosine_similarity(a: Image.Image, b: Image.Image) -> float:
    ha = a.convert("RGB").resize((128, 128)).histogram()
    hb = b.convert("RGB").resize((128, 128)).histogram()
    dot = 0.0
    na = 0.0
    nb = 0.0
    for xa, xb in zip(ha, hb):
        fa = float(xa)
        fb = float(xb)
        dot += fa * fb
        na += fa * fa
        nb += fb * fb
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / math.sqrt(na * nb))


def _image_stats(img: Image.Image) -> Dict[str, Any]:
    rgb = img.convert("RGB")
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    stddev = float(stat.stddev[0] if stat.stddev else 0.0)
    mean = float(stat.mean[0] if stat.mean else 0.0)
    return {
        "width": int(rgb.width),
        "height": int(rgb.height),
        "entropy": _image_entropy(gray),
        "brightness_mean": mean,
        "brightness_stddev": stddev,
        "avg_hash": _avg_hash(rgb),
    }


class _ClipScorer:
    _model: Any = None
    _processor: Any = None
    _device: str = "cpu"
    _model_id: Optional[str] = None

    @classmethod
    def available(cls) -> bool:
        return CLIPModel is not None and CLIPProcessor is not None and torch is not None

    @classmethod
    def _ensure_loaded(cls, model_id: str) -> None:
        if not cls.available():
            raise RuntimeError("transformers/torch not available")
        if cls._model is not None and cls._processor is not None and cls._model_id == model_id:
            return

        cls._processor = CLIPProcessor.from_pretrained(model_id)
        cls._model = CLIPModel.from_pretrained(model_id)
        cls._model.eval()
        cls._device = "cuda" if torch.cuda.is_available() else "cpu"
        cls._model.to(cls._device)
        cls._model_id = model_id

    @classmethod
    def score_prompts(cls, *, model_id: str, image: Image.Image, prompts: List[str]) -> List[float]:
        cls._ensure_loaded(model_id)
        assert cls._processor is not None
        assert cls._model is not None
        assert torch is not None

        inputs = cls._processor(text=prompts, images=image, return_tensors="pt", padding=True)
        for k, v in list(inputs.items()):
            if hasattr(v, "to"):
                inputs[k] = v.to(cls._device)

        with torch.no_grad():
            outputs = cls._model(**inputs)
            probs = outputs.logits_per_image[0].softmax(dim=0).detach().cpu().tolist()
        return [float(x) for x in probs]


class NonSareeQCGate:
    def __init__(self, config: Optional[QCConfig] = None) -> None:
        self.config = config or QCConfig.from_env()

    @classmethod
    def from_env(cls) -> "NonSareeQCGate":
        return cls(QCConfig.from_env())

    def _family_prompts(self) -> Tuple[List[str], List[Tuple[str, str]]]:
        prompts: List[str] = []
        mapping: List[Tuple[str, str]] = []

        for fam in _ALLOWED_FAMILIES:
            txt = _FAMILY_TEXT[fam]
            prompts.append(f"a studio photo of a person wearing a {txt}")
            mapping.append((fam, "wearing"))

        for fam in _ALLOWED_FAMILIES:
            txt = _FAMILY_TEXT[fam]
            prompts.append(f"a standalone product photo of a {txt} on a plain background")
            mapping.append((fam, "product"))

        prompts.append("a studio photo of a single person wearing clothing")
        mapping.append(("generic_person", "wearing"))

        prompts.append("a standalone product photo of clothing with no person")
        mapping.append(("generic_product", "product"))

        return prompts, mapping

    def _clip_family_check(self, *, expected_family: str, target_img: Image.Image) -> Dict[str, Any]:
        if not _ClipScorer.available():
            return {
                "available": False,
                "reason": "clip_not_available",
            }

        prompts, mapping = self._family_prompts()
        probs = _ClipScorer.score_prompts(
            model_id=self.config.clip_model_id,
            image=target_img.convert("RGB"),
            prompts=prompts,
        )

        wearing_scores: Dict[str, float] = {}
        product_scores: Dict[str, float] = {}
        best_label = None
        best_score = -1.0

        for (fam, kind), score in zip(mapping, probs):
            if score > best_score:
                best_label = {"family": fam, "kind": kind}
                best_score = score
            if kind == "wearing":
                wearing_scores[fam] = float(score)
            elif kind == "product":
                product_scores[fam] = float(score)

        expected_wearing = float(wearing_scores.get(expected_family, 0.0))
        expected_product = float(product_scores.get(expected_family, 0.0))

        other_wearing = 0.0
        other_family = None
        for fam, score in wearing_scores.items():
            if fam in {expected_family, "generic_person"}:
                continue
            if score > other_wearing:
                other_wearing = float(score)
                other_family = fam

        predicted_family = None
        predicted_score = -1.0
        for fam, score in wearing_scores.items():
            if fam in {"generic_person"}:
                continue
            if score > predicted_score:
                predicted_family = fam
                predicted_score = float(score)

        human_vs_product_margin = wearing_scores.get("generic_person", 0.0) - product_scores.get("generic_product", 0.0)

        return {
            "available": True,
            "predicted_family": predicted_family,
            "predicted_family_score": predicted_score,
            "expected_wearing_score": expected_wearing,
            "expected_product_score": expected_product,
            "margin_over_product": expected_wearing - expected_product,
            "margin_over_other_family": expected_wearing - other_wearing,
            "other_family": other_family,
            "other_family_score": other_wearing,
            "human_vs_product_margin": human_vs_product_margin,
            "best_overall": best_label,
            "all_wearing_scores": wearing_scores,
            "all_product_scores": product_scores,
        }

    def evaluate(
        self,
        *,
        expected_family: str,
        source_garment_path: str,
        source_model_path: str,
        target_path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> QCDecision:
        expected = _canon_family(expected_family)
        context = dict(context or {})

        if expected not in _ALLOWED_FAMILIES:
            return QCDecision(
                accepted=False,
                status="review",
                expected_family=expected,
                reasons=[f"unsupported_expected_family:{expected}"],
                context=context,
            )

        garment_img = Image.open(source_garment_path).convert("RGB")
        model_img = Image.open(source_model_path).convert("RGB")
        target_img = Image.open(target_path).convert("RGB")

        garment_stats = _image_stats(garment_img)
        model_stats = _image_stats(model_img)
        target_stats = _image_stats(target_img)

        garment_hash_dist = _hamming_distance(int(target_stats["avg_hash"]), int(garment_stats["avg_hash"]))
        model_hash_dist = _hamming_distance(int(target_stats["avg_hash"]), int(model_stats["avg_hash"]))
        garment_hist = _hist_cosine_similarity(target_img, garment_img)
        model_hist = _hist_cosine_similarity(target_img, model_img)

        reasons: List[str] = []

        if int(target_stats["width"]) < self.config.min_width or int(target_stats["height"]) < self.config.min_height:
            reasons.append("target_too_small")

        if float(target_stats["entropy"]) < self.config.min_entropy:
            reasons.append("target_low_entropy")

        if float(target_stats["brightness_stddev"]) < self.config.min_brightness_stddev:
            reasons.append("target_low_brightness_stddev")

        if garment_hash_dist <= self.config.reject_if_target_matches_garment_hash_le and garment_hist >= self.config.reject_if_target_matches_garment_hist_ge:
            reasons.append("target_matches_garment_input")

        if model_hash_dist <= self.config.reject_if_target_matches_model_hash_le and model_hist >= self.config.reject_if_target_matches_model_hist_ge:
            reasons.append("target_matches_model_input")

        clip_info = self._clip_family_check(expected_family=expected, target_img=target_img)

        if clip_info.get("available"):
            predicted_family = clip_info.get("predicted_family")
            expected_wearing_score = float(clip_info.get("expected_wearing_score") or 0.0)
            margin_over_product = float(clip_info.get("margin_over_product") or 0.0)
            margin_over_other_family = float(clip_info.get("margin_over_other_family") or 0.0)
            human_vs_product_margin = float(clip_info.get("human_vs_product_margin") or 0.0)

            if predicted_family != expected:
                reasons.append(f"predicted_family_mismatch:{predicted_family}")

            if expected_wearing_score < self.config.clip_min_expected_wearing_score:
                reasons.append("expected_family_wearing_score_too_low")

            if margin_over_product < self.config.clip_min_expected_margin_over_product:
                reasons.append("expected_family_not_stronger_than_product_only")

            if margin_over_other_family < self.config.clip_min_expected_margin_over_other_family:
                reasons.append("expected_family_not_stronger_than_other_family")

            if human_vs_product_margin <= 0.0:
                reasons.append("target_looks_like_product_only")
        else:
            if self.config.require_clip_classifier and not self.config.allow_without_clip:
                return QCDecision(
                    accepted=False,
                    status="review",
                    expected_family=expected,
                    predicted_family=None,
                    reasons=["clip_required_but_unavailable"],
                    metrics={
                        "target": target_stats,
                        "source_garment": garment_stats,
                        "source_model": model_stats,
                        "garment_hash_distance": garment_hash_dist,
                        "model_hash_distance": model_hash_dist,
                        "garment_hist_similarity": garment_hist,
                        "model_hist_similarity": model_hist,
                    },
                    clip=clip_info,
                    context=context,
                )

        accepted = len(reasons) == 0
        return QCDecision(
            accepted=accepted,
            status="accepted" if accepted else "rejected",
            expected_family=expected,
            predicted_family=clip_info.get("predicted_family"),
            reasons=reasons,
            metrics={
                "target": target_stats,
                "source_garment": garment_stats,
                "source_model": model_stats,
                "garment_hash_distance": garment_hash_dist,
                "model_hash_distance": model_hash_dist,
                "garment_hist_similarity": garment_hist,
                "model_hist_similarity": model_hist,
            },
            clip=clip_info,
            context=context,
        )