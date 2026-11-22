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


class BuildingError(ValueError):
    """A building registry definition is malformed or duplicated."""


class KeyLedgerError(RuntimeError):
    """A key-transfer operation breaks the handover state machine."""


class PlanningError(ValueError):
    """A night scenario (graph, entrances, parcels, counters) is malformed."""
