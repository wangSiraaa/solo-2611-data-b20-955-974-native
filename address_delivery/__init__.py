"""Address catalog and delivery jobs (synthetic addresses only).

Two independent stores share a SQLite backend:

* :mod:`address_delivery.catalog` - cities/districts/streets, scoped names and
  address resolution through the complete parent chain, plus coverages.
* :mod:`address_delivery.delivery` - parcel delivery attempts and frozen jobs.

:class:`address_delivery.service.AddressService` wires them together for the
combined workflow.
"""
from __future__ import annotations

from .catalog import Catalog, CoverageHit, Resolution, StreetCandidate
from .cabinet import MAX_CELLS, MAX_LETTERS, Move, MovePlan, ReturnCabinet
from .counter import POLICY_CYCLE, POLICY_STEP, CounterSession
from .delivery import AttemptResult, BatchResult, DeliveryStore
from .errors import CabinetError, CatalogError, DeliveryError, InputFormatError, RenumberingError
from .input import (
    address_input_document,
    parse_address_input_document,
    parse_csv_text,
    read_address_input_json,
    read_csv_addresses,
)
from .models import AddressInput, validate_address_input
from .normalization import make_identity, normalize_text
from .renumbering import (
    MAX_ERAS,
    Door,
    DoorTrace,
    EraTrace,
    IssuedDoor,
    RenumberingStore,
    RenumberStrip,
    apply_publication,
)
from .service import AddressService

__all__ = [
    "AddressInput",
    "AddressService",
    "AttemptResult",
    "BatchResult",
    "CabinetError",
    "Catalog",
    "CatalogError",
    "CounterSession",
    "CoverageHit",
    "DeliveryError",
    "DeliveryStore",
    "Door",
    "DoorTrace",
    "EraTrace",
    "InputFormatError",
    "MAX_CELLS",
    "MAX_ERAS",
    "MAX_LETTERS",
    "Move",
    "MovePlan",
    "POLICY_CYCLE",
    "POLICY_STEP",
    "RenumberingError",
    "RenumberingStore",
    "RenumberStrip",
    "Resolution",
    "ReturnCabinet",
    "StreetCandidate",
    "IssuedDoor",
    "address_input_document",
    "apply_publication",
    "make_identity",
    "normalize_text",
    "parse_address_input_document",
    "parse_csv_text",
    "read_address_input_json",
    "read_csv_addresses",
    "validate_address_input",
]

__version__ = "1.0.0"
