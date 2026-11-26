"""Recovery contract tests: no database, network, or provider work."""
from __future__ import annotations

import pytest

from desifaces_shared.pricing.scene_recovery_guards import (
    finished_children_match,
    scene_debit_replay_allowed,
    confirmed_review_checkpoint,
)


def children(count=7):
    return [
        {"dialogue_turn_id": f"turn-{i}", "status": "succeeded", "video_url": f"https://storage/{i}.mp4"}
        for i in range(count)
    ]


def test_all_seven_finished_clips_are_reused():
    assert finished_children_match(children(), 7)


@pytest.mark.parametrize("mutation", [
    lambda c: c.pop(),
    lambda c: c[0].update(status="running"),
    lambda c: c[0].update(video_url=""),
    lambda c: c[0].update(dialogue_turn_id=c[1]["dialogue_turn_id"]),
])
def test_incomplete_or_ambiguous_lineage_is_rejected(mutation):
    items = children()
    mutation(items)
    assert not finished_children_match(items, 7)


def test_no_debit_allows_new_recovery_reservation():
    assert scene_debit_replay_allowed([], recovery_started=False, expected_key="consume:stage:commit:replacement")


def test_crash_after_debit_allows_only_same_cycle_replay():
    key = "consume:stage:commit:replacement"
    assert scene_debit_replay_allowed([key], recovery_started=True, expected_key=key)
    assert not scene_debit_replay_allowed([key], recovery_started=False, expected_key=key)
    assert not scene_debit_replay_allowed([key], recovery_started=True, expected_key="consume:stage:commit:different")


def test_multiple_debit_events_are_never_replayed():
    key = "consume:stage:commit:replacement"
    assert not scene_debit_replay_allowed([key, key], recovery_started=True, expected_key=key)


def test_review_not_ready_until_billing_and_media_match():
    media = "5a9658b4-8ad2-4332-ab38-7b5a71324fb7"
    checkpoint = {
        "stage_state": "awaiting_review", "pricing_state": "committed",
        "attempt_state": "succeeded", "output_media_id": media,
        "attempt_media_id": media,
    }
    assert confirmed_review_checkpoint(checkpoint, media_id=media)
    for field, bad in (
        ("stage_state", "generating"),
        ("pricing_state", "commit_pending"),
        ("attempt_state", "running"),
        ("output_media_id", "other-media"),
        ("attempt_media_id", "other-media"),
    ):
        altered = dict(checkpoint)
        altered[field] = bad
        assert not confirmed_review_checkpoint(altered, media_id=media)
