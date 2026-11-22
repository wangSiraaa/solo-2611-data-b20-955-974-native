"""Address catalog and delivery jobs (synthetic addresses only).

Two independent stores share a SQLite backend:

* :mod:`address_delivery.catalog` - cities/districts/streets, scoped names and
  address resolution through the complete parent chain, plus coverages.
* :mod:`address_delivery.delivery` - parcel delivery attempts and frozen jobs.

On top of the committed jobs sit two opt-in extensions:

* :mod:`address_delivery.readers` - synthetic optical readers with a small
  equal-sized confusion group, proof-based calibration (one bijection per
  machine) and contradiction withdrawal of the reading credential.
* :mod:`address_delivery.labels` - page/row label layouts, shared district
  headers, literal "bright" press cells (impressions), semantic validation
  against every receiving counter, and the cells/impressions planners.

:class:`address_delivery.service.AddressService` wires the core stores together.
"""
from __future__ import annotations

from .catalog import Catalog, CoverageHit, Resolution, StreetCandidate
from .delivery import AttemptResult, BatchResult, DeliveryStore
from .errors import CatalogError, DeliveryError, InputFormatError, LayoutError, ReaderContradiction, ReaderError
from .input import (
    address_input_document,
    parse_address_input_document,
    parse_csv_text,
    read_address_input_json,
    read_csv_addresses,
)
from .labels import (
    PagePlan,
    PlanJob,
    Recipient,
    StableAddress,
    diagnose,
    grouped_serialize,
    plan_deck,
    plan_min_impressions,
    resolve_row,
    serialize_page,
)
from .models import AddressInput, validate_address_input
from .normalization import make_identity, normalize_text
from .readers import Observation, Proof, Reader, reader_from_json
from .service import AddressService

__all__ = [
    "AddressInput",
    "AddressService",
    "AttemptResult",
    "BatchResult",
    "Catalog",
    "CatalogError",
    "CoverageHit",
    "DeliveryError",
    "DeliveryStore",
    "InputFormatError",
    "LayoutError",
    "Observation",
    "PagePlan",
    "PlanJob",
    "Proof",
    "Reader",
    "ReaderContradiction",
    "ReaderError",
    "Recipient",
    "Resolution",
    "StableAddress",
    "StreetCandidate",
    "address_input_document",
    "diagnose",
    "grouped_serialize",
    "make_identity",
    "normalize_text",
    "parse_address_input_document",
    "parse_csv_text",
    "plan_deck",
    "plan_min_impressions",
    "read_address_input_json",
    "read_csv_addresses",
    "reader_from_json",
    "resolve_row",
    "serialize_page",
    "validate_address_input",
]

__version__ = "1.0.0"
