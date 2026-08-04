"""URL-safe PostgreSQL password policy for Compose DATABASE_URL interpolation."""

from __future__ import annotations

from . import MIN_PASSWORD_LENGTH, PASSWORD_ALLOWED


class PasswordError(ValueError):
    """Invalid staging database password."""


_EXACT_FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        "password",
        "changeme",
        "secret",
        "replace-with-unique-staging-password",
        "replace-with-32plus-url-safe-staging-password",
    }
)


def validate_staging_password(password: str) -> None:
    if password is None:
        raise PasswordError("POSTGRES_PASSWORD is empty")
    if password in _EXACT_FORBIDDEN:
        raise PasswordError("POSTGRES_PASSWORD must not use a default/example value")
    lower = password.lower()
    if "replace-with" in lower or "changeme" in lower or "example" in lower:
        raise PasswordError("POSTGRES_PASSWORD looks like an example/placeholder")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"POSTGRES_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if any(ch not in PASSWORD_ALLOWED for ch in password):
        raise PasswordError(
            "POSTGRES_PASSWORD contains characters unsafe for raw DATABASE_URL "
            "interpolation; allowed: A-Z a-z 0-9 . _ ~ -"
        )
