"""Synthetic staging-password fixtures for release tests (not real credentials)."""

from __future__ import annotations


def valid_test_password() -> str:
    """Return an obviously synthetic password that still meets production policy.

    Constructed from a repeated harmless character — no high-entropy literals.
    """
    return "A" * 32
