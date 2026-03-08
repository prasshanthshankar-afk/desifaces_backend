from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# small helpers
# -------------------------------------------------------------------

def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _uniq_strs(xs: Sequence[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        s = _norm(x)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _stable_hash_int(s: str) -> int:
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest(), 16)


def _parse_az_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("az://"):
        raise ValueError(f"Not an az:// URI: {uri}")
    rest = uri[len("az://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid az:// URI: {uri}")
    return parts[0], parts[1]


def _download_http_text(url: str, timeout_s: int = 120) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_blob_service_client() -> BlobServiceClient:
    conn = (
        os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        or os.environ.get("AZURE_BLOB_CONNECTION_STRING")
        or os.environ.get("AZURE_STORAGE_CONN_STR")
    )
    if conn:
        return BlobServiceClient.from_connection_string(conn)

    account_url = (
        os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        or os.environ.get("AZURE_BLOB_ACCOUNT_URL")
    )
    credential = (
        os.environ.get("AZURE_STORAGE_KEY")
        or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        or os.environ.get("AZURE_BLOB_KEY")
        or os.environ.get("AZURE_BLOB_SAS_TOKEN")
    )
    if account_url and credential:
        return BlobServiceClient(account_url=account_url, credential=credential)

    raise RuntimeError(
        "Azure credentials not found. Set AZURE_STORAGE_CONNECTION_STRING "
        "or AZURE_STORAGE_ACCOUNT_URL + key/SAS."
    )


def _download_az_text(uri: str) -> str:
    bsc = _get_blob_service_client()
    container, blob_name = _parse_az_uri(uri)
    bc = bsc.get_blob_client(container=container, blob=blob_name)
    return bc.download_blob().readall().decode("utf-8", errors="replace")


def _load_json_from_uri(uri: str) -> Dict[str, Any]:
    if uri.startswith("az://"):
        raw = _download_az_text(uri)
        return json.loads(raw)
    if uri.startswith("http://") or uri.startswith("https://"):
        raw = _download_http_text(uri)
        return json.loads(raw)

    with open(uri, "r", encoding="utf-8") as f:
        return json.load(f)


def _default_allowed_for_gender(gender: str) -> List[str]:
    g = _norm(gender)
    if g == "female":
        return ["salwar_suit", "lehenga_set", "upper_body", "lower_body", "dresses"]
    if g == "male":
        return ["kurta_pyjama", "dhoti_kurta", "sherwani", "upper_body", "lower_body", "dresses"]
    return ["upper_body", "lower_body", "dresses"]


def _derive_scan_prefix_from_manifest_uri(manifest_uri: str) -> Optional[str]:
    explicit = (os.environ.get("COMMERCE_PLATFORM_MODELS_PREFIX") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    if manifest_uri.startswith("az://"):
        container, blob_name = _parse_az_uri(manifest_uri)
        if blob_name.endswith("/manifest.json"):
            prefix = blob_name[: -len("/manifest.json")]
            return f"az://{container}/{prefix}".rstrip("/")
    return None


# -------------------------------------------------------------------
# data models
# -------------------------------------------------------------------

@dataclass(frozen=True)
class PlatformModelAsset:
    role: str
    url: str
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class PlatformModel:
    model_code: str
    gender: str
    framing: str
    pose: str
    age_band: str
    region: str
    body_type: str
    skin_tone: str
    style_tags: List[str]
    quality_score: float
    is_active: bool
    allowed_garment_kinds: List[str]
    preferred_garment_kinds: List[str]
    qc: Dict[str, Any]
    meta: Dict[str, Any]
    assets: List[PlatformModelAsset] = field(default_factory=list)


# -------------------------------------------------------------------
# selector
# -------------------------------------------------------------------

class PlatformModelSelector:
    """
    Production-grade selector.

    Improvements over the earlier version:
    - manifest-first, but auto-falls back to Azure prefix scan if manifest is missing
    - skips broken/missing blob assets instead of crashing
    - supports both Indian garment families and generic western/non-saree families
    - deterministic selection from top-K, but blob-aware
    """

    def __init__(
        self,
        *,
        manifest_uri: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_ttl_s: Optional[int] = None,
        top_k_default: Optional[int] = None,
        asset_url_resolver: Optional[Callable[[str], str]] = None,
        asset_exists_checker: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.manifest_uri = (
            manifest_uri
            or os.environ.get("COMMERCE_PLATFORM_MODELS_MANIFEST")
            or "az://commerce-training/pools/platform_models/v1/manifest.json"
        )
        self.cache_dir = (
            cache_dir
            or os.environ.get("COMMERCE_PLATFORM_MODELS_CACHE_DIR")
            or "/var/cache/df_platform_models"
        )
        self.cache_ttl_s = int(
            cache_ttl_s
            or os.environ.get("COMMERCE_PLATFORM_MODELS_CACHE_TTL_S")
            or 300
        )
        self.top_k_default = int(
            top_k_default
            or os.environ.get("COMMERCE_PLATFORM_MODELS_TOP_K")
            or 10
        )
        self.asset_url_resolver = asset_url_resolver
        self.asset_exists_checker = asset_exists_checker
        self.scan_prefix = _derive_scan_prefix_from_manifest_uri(self.manifest_uri)

        self._lock = threading.Lock()
        self._manifest_loaded_at = 0.0
        self._manifest: Dict[str, Any] = {}

        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # manifest loading
    # ------------------------------------------------------------

    def _cache_path(self) -> str:
        safe = hashlib.sha256(self.manifest_uri.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, f"platform_models_manifest_{safe}.json")

    def _validate_manifest(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ValueError("Platform model manifest must be a dict")
        models = manifest.get("models")
        if not isinstance(models, list):
            raise ValueError("Platform model manifest missing models[]")
        return manifest

    def _scan_azure_prefix_to_manifest(self, prefix_uri: str) -> Dict[str, Any]:
        if not prefix_uri.startswith("az://"):
            raise ValueError(f"Unsupported scan prefix: {prefix_uri}")

        bsc = _get_blob_service_client()
        container, prefix = _parse_az_uri(prefix_uri)
        prefix = prefix.rstrip("/") + "/"

        by_model: Dict[str, Dict[str, Any]] = {}

        cc = bsc.get_container_client(container)
        for blob in cc.list_blobs(name_starts_with=prefix):
            name = str(blob.name)
            rel = name[len(prefix) :]
            parts = rel.split("/")
            if len(parts) < 5:
                continue

            gender, framing, pose, model_code = parts[0], parts[1], parts[2], parts[3]
            filename = parts[4]
            key = f"{gender}/{framing}/{pose}/{model_code}"

            entry = by_model.setdefault(
                key,
                {
                    "model_code": model_code,
                    "gender": _norm(gender),
                    "framing": _norm(framing),
                    "pose": _norm(pose),
                    "age_band": "adult",
                    "region": "india",
                    "body_type": "average",
                    "skin_tone": "medium",
                    "style_tags": ["catalog", "clean_bg"],
                    "quality_score": 0.0,
                    "is_active": True,
                    "allowed_garment_kinds": [],
                    "preferred_garment_kinds": [],
                    "qc": {},
                    "meta": {},
                    "assets": [],
                },
            )

            if filename == "meta.json":
                try:
                    bc = bsc.get_blob_client(container=container, blob=name)
                    meta = json.loads(bc.download_blob().readall().decode("utf-8", errors="replace"))
                except Exception:
                    meta = {}
                if isinstance(meta, dict):
                    entry["age_band"] = _norm(meta.get("age_band") or entry["age_band"])
                    entry["region"] = _norm(meta.get("region") or entry["region"])
                    entry["body_type"] = _norm(meta.get("body_type") or entry["body_type"])
                    entry["skin_tone"] = _norm(meta.get("skin_tone") or entry["skin_tone"])
                    entry["quality_score"] = _safe_float(meta.get("quality_score"), entry["quality_score"])
                    entry["is_active"] = bool(meta.get("is_active", True))
                    entry["style_tags"] = _uniq_strs(meta.get("style_tags") or entry["style_tags"])
                    entry["allowed_garment_kinds"] = _uniq_strs(meta.get("allowed_garment_kinds") or entry["allowed_garment_kinds"])
                    entry["preferred_garment_kinds"] = _uniq_strs(meta.get("preferred_garment_kinds") or entry["preferred_garment_kinds"])
                    entry["qc"] = meta.get("qc") if isinstance(meta.get("qc"), dict) else entry["qc"]
                    entry["meta"] = meta
                continue

            lower = filename.lower()
            if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                role = "primary" if lower.startswith("primary") else os.path.splitext(filename)[0]
                entry["assets"].append(
                    {
                        "role": role,
                        "url": f"az://{container}/{name}",
                        "width": None,
                        "height": None,
                    }
                )

        models: List[Dict[str, Any]] = []
        for _, entry in sorted(by_model.items()):
            if not entry["assets"]:
                continue
            if not entry["allowed_garment_kinds"]:
                entry["allowed_garment_kinds"] = _default_allowed_for_gender(entry["gender"])
            models.append(entry)

        return {
            "version": "1.0",
            "source": f"scan:{prefix_uri}",
            "generated_at": int(time.time()),
            "models": models,
        }

    def _load_manifest_uncached(self) -> Dict[str, Any]:
        try:
            manifest = _load_json_from_uri(self.manifest_uri)
            manifest = self._validate_manifest(manifest)
        except Exception as e:
            if self.scan_prefix:
                logger.warning("platform_model_selector: manifest load failed (%s); scanning prefix %s", e, self.scan_prefix)
                manifest = self._scan_azure_prefix_to_manifest(self.scan_prefix)
                manifest = self._validate_manifest(manifest)
            else:
                raise

        cache_path = self._cache_path()
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            logger.exception("Failed to persist platform model manifest cache to %s", cache_path)

        return manifest

    def load_manifest(self, *, force_refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._manifest
                and (now - self._manifest_loaded_at) < self.cache_ttl_s
            ):
                return self._manifest

            cache_path = self._cache_path()

            try:
                manifest = self._load_manifest_uncached()
                self._manifest = manifest
                self._manifest_loaded_at = now
                return manifest
            except Exception:
                logger.exception("Failed to refresh platform model manifest from %s", self.manifest_uri)

                if os.path.exists(cache_path):
                    try:
                        with open(cache_path, "r", encoding="utf-8") as f:
                            manifest = json.load(f)
                        manifest = self._validate_manifest(manifest)
                        self._manifest = manifest
                        self._manifest_loaded_at = now
                        logger.warning("Using cached platform model manifest from %s", cache_path)
                        return manifest
                    except Exception:
                        logger.exception("Cached platform model manifest is invalid: %s", cache_path)

                raise

    def refresh_manifest(self) -> Dict[str, Any]:
        return self.load_manifest(force_refresh=True)

    def manifest_summary(self) -> Dict[str, Any]:
        manifest = self.load_manifest()
        models = self._parse_models(manifest)
        by_gender: Dict[str, int] = {}
        by_bucket: Dict[str, int] = {}
        active_count = 0

        for m in models:
            by_gender[m.gender] = by_gender.get(m.gender, 0) + 1
            bucket = f"{m.gender}/{m.framing}/{m.pose}"
            by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
            if m.is_active:
                active_count += 1

        return {
            "manifest_uri": self.manifest_uri,
            "scan_prefix": self.scan_prefix,
            "model_count": len(models),
            "active_count": active_count,
            "by_gender": by_gender,
            "by_bucket": by_bucket,
        }

    # ------------------------------------------------------------
    # model parsing
    # ------------------------------------------------------------

    def _parse_models(self, manifest: Dict[str, Any]) -> List[PlatformModel]:
        out: List[PlatformModel] = []

        for raw in manifest.get("models", []) or []:
            if not isinstance(raw, dict):
                continue

            assets: List[PlatformModelAsset] = []
            for a in raw.get("assets", []) or []:
                if not isinstance(a, dict):
                    continue
                url = str(a.get("url") or "").strip()
                if not url:
                    continue
                assets.append(
                    PlatformModelAsset(
                        role=str(a.get("role") or "primary"),
                        url=url,
                        width=_safe_int(a.get("width")) or None,
                        height=_safe_int(a.get("height")) or None,
                    )
                )

            if not assets:
                continue

            model_code = str(raw.get("model_code") or "").strip()
            if not model_code:
                continue

            gender = _norm(raw.get("gender") or "any")
            allowed = _uniq_strs(_as_list(raw.get("allowed_garment_kinds")))
            if not allowed:
                allowed = _default_allowed_for_gender(gender)

            out.append(
                PlatformModel(
                    model_code=model_code,
                    gender=gender,
                    framing=_norm(raw.get("framing")),
                    pose=_norm(raw.get("pose")),
                    age_band=_norm(raw.get("age_band") or "adult"),
                    region=_norm(raw.get("region") or "india"),
                    body_type=_norm(raw.get("body_type") or "average"),
                    skin_tone=_norm(raw.get("skin_tone") or "medium"),
                    style_tags=_uniq_strs(_as_list(raw.get("style_tags"))),
                    quality_score=_safe_float(raw.get("quality_score"), 0.0),
                    is_active=bool(raw.get("is_active", True)),
                    allowed_garment_kinds=allowed,
                    preferred_garment_kinds=_uniq_strs(_as_list(raw.get("preferred_garment_kinds"))),
                    qc=raw.get("qc") if isinstance(raw.get("qc"), dict) else {},
                    meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
                    assets=assets,
                )
            )

        return out

    def list_models(self, *, force_refresh_manifest: bool = False) -> List[PlatformModel]:
        manifest = self.load_manifest(force_refresh=force_refresh_manifest)
        return self._parse_models(manifest)

    # ------------------------------------------------------------
    # filtering + ranking
    # ------------------------------------------------------------

    def _primary_asset(self, model: PlatformModel) -> PlatformModelAsset:
        primaries = [a for a in model.assets if _norm(a.role) == "primary"]
        return primaries[0] if primaries else model.assets[0]

    def _strict_rules(self, garment_kind: str) -> Dict[str, Any]:
        g = _norm(garment_kind)
        if g in {"salwar_suit", "lehenga_set"}:
            return {"gender": "female", "framing": {"full_body"}, "pose": {"front"}}
        if g == "kurta_pyjama":
            return {"gender": "male", "framing": {"full_body", "three_quarter"}, "pose": {"front"}}
        if g == "dhoti_kurta":
            return {"gender": "male", "framing": {"full_body"}, "pose": {"front"}}
        if g == "sherwani":
            return {"gender": "male", "framing": {"full_body", "three_quarter"}, "pose": {"front"}}
        # generic western / non-saree families
        if g in {"upper_body", "lower_body", "dresses"}:
            return {"gender": None, "framing": {"full_body", "three_quarter"}, "pose": {"front"}}
        return {"gender": None, "framing": set(), "pose": set()}

    def _eligible_models(
        self,
        models: Sequence[PlatformModel],
        *,
        garment_kind: str,
    ) -> List[PlatformModel]:
        g = _norm(garment_kind)
        rules = self._strict_rules(g)
        generic_garments = {"upper_body", "lower_body", "dresses"}

        def _matches(m: PlatformModel, *, relax_pose_framing: bool, relax_allowed: bool) -> bool:
            if not m.is_active or not m.model_code:
                return False
            if rules["gender"] and m.gender != rules["gender"]:
                return False
            if not relax_pose_framing:
                if rules["framing"] and m.framing not in rules["framing"]:
                    return False
                if rules["pose"] and m.pose not in rules["pose"]:
                    return False
            if not relax_allowed:
                if m.allowed_garment_kinds and g not in m.allowed_garment_kinds:
                    return False
            return True

        # exact
        exact = [m for m in models if _matches(m, relax_pose_framing=False, relax_allowed=False)]
        if exact:
            return exact

        # generic garment family: allow models with no explicit garment mapping
        if g in generic_garments:
            generic_relaxed = [m for m in models if _matches(m, relax_pose_framing=False, relax_allowed=True)]
            if generic_relaxed:
                return generic_relaxed

        # relax framing/pose first
        relaxed_pose = [m for m in models if _matches(m, relax_pose_framing=True, relax_allowed=False)]
        if relaxed_pose:
            return relaxed_pose

        # last resort for generic families only
        relaxed_all = [m for m in models if _matches(m, relax_pose_framing=True, relax_allowed=True)]
        return relaxed_all

    def _rank_score(
        self,
        *,
        model: PlatformModel,
        garment_kind: str,
        preferred_tags: Optional[Sequence[str]],
        recent_model_codes: Optional[Sequence[str]],
    ) -> float:
        g = _norm(garment_kind)
        score = 0.0

        score += model.quality_score * 100.0

        if g in model.preferred_garment_kinds:
            score += 25.0
        if g in model.allowed_garment_kinds:
            score += 10.0

        if "catalog" in model.style_tags:
            score += 5.0
        if "clean_bg" in model.style_tags:
            score += 5.0
        if "ethnic_friendly" in model.style_tags:
            score += 4.0

        if model.qc.get("full_body_visible") is True:
            score += 5.0
        if model.qc.get("face_ok") is True:
            score += 3.0
        if model.qc.get("hands_ok") is True:
            score += 2.0

        if preferred_tags:
            wanted = {_norm(t) for t in preferred_tags if _norm(t)}
            score += sum(2.0 for t in model.style_tags if t in wanted)

        if recent_model_codes:
            recent = {_norm(x) for x in recent_model_codes if _norm(x)}
            if _norm(model.model_code) in recent:
                score -= 15.0

        if g in {"salwar_suit", "lehenga_set", "dhoti_kurta"} and model.framing == "full_body":
            score += 6.0

        if model.pose == "front":
            score += 1.0

        return round(score, 4)

    def _default_asset_exists(self, url: str) -> bool:
        try:
            if url.startswith("az://"):
                bsc = _get_blob_service_client()
                container, blob_name = _parse_az_uri(url)
                return bool(bsc.get_blob_client(container=container, blob=blob_name).exists())
            if url.startswith("http://") or url.startswith("https://"):
                return True
            return os.path.exists(url)
        except Exception:
            return False

    def _asset_exists(self, url: str) -> bool:
        if self.asset_exists_checker:
            try:
                return bool(self.asset_exists_checker(url))
            except Exception:
                return False
        return self._default_asset_exists(url)

    def _resolve_asset_url(self, url: str) -> str:
        if self.asset_url_resolver:
            return self.asset_url_resolver(url)
        return url

    def _safe_resolve_primary(self, model: PlatformModel) -> Optional[str]:
        primary = self._primary_asset(model)
        if not self._asset_exists(primary.url):
            logger.warning("platform_model_selector: skipping missing asset model_code=%s url=%s", model.model_code, primary.url)
            return None
        try:
            resolved = self._resolve_asset_url(primary.url)
            return str(resolved or "").strip() or None
        except Exception as e:
            logger.warning("platform_model_selector: skipping unresolved asset model_code=%s url=%s err=%s", model.model_code, primary.url, e)
            return None

    def list_eligible_models(
        self,
        *,
        garment_kind: str,
        preferred_tags: Optional[Sequence[str]] = None,
        recent_model_codes: Optional[Sequence[str]] = None,
        force_refresh_manifest: bool = False,
    ) -> List[Dict[str, Any]]:
        manifest = self.load_manifest(force_refresh=force_refresh_manifest)
        models = self._parse_models(manifest)
        eligible = self._eligible_models(models, garment_kind=garment_kind)

        ranked: List[Tuple[float, PlatformModel]] = []
        for m in eligible:
            ranked.append(
                (
                    self._rank_score(
                        model=m,
                        garment_kind=garment_kind,
                        preferred_tags=preferred_tags,
                        recent_model_codes=recent_model_codes,
                    ),
                    m,
                )
            )
        ranked.sort(key=lambda x: (-x[0], x[1].model_code))

        out: List[Dict[str, Any]] = []
        for score, m in ranked:
            resolved = self._safe_resolve_primary(m)
            if not resolved:
                continue
            out.append(
                {
                    "model_code": m.model_code,
                    "gender": m.gender,
                    "framing": m.framing,
                    "pose": m.pose,
                    "quality_score": m.quality_score,
                    "rank_score": score,
                    "allowed_garment_kinds": m.allowed_garment_kinds,
                    "preferred_garment_kinds": m.preferred_garment_kinds,
                    "style_tags": m.style_tags,
                    "primary_asset_url": resolved,
                }
            )
        return out

    # ------------------------------------------------------------
    # public API
    # ------------------------------------------------------------

    def select_platform_model(
        self,
        *,
        garment_kind: str,
        tenant_id: str,
        quote_id: str,
        product_id: Optional[str] = None,
        preferred_tags: Optional[Sequence[str]] = None,
        recent_model_codes: Optional[Sequence[str]] = None,
        top_k: Optional[int] = None,
        force_refresh_manifest: bool = False,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(force_refresh=force_refresh_manifest)
        models = self._parse_models(manifest)
        eligible = self._eligible_models(models, garment_kind=garment_kind)

        if not eligible:
            raise ValueError(f"No eligible platform models found for garment_kind={garment_kind}")

        ranked: List[Tuple[float, PlatformModel]] = []
        for m in eligible:
            ranked.append(
                (
                    self._rank_score(
                        model=m,
                        garment_kind=garment_kind,
                        preferred_tags=preferred_tags,
                        recent_model_codes=recent_model_codes,
                    ),
                    m,
                )
            )

        ranked.sort(key=lambda x: (-x[0], x[1].model_code))

        effective_top_k = max(1, min(int(top_k or self.top_k_default), len(ranked)))
        top_candidates = ranked[:effective_top_k]

        seed_text = f"{_norm(tenant_id)}:{_norm(quote_id)}:{_norm(product_id)}:{_norm(garment_kind)}"
        start_idx = _stable_hash_int(seed_text) % len(top_candidates)

        skipped: List[Dict[str, Any]] = []

        # Try top-K first, starting from deterministic index
        for off in range(len(top_candidates)):
            idx = (start_idx + off) % len(top_candidates)
            selected_score, selected_model = top_candidates[idx]
            resolved_url = self._safe_resolve_primary(selected_model)
            if not resolved_url:
                skipped.append({"model_code": selected_model.model_code, "reason": "missing_or_unresolvable_primary"})
                continue
            primary = self._primary_asset(selected_model)
            return {
                "model_code": selected_model.model_code,
                "primary_asset_url": resolved_url,
                "primary_asset_role": primary.role,
                "gender": selected_model.gender,
                "framing": selected_model.framing,
                "pose": selected_model.pose,
                "allowed_garment_kinds": selected_model.allowed_garment_kinds,
                "preferred_garment_kinds": selected_model.preferred_garment_kinds,
                "quality_score": selected_model.quality_score,
                "rank_score": selected_score,
                "eligible_count": len(eligible),
                "top_k_count": len(top_candidates),
                "selected_index_within_top_k": idx,
                "manifest_uri": self.manifest_uri,
                "scan_prefix": self.scan_prefix,
                "skipped_candidates": skipped[:10],
                "top_candidates": [
                    {
                        "model_code": m.model_code,
                        "rank_score": score,
                    }
                    for score, m in top_candidates
                ],
                "model": {
                    "model_code": selected_model.model_code,
                    "gender": selected_model.gender,
                    "framing": selected_model.framing,
                    "pose": selected_model.pose,
                    "age_band": selected_model.age_band,
                    "region": selected_model.region,
                    "body_type": selected_model.body_type,
                    "skin_tone": selected_model.skin_tone,
                    "style_tags": selected_model.style_tags,
                    "quality_score": selected_model.quality_score,
                    "is_active": selected_model.is_active,
                    "allowed_garment_kinds": selected_model.allowed_garment_kinds,
                    "preferred_garment_kinds": selected_model.preferred_garment_kinds,
                    "qc": selected_model.qc,
                    "meta": selected_model.meta,
                    "assets": [
                        {
                            "role": a.role,
                            "url": self._resolve_asset_url(a.url) if self._asset_exists(a.url) else a.url,
                            "width": a.width,
                            "height": a.height,
                        }
                        for a in selected_model.assets
                    ],
                },
            }

        # If top-K all stale, try the rest
        for selected_score, selected_model in ranked[effective_top_k:]:
            resolved_url = self._safe_resolve_primary(selected_model)
            if not resolved_url:
                skipped.append({"model_code": selected_model.model_code, "reason": "missing_or_unresolvable_primary"})
                continue
            primary = self._primary_asset(selected_model)
            return {
                "model_code": selected_model.model_code,
                "primary_asset_url": resolved_url,
                "primary_asset_role": primary.role,
                "gender": selected_model.gender,
                "framing": selected_model.framing,
                "pose": selected_model.pose,
                "allowed_garment_kinds": selected_model.allowed_garment_kinds,
                "preferred_garment_kinds": selected_model.preferred_garment_kinds,
                "quality_score": selected_model.quality_score,
                "rank_score": selected_score,
                "eligible_count": len(eligible),
                "top_k_count": len(top_candidates),
                "selected_index_within_top_k": None,
                "manifest_uri": self.manifest_uri,
                "scan_prefix": self.scan_prefix,
                "skipped_candidates": skipped[:20],
                "top_candidates": [
                    {
                        "model_code": m.model_code,
                        "rank_score": score,
                    }
                    for score, m in top_candidates
                ],
                "model": {
                    "model_code": selected_model.model_code,
                    "gender": selected_model.gender,
                    "framing": selected_model.framing,
                    "pose": selected_model.pose,
                    "age_band": selected_model.age_band,
                    "region": selected_model.region,
                    "body_type": selected_model.body_type,
                    "skin_tone": selected_model.skin_tone,
                    "style_tags": selected_model.style_tags,
                    "quality_score": selected_model.quality_score,
                    "is_active": selected_model.is_active,
                    "allowed_garment_kinds": selected_model.allowed_garment_kinds,
                    "preferred_garment_kinds": selected_model.preferred_garment_kinds,
                    "qc": selected_model.qc,
                    "meta": selected_model.meta,
                    "assets": [
                        {
                            "role": a.role,
                            "url": self._resolve_asset_url(a.url) if self._asset_exists(a.url) else a.url,
                            "width": a.width,
                            "height": a.height,
                        }
                        for a in selected_model.assets
                    ],
                },
            }

        raise ValueError(
            f"No usable platform models found for garment_kind={garment_kind}; "
            f"eligible={len(eligible)} but all selected assets were missing or unresolvable"
        )


_selector_singleton: Optional[PlatformModelSelector] = None
_selector_lock = threading.Lock()


def get_platform_model_selector(
    *,
    manifest_uri: Optional[str] = None,
    asset_url_resolver: Optional[Callable[[str], str]] = None,
) -> PlatformModelSelector:
    global _selector_singleton
    with _selector_lock:
        if _selector_singleton is None:
            _selector_singleton = PlatformModelSelector(
                manifest_uri=manifest_uri,
                asset_url_resolver=asset_url_resolver,
            )
        return _selector_singleton