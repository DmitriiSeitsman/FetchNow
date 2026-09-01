"""Premium entitlement domain errors."""


class PremiumEntitlementError(Exception):
    """Base Premium entitlement failure."""


class PremiumEntitlementInvariantError(PremiumEntitlementError):
    """Paid-order preconditions for issuance were not met."""
