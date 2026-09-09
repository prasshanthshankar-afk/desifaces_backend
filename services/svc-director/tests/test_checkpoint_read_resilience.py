from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
main = (ROOT / "services/svc-director/app/app/main.py").read_text()


def test_get_run_retries_only_transient_checkpoint_database_failures():
    assert 'from psycopg import Error as PsycopgError' in main
    assert '_TRANSIENT_CHECKPOINT_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})' in main
    assert 'sqlstate.startswith("08")' in main
    assert 'async def _aget_state_resilient' in main
    assert main.count('return await graph.aget_state(config)') == 2
    assert 'snapshot = await _aget_state_resilient(graph, config)' in main


def test_retry_failure_is_controlled_and_does_not_mask_non_transient_errors():
    helper = main.split('async def _aget_state_resilient', 1)[1].split('@asynccontextmanager', 1)[0]
    assert helper.count('if not _is_transient_checkpoint_error(exc):\n            raise') == 2
    assert 'status.HTTP_503_SERVICE_UNAVAILABLE' in helper
    assert 'creative_director_state_temporarily_unavailable' in helper
    assert 'await asyncio.sleep(0.05)' in helper
