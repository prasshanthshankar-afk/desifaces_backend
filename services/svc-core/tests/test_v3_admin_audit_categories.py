from app.routes.admin_audit import _audit_category, _audit_outcome, _redact_payload, _safe_target


def test_audit_category_taxonomy():
    assert _audit_category("auth.login.success", "session") == "sessions"
    assert _audit_category("admin.role.grant", "user_role") == "access"
    assert _audit_category("admin.user.update", "user") == "users"
    assert _audit_category("credits.reservation.created", "credit") == "billing"
    assert _audit_category("face.job.failed", "job") == "jobs"
    assert _audit_category("director.workflow.failed", "workflow") == "workflows"
    assert _audit_category("media.asset.refreshed", "media_asset") == "media"
    assert _audit_category("provider.timeout", "provider") == "providers"
    assert _audit_category("assistant.health", "assistant") == "assistant"
    assert _audit_category("support.request.created", "support_request") == "support"
    assert _audit_category("developer.api_key.created", "api_key") == "developer"
    assert _audit_category("system.deploy.completed", "deployment") == "system"


def test_audit_outcome_mapping():
    assert _audit_outcome("auth.login.failed") == "failed"
    assert _audit_outcome("workflow.retry.requested") == "pending"
    assert _audit_outcome("admin.role.grant") == "success"


def test_audit_redaction_never_returns_secrets():
    payload = {
        "access_token": "abc",
        "nested": {
            "password_hash": "hash",
            "safe": "value",
            "authorization": "Bearer abc",
        },
        "items": [{"refresh_token": "def", "status": "ok"}],
    }
    safe = _redact_payload(payload)
    assert safe["access_token"] == "[REDACTED]"
    assert safe["nested"]["password_hash"] == "[REDACTED]"
    assert safe["nested"]["authorization"] == "[REDACTED]"
    assert safe["nested"]["safe"] == "value"
    assert safe["items"][0]["refresh_token"] == "[REDACTED]"
    assert safe["items"][0]["status"] == "ok"


def test_session_targets_are_not_exposed():
    assert _safe_target("session", "refresh-token-hash") == "session:redacted"
    assert _safe_target("auth", "user@example.com") == "auth:redacted"
    assert _safe_target("user", "123") == "user:123"
