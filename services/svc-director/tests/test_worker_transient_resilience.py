from __future__ import annotations

import socket

from app.worker import _is_transient_infrastructure_error


def test_dns_failure_is_transient() -> None:
    assert _is_transient_infrastructure_error(
        socket.gaierror(-3, "Temporary failure in name resolution")
    )


def test_connection_error_is_transient() -> None:
    assert _is_transient_infrastructure_error(ConnectionError("connection refused"))


def test_non_infrastructure_error_is_not_transient() -> None:
    assert not _is_transient_infrastructure_error(ValueError("creative plan invalid"))


def test_nested_dns_failure_is_detected() -> None:
    try:
        try:
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        except socket.gaierror as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError as exc:
        assert _is_transient_infrastructure_error(exc)
