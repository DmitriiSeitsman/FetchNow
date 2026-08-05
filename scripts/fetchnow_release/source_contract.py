"""Versioned release source contracts (PRD1C2 legacy + PRD1C3B1 policy)."""

from __future__ import annotations

from .c2_constants import (
    CURRENT_PREPARE_SOURCE_CONTRACT_VERSION,
    DEPLOY_PLAN_MIN_SOURCE_CONTRACT_VERSION,
    REQUIRED_SOURCE_FILES_V1,
    REQUIRED_SOURCE_FILES_V2,
    SOURCE_CONTRACT_VERSION_V1,
    SOURCE_CONTRACT_VERSION_V2,
    SUPPORTED_SOURCE_CONTRACT_VERSIONS,
)


def required_source_files_for_version(source_contract_version: int) -> tuple[str, ...]:
    """Return the pinned required-source path list for a contract version."""
    if source_contract_version == SOURCE_CONTRACT_VERSION_V1:
        return REQUIRED_SOURCE_FILES_V1
    if source_contract_version == SOURCE_CONTRACT_VERSION_V2:
        return REQUIRED_SOURCE_FILES_V2
    raise ValueError(
        f"unsupported source_contract_version: {source_contract_version!r}"
    )


def assert_deploy_plan_target_contract(source_contract_version: int) -> None:
    """Deploy-plan targets must declare a supported post-B1 source contract."""
    if source_contract_version not in SUPPORTED_SOURCE_CONTRACT_VERSIONS:
        raise ValueError(
        f"unsupported source_contract_version: {source_contract_version!r}"
    )
    if source_contract_version < DEPLOY_PLAN_MIN_SOURCE_CONTRACT_VERSION:
        raise ValueError(
            "target release source_contract_version must be "
            f">= {DEPLOY_PLAN_MIN_SOURCE_CONTRACT_VERSION} for deploy-plan; "
            f"legacy v{SOURCE_CONTRACT_VERSION_V1} releases remain valid as "
            "current state but are not deploy-plan targets"
        )


def prepare_source_contract_version() -> int:
    """Source contract version emitted by new release prepare."""
    return CURRENT_PREPARE_SOURCE_CONTRACT_VERSION
