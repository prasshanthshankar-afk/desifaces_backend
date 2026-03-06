# services/svc-marketing/app/app/services/planning/evidence_retriever.py
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("svc-marketing-evidence")


def _s(x: Any) -> str:
    return str(x or "").strip()


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            y = json.loads(s)
            return y if isinstance(y, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _tokenize(s: str) -> List[str]:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    parts = [p.strip() for p in s.split() if p.strip()]
    # small normalization
    out: List[str] = []
    for p in parts:
        if p in ("the", "and", "for", "with", "from", "into", "that", "this", "you", "your", "are"):
            continue
        if len(p) <= 2:
            continue
        out.append(p)
    return out


@dataclass
class EvidenceItem:
    id: str
    tags: List[str]
    bullets: List[str]
    sources: List[str]


class EvidenceRetriever:
    """
    Lightweight RAG-style retriever.

    Optional KB file (JSON) can be provided via env:
      - MARKETING_EVIDENCE_KB_PATH=/path/to/evidence_kb.json

    KB schema (recommended):
      [
        {
          "id": "creator_consistency",
          "tags": ["creator", "instagram", "consistency", "short-form"],
          "bullets": ["...", "..."],
          "sources": ["internal:desifaces/notes", "public:... (optional)"]
        }
      ]

    If KB not present, returns safe deterministic bullets (no invented stats).
    """

    def __init__(self, kb_path: Optional[str] = None) -> None:
        self.kb_path = (kb_path or os.getenv("MARKETING_EVIDENCE_KB_PATH") or "").strip()
        self._items: List[EvidenceItem] = []
        self._loaded = False

    def _load_kb(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not self.kb_path:
            return
        if not os.path.exists(self.kb_path):
            logger.warning("evidence kb path does not exist: %s", self.kb_path)
            return

        try:
            raw = json.load(open(self.kb_path, "r", encoding="utf-8"))
            items: List[EvidenceItem] = []
            for obj in _as_list(raw):
                d = _as_dict(obj)
                item = EvidenceItem(
                    id=_s(d.get("id")) or "kb_item",
                    tags=[_s(x) for x in _as_list(d.get("tags")) if _s(x)],
                    bullets=[_s(x) for x in _as_list(d.get("bullets")) if _s(x)],
                    sources=[_s(x) for x in _as_list(d.get("sources")) if _s(x)],
                )
                if item.bullets:
                    items.append(item)
            self._items = items
            logger.info("evidence kb loaded items=%s path=%s", len(self._items), self.kb_path)
        except Exception as e:
            logger.warning("failed to load evidence kb: %s", str(e))
            self._items = []

    def _fallback_bullets(
        self,
        persona: str,
        industry: str,
        season_event: str,
        offer: str,
        language_hint: str,
    ) -> Tuple[List[str], List[str]]:
        # IMPORTANT: do not claim numbers/metrics; keep general + truthful.
        p = (persona or "creator").lower()
        ind = industry or "creator"
        season = season_event or ""
        off = offer or ""
        lang = language_hint or "en"

        bullets = [
            "Most creators and small businesses struggle to post consistently because production (script, shoot, edit) takes too long.",
            "Short-form content works best when the hook is immediate and the story feels real—not like a scripted ad.",
            "Consistency matters more than perfection; fast turnaround helps people keep posting regularly.",
            "People respond better when content matches their language and context (local vibe, relatable setting).",
            "A single person often has to do everything: idea → script → camera → edit → publish; reducing steps improves output.",
        ]

        # Slightly tailor a couple bullets to persona
        if "college" in p:
            bullets.insert(0, "Students often want to post but avoid the effort (retakes, editing, camera shyness).")
        if "business" in p or "smb" in p:
            bullets.insert(0, "Small business owners need promo content but can’t pause operations to shoot videos daily.")

        # Gentle mention of DesiFaces benefits as general capability (not metrics)
        bullets.append("DesiFaces.ai can help by generating a consistent on-camera host (face), voice in the right locale, and a talking video quickly—ready to post.")
        if season.strip():
            bullets.append(f"Seasonal hooks work best when shown through a real moment (e.g., {season}), not as a loud offer.")
        if off.strip():
            bullets.append("Offers work best when the story shows the value first, then reveals the offer as a natural next step.")

        sources: List[str] = []
        # No fake citations; if you later add a KB file you can include real sources there.
        return bullets[:10], sources

    def retrieve(
        self,
        *,
        persona: str,
        industry: str,
        season_event: str = "",
        offer: str = "",
        language_hint: str = "",
        k: int = 8,
    ) -> Dict[str, Any]:
        self._load_kb()

        query = " ".join([persona, industry, season_event, offer, language_hint]).strip()
        qtokens = set(_tokenize(query))

        if not self._items or not qtokens:
            bullets, sources = self._fallback_bullets(persona, industry, season_event, offer, language_hint)
            return {"bullets": bullets[:k], "sources": sources}

        scored: List[Tuple[int, EvidenceItem]] = []
        for it in self._items:
            tags = set(_tokenize(" ".join(it.tags)))
            overlap = len(qtokens.intersection(tags))
            scored.append((overlap, it))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [it for score, it in scored if score > 0][: max(1, min(12, k))]

        bullets_out: List[str] = []
        sources_out: List[str] = []
        seen_b = set()
        for it in top:
            for b in it.bullets:
                bb = _s(b)
                if not bb or bb in seen_b:
                    continue
                bullets_out.append(bb)
                seen_b.add(bb)
                if len(bullets_out) >= k:
                    break
            for s in it.sources:
                ss = _s(s)
                if ss and ss not in sources_out:
                    sources_out.append(ss)
            if len(bullets_out) >= k:
                break

        if not bullets_out:
            bullets_out, sources_out = self._fallback_bullets(persona, industry, season_event, offer, language_hint)

        return {"bullets": bullets_out[:k], "sources": sources_out}