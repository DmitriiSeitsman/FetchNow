"""Fail-closed Robokassa A1 settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings


def _test_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ROBOKASSA_MODE": "test",
        "ROBOKASSA_MERCHANT_LOGIN": "demo",
        "ROBOKASSA_SIGNATURE_ALGORITHM": "sha256",
        "ROBOKASSA_TEST_PASSWORD1": "test-password-one",
        "ROBOKASSA_TEST_PASSWORD2": "test-password-two",
        "ROBOKASSA_TEST_AMOUNT_MINOR": 100,
        "ROBOKASSA_RECEIPT_TAX": "none",
        "ROBOKASSA_RECEIPT_PAYMENT_METHOD": "full_payment",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_disabled_mode_starts_without_credentials() -> None:
    settings = Settings()
    assert settings.robokassa_mode == "disabled"
    assert settings.robokassa_test_amount_minor == 0
    assert settings.robokassa_test_password1.get_secret_value() == ""
    assert settings.premium_test_checkout_visible is False
    assert "robokassa_test_password1" not in repr(settings)


def test_checkout_visibility_boolean_is_strict_and_independent_of_mode() -> None:
    settings = _test_settings(PREMIUM_TEST_CHECKOUT_VISIBLE=True)
    assert settings.premium_test_checkout_visible
    with pytest.raises(ValidationError):
        _test_settings(PREMIUM_TEST_CHECKOUT_VISIBLE="sometimes")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ROBOKASSA_MERCHANT_LOGIN", ""),
        ("ROBOKASSA_TEST_PASSWORD1", ""),
        ("ROBOKASSA_TEST_PASSWORD2", ""),
        ("ROBOKASSA_TEST_AMOUNT_MINOR", 0),
        ("ROBOKASSA_TEST_AMOUNT_MINOR", -1),
        ("ROBOKASSA_RECEIPT_TAX", ""),
        ("ROBOKASSA_RECEIPT_PAYMENT_METHOD", ""),
    ],
)
def test_test_mode_requires_complete_deploy_time_profile(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _test_settings(**{field: value})


@pytest.mark.parametrize("mode", ["live", "production", "enabled"])
def test_live_or_unknown_mode_is_impossible(mode: str) -> None:
    with pytest.raises(ValidationError):
        _test_settings(ROBOKASSA_MODE=mode)


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha512", "HS256"])
def test_unsupported_classic_hash_is_rejected(algorithm: str) -> None:
    with pytest.raises(ValidationError):
        _test_settings(ROBOKASSA_SIGNATURE_ALGORITHM=algorithm)


def test_test_mode_accepts_only_reviewed_sha256_profile() -> None:
    settings = _test_settings(
        ROBOKASSA_MERCHANT_LOGIN=" demo ",
        ROBOKASSA_SIGNATURE_ALGORITHM="SHA256",
    )
    assert settings.robokassa_mode == "test"
    assert settings.robokassa_signature_algorithm == "sha256"
    assert settings.robokassa_merchant_login == "demo"
    assert settings.robokassa_test_amount_minor == 100


def test_confirmed_fetchnow_test_merchant_profile() -> None:
    """Operator-confirmed cabinet: MerchantLogin fetchnow, SHA256, 100 kopecks."""
    settings = _test_settings(
        ROBOKASSA_MERCHANT_LOGIN="fetchnow",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_AMOUNT_MINOR=100,
    )
    assert settings.robokassa_merchant_login == "fetchnow"
    assert settings.robokassa_signature_algorithm == "sha256"
    assert settings.robokassa_test_amount_minor == 100


def test_secrets_are_not_runtime_config_contracts() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "fetchnow_release"
        / "config_contract.py"
    ).read_text(encoding="utf-8")
    assert "ROBOKASSA_TEST_PASSWORD1" not in source
    assert "ROBOKASSA_TEST_PASSWORD2" not in source
    assert "ROBOKASSA_MODE" not in source


def test_invalid_secret_is_redacted_from_validation_error() -> None:
    secret = "must-never-appear\n"
    with pytest.raises(ValidationError) as caught:
        _test_settings(ROBOKASSA_TEST_PASSWORD1=secret)
    assert secret.strip() not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ROBOKASSA_TEST_PASSWORD1", "'secret'"),
        ("ROBOKASSA_TEST_PASSWORD1", '"secret"'),
        ("ROBOKASSA_TEST_PASSWORD2", "'secret-two'"),
        ("ROBOKASSA_TEST_PASSWORD2", '"secret-two"'),
    ],
)
def test_wrapped_quotes_in_robokassa_passwords_are_rejected(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match="unquoted"):
        _test_settings(**{field: value})


def test_unquoted_robokassa_passwords_are_accepted() -> None:
    settings = _test_settings(
        ROBOKASSA_TEST_PASSWORD1="plain-secret-one",
        ROBOKASSA_TEST_PASSWORD2="plain-secret-two",
    )
    assert settings.robokassa_test_password1.get_secret_value() == "plain-secret-one"
    assert settings.robokassa_test_password2.get_secret_value() == "plain-secret-two"
