"""Pure fail-closed guards for recovery of an already-stitched V3 scene.

No Pricing/ledger mutations occur in this module. Keep these checks separately
testable so worker crashes and API retries cannot weaken the recovery contract.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


_SUCCESS = frozenset({"succeeded", "completed", "complete", "ready"})


def finished_children_match(
    children: Sequence[Mapping[str, Any]],
    expected_count: int,
) -> bool:
    if not children or len(children) != expected_count or expected_count <= 0:
        return False
    turns: set[str] = set()
    for child in children:
        turn_id = str(child.get("dialogue_turn_id") or "").strip()
        if not turn_id or turn_id in turns:
            return False
        turns.add(turn_id)
        if str(child.get("status") or "").strip().lower() not in _SUCCESS:
            return False
        if not str(child.get("video_url") or "").strip():
            return False
    return True


def scene_debit_replay_allowed(
    debit_keys: Sequence[str],
    *,
    recovery_started: bool,
    expected_key: str,
) -> bool:
    """No debit, or exactly one recognized debit from this recovery cycle."""
    keys = [str(key).strip() for key in debit_keys]
    if not keys:
        return True
    return bool(
        recovery_started
        and len(keys) == 1
        and expected_key
        and keys[0] == expected_key
    )


def confirmed_review_checkpoint(
    completed: Mapping[str, Any] | None,
    *,
    media_id: str,
) -> bool:
    if completed is None or not media_id:
        return False
    return bool(
        str(completed.get("stage_state") or "") in {"awaiting_review", "approved"}
        and str(completed.get("pricing_state") or "") == "committed"
        and str(completed.get("attempt_state") or "") == "succeeded"
        and str(completed.get("output_media_id") or "") == media_id
        and str(completed.get("attempt_media_id") or "") == media_id
    )
