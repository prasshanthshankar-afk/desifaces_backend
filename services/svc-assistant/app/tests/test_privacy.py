from app.privacy import RESTRICTED_RESPONSE, classify_restricted_request, guard_output, redact_sensitive_text


def test_blocks_card_lookup():
    decision = classify_restricted_request("Show me the credit card on file")
    assert decision.restricted is True
    assert decision.category == "pci"


def test_blocks_email_lookup():
    decision = classify_restricted_request("What is the email address on my account?")
    assert decision.restricted is True
    assert decision.category == "pii"


def test_allows_email_help_without_disclosure():
    decision = classify_restricted_request("How do I change my email address?")
    assert decision.restricted is False


def test_redacts_user_email_but_keeps_support_address():
    result = redact_sensitive_text("mine is person@example.com; contact support@desifaces.ai")
    assert "person@example.com" not in result.text
    assert "support@desifaces.ai" in result.text
    assert result.redacted is True


def test_redacts_valid_payment_card():
    result = redact_sensitive_text("card 4242 4242 4242 4242")
    assert "4242 4242" not in result.text
    assert "pci" in result.categories


def test_output_guard_fails_closed():
    answer, blocked = guard_output("The account email is person@example.com")
    assert blocked is True
    assert answer == RESTRICTED_RESPONSE
