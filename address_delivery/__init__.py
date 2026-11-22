"""Address catalog and delivery jobs (synthetic addresses only).

Two independent stores share a SQLite backend:

* :mod:`address_delivery.catalog` - cities/districts/streets, scoped names and
  address resolution through the complete parent chain, plus coverages.
* :mod:`address_delivery.delivery` - parcel delivery attempts and frozen jobs.

:class:`address_delivery.service.AddressService` wires them together for the
combined workflow.
"""
from __future__ import annotations

from .buildings import (
    Building,
    BuildingRecognition,
    BuildingRegistry,
    BUILDING_NO_JOB,
    BUILDING_TWO,
    BUILDING_UNIQUE,
    BUILDING_UNMATCHED,
)
from .catalog import Catalog, CoverageHit, Resolution, StreetCandidate
from .delivery import AttemptResult, BatchResult, DeliveryStore
from .errors import (
    BuildingError,
    CatalogError,
    DeliveryError,
    InputFormatError,
    KeyLedgerError,
    PlanningError,
)
from .input import (
    address_input_document,
    parse_address_input_document,
    parse_csv_text,
    read_address_input_json,
    read_csv_addresses,
)
from .keys import (
    Counter,
    KeyLedger,
    Ownership,
    PhysicalKey,
    Reservation,
    TransferRecord,
)
from .models import AddressInput, validate_address_input
from .night import (
    Edge,
    Entrance,
    NightDesk,
    NightParcel,
    NightPlanner,
    NightScenario,
    RoutePlan,
    StreetGraph,
)
from .normalization import make_identity, normalize_text
from .service import AddressService

__all__ = [
    "AddressInput",
    "AddressService",
    "AttemptResult",
    "BatchResult",
    "Building",
    "BuildingError",
    "BuildingRecognition",
    "BuildingRegistry",
    "BUILDING_NO_JOB",
    "BUILDING_TWO",
    "BUILDING_UNIQUE",
    "BUILDING_UNMATCHED",
    "Catalog",
    "CatalogError",
    "Counter",
    "CoverageHit",
    "DeliveryError",
    "DeliveryStore",
    "Edge",
    "Entrance",
    "InputFormatError",
    "KeyLedger",
    "KeyLedgerError",
    "NightDesk",
    "NightParcel",
    "NightPlanner",
    "NightScenario",
    "Ownership",
    "PhysicalKey",
    "PlanningError",
    "Reservation",
    "Resolution",
    "RoutePlan",
    "StreetCandidate",
    "StreetGraph",
    "TransferRecord",
    "address_input_document",
    "make_identity",
    "normalize_text",
    "parse_address_input_document",
    "parse_csv_text",
    "read_address_input_json",
    "read_csv_addresses",
    "validate_address_input",
]

__version__ = "1.0.0"
