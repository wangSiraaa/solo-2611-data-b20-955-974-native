"""Shared test fixtures: build services from the bundled materials or synthetic docs."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "707"
sys.path.insert(0, str(ROOT))

from address_delivery import AddressService  # noqa: E402


def load_json(name: str) -> dict:
    return json.loads((MATERIALS / name).read_text(encoding="utf-8"))


class MaterialServiceCase(unittest.TestCase):
    """Base case: a fresh in-memory service loaded with the bundled r1 data."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = AddressService(MATERIALS and (Path(self.tmp.name) / "svc.db"))
        self.svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        self.addCleanup(self.svc.close)

    def apply_revision(self) -> int:
        return self.svc.apply_revision(load_json("revision.json"))


def fresh_synthetic_service(tmpdir: Path | str | None = None) -> AddressService:
    """A separate service loaded with the synthetic catalog (no r1 data)."""
    if tmpdir is None:
        svc = AddressService(":memory:")
    else:
        svc = AddressService(Path(tmpdir) / "synthetic.db")
    catalog, coverage = synthetic_docs()
    svc.catalog.load_initial(catalog, coverage)
    return svc


def synthetic_docs() -> tuple[dict, dict]:
    """A compact catalog exercising cross-district names and parity endpoints."""
    catalog = {
        "revision": "x1",
        "cities": [
            {"city_id": "C1", "name": "甲市"},
            {"city_id": "C2", "name": "乙市"},
        ],
        "districts": [
            {"district_id": "D1", "city_id": "C1", "name": "北区", "aliases": ["北", "同名"]},
            {"district_id": "D2", "city_id": "C1", "name": "南区", "aliases": ["南", "同名"]},
            {"district_id": "D9", "city_id": "C2", "name": "北区", "aliases": ["北"]},
        ],
        "streets": [
            {"street_id": "S1", "district_id": "D1", "name": "中心路", "aliases": [], "active": True},
            {"street_id": "S2", "district_id": "D2", "name": "中心路", "aliases": [], "active": True},
            {"street_id": "S3", "district_id": "D9", "name": "中心路", "aliases": [], "active": True},
            {"street_id": "S4", "district_id": "D1", "name": "公园路", "aliases": ["园 路"], "active": True},
        ],
    }
    coverage = {
        "revision": "x1",
        "coverages": [
            {"coverage_id": "E1", "street_id": "S4", "station_id": "P", "lo": 2, "hi": 10, "parity": "even"},
            {"coverage_id": "E2", "street_id": "S4", "station_id": "Q", "lo": 1, "hi": 9, "parity": "odd"},
        ],
    }
    return catalog, coverage


def planner_docs() -> tuple[dict, dict]:
    """Worked example: the street 杉 exists east (东/新城) and west (西)."""
    catalog = {
        "revision": "p1",
        "cities": [{"city_id": "C1", "name": "临川市"}],
        "districts": [
            {"district_id": "DE", "city_id": "C1", "name": "新城", "aliases": ["东"]},
            {"district_id": "DW", "city_id": "C1", "name": "西区", "aliases": ["西"]},
        ],
        "streets": [
            {"street_id": "SE", "district_id": "DE", "name": "杉", "aliases": [], "active": True},
            {"street_id": "SW", "district_id": "DW", "name": "杉", "aliases": [], "active": True},
        ],
    }
    coverage = {
        "revision": "p1",
        "coverages": [
            {"coverage_id": "KE", "street_id": "SE", "station_id": "A", "lo": 1, "hi": 100, "parity": "any"},
            {"coverage_id": "KW", "street_id": "SW", "station_id": "B", "lo": 1, "hi": 100, "parity": "any"},
        ],
    }
    return catalog, coverage


def fresh_planner_catalog():
    """A standalone Catalog loaded with :func:`planner_docs`."""
    from address_delivery.catalog import Catalog
    from address_delivery.db import connect

    catalog = Catalog(connect(":memory:"))
    catalog.load_initial(*planner_docs())
    return catalog
