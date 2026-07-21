from ego.redaction import redact_sensitive_text


def test_redacts_environment_secrets_and_known_token_shapes(monkeypatch: object) -> None:
    import os

    os.environ["EGO_TEST_SECRET_TOKEN"] = "super-secret-value"
    try:
        value = redact_sensitive_text(
            'token=super-secret-value api_key="sk_abcdefghijklmnopqrstuvwxyz"'
        )
    finally:
        os.environ.pop("EGO_TEST_SECRET_TOKEN", None)
    assert "super-secret-value" not in value
    assert "sk_abcdefghijklmnopqrstuvwxyz" not in value
    assert "***REDACTED***" in value
