from __future__ import annotations

import inspect

from app import fusion_resilience_routes
from app.fusion_execution_parent_pricing import ParentPricedSceneFusionExecutionService


def test_retry_stitch_route_is_zero_child_dispatch_and_zero_child_charge_contract():
    source = inspect.getsource(fusion_resilience_routes.retry_scene_stitch)

    assert 'required_children != 0' in source
    assert 'child_confirmations=[]' in source
    assert '"retry_scope": "stitch_only"' in source
    assert '"new_child_charges": 0' in source
    assert '"new_child_dispatches": 0' in source


def test_parent_priced_dispatch_has_explicit_stitch_only_retry_branch():
    source = inspect.getsource(ParentPricedSceneFusionExecutionService.dispatch)

    stitch_only = source.split('if not expected_turns:', 1)
    assert len(stitch_only) == 2
    branch = stitch_only[1].split('children = await _compile_children(', 1)[0]

    assert '"dispatch_outcome": "stitch_only_retry"' in branch
    assert '_create_internal_child' not in branch
    assert 'create_job' not in branch
