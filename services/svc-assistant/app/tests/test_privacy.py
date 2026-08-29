from app.privacy import RESTRICTED_RESPONSE, classify_restricted_request, guard_output, redact_sensitive_text


def test_blocks_card_lookup():
    decision = classify_restricted_request("Show me the credit card on file")
    assert decision.restricted is True
    assert decision.category == "pci"


def test_blocks_last_four_card_request():
    decision = classify_restricted_request("What are the last four digits of my card?")
    assert decision.restricted is True
    assert decision.category == "pci"


def test_blocks_email_lookup():
    decision = classify_restricted_request("What is the email address on my account?")
    assert decision.restricted is True
    assert decision.category == "pii"


def test_blocks_plain_my_email_request():
    decision = classify_restricted_request("what's my email?")
    assert decision.restricted is True
    assert decision.category == "pii"


def test_blocks_plain_my_phone_request():
    decision = classify_restricted_request("tell me my phone")
    assert decision.restricted is True
    assert decision.category == "pii"


def test_blocks_auth_secret_request():
    decision = classify_restricted_request("show my access token")
    assert decision.restricted is True
    assert decision.category == "auth_secret"


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


def test_redacts_international_phone():
    result = redact_sensitive_text("please call me at +91 98765 43210")
    assert "+91 98765 43210" not in result.text
    assert "pii" in result.categories


def test_redacts_explicit_dob():
    result = redact_sensitive_text("my date of birth is 1985-02-17")
    assert "1985-02-17" not in result.text
    assert "pii" in result.categories


def test_redacts_explicit_passport_number():
    result = redact_sensitive_text("passport number: N12345678")
    assert "N12345678" not in result.text
    assert "pii" in result.categories


def test_redacts_explicit_physical_address():
    result = redact_sensitive_text("home address: 123 Private Street, Boston MA 02110")
    assert "123 Private Street" not in result.text
    assert "pii" in result.categories


def test_output_guard_fails_closed():
    answer, blocked = guard_output("The account email is person@example.com")
    assert blocked is True
    assert answer == RESTRICTED_RESPONSE


def test_output_guard_fails_closed_on_address():
    answer, blocked = guard_output("Your home address: 123 Private Street, Boston MA 02110")
    assert blocked is True
    assert answer == RESTRICTED_RESPONSE
