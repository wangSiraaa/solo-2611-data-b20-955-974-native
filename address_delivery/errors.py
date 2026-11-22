"""Public exception types.

``InputFormatError`` is *structural*: a bad CSV/document is rejected before any
delivery attempt is made.  Business failures (unknown address, ambiguity, an
inactive street, no/ambiguous station) are never exceptions - they are returned
as structured row results by the delivery service.
"""
from __future__ import annotations


class InputFormatError(ValueError):
    """The input representation (CSV, address-input/v1 document) is malformed."""


class CatalogError(RuntimeError):
    """The catalog is not initialised or a catalog revision is invalid."""


class DeliveryError(RuntimeError):
    """An internal delivery-store invariant was violated."""


class RenumberingError(RuntimeError):
    """A renumbering publication, trace or issue request is invalid.

    ``code`` classifies the failure and ``conflicts`` carries the reviewable
    doors/strips involved (empty when not applicable).
    """

    def __init__(self, message: str, *, code: str = "renumbering_invalid", conflicts=None) -> None:
        super().__init__(message)
        self.code = code
        self.conflicts = conflicts or []


class CabinetError(RuntimeError):
    """A cabinet configuration or teller operation violates the rules."""
