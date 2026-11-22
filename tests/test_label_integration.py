"""Label printing integration: committed-job sourcing, receipts and the CLI."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import MATERIALS, ROOT

from address_delivery.labels import (
    PlanJob,
    Recipient,
    StableAddress,
    plan_deck,
)


def _reader(reader_id, paper, output, exact=None, pinned=None):
    from address_delivery.readers import Observation, Proof, Reader

    proofs = []
    if exact is not None:
        proofs.append(Proof(exact[0], tuple(Observation.exact(g) for g in exact[1])))
    if pinned is not None:
        proofs.append(Proof(pinned[0], tuple(Observation.exact(g) for g in pinned[1])))
    return Reader(reader_id, "e-" + reader_id, paper, output, proofs)


class CommittedJobSourcingTests(unittest.TestCase):
    """The print list comes from parcels that actually committed."""

    def setUp(self):
        from address_delivery import AddressService

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = AddressService(Path(self.tmp.name) / "svc.db")
        self.svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        self.addCleanup(self.svc.close)

    def test_only_successful_jobs_contribute_stable_facts(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        rows = self.svc.delivery.stable_addresses()
        # r1 successes: P2, P5, P6. All on S3 district D1.
        self.assertEqual({r["parcel_id"] for r in rows}, {"P2", "P5", "P6"})
        for row in rows:
            self.assertEqual(row["street_id"], "S3")
            self.assertEqual(row["district_id"], "D1")
            self.assertEqual(row["house_no"], 2)
        # Ordered by house number then parcel id (all house 2 here).
        self.assertEqual([r["parcel_id"] for r in rows], ["P2", "P5", "P6"])

    def test_stable_facts_survive_revision(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        before = self.svc.delivery.stable_addresses()
        self.svc.apply_revision(MATERIALS / "revision.json")
        after = self.svc.delivery.stable_addresses()
        # Same committed parcels, same frozen stable IDs.
        self.assertEqual(before, after)

    def test_planned_page_readable_for_all_counters(self):
        self.svc.import_csv(MATERIALS / "addresses.csv")
        rows = self.svc.delivery.stable_addresses()
        jobs = [
            PlanJob(
                address=StableAddress(
                    parcel_id=r["parcel_id"],
                    city_id=r["city_id"],
                    district_id=r["district_id"],
                    street_id=r["street_id"],
                    house_no=r["house_no"],
                ),
                max_rows=20,
                max_cells=40,
            )
            for r in rows
        ]
        # Catalog here has no 东/西 confusion, so one calibrated reader suffices.
        reader = _reader("A", ["东", "西"], ["东", "西"], exact=("东西", "东西"))
        recips = [Recipient(reader, self.svc.catalog)]
        sol = plan_deck(jobs, self.svc.catalog, recips, max_impressions=8)
        self.assertIsNotNone(sol)
        payload = sol.as_dict(recips)
        # One page, shared header 东 (district D1 alias), three rows of 学园路/2.
        self.assertEqual(payload["page_count"], 1)
        page = payload["pages"][0]
        self.assertEqual(page["header_text"], "东")
        self.assertEqual(len(page["rows"]), 3)
        self.assertTrue(payload["receipts"]["A"][0]["ok"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "a.db")
        subprocess.run(
            [sys.executable, "-m", "address_delivery", "init", "--db", self.db,
             "--materials", str(MATERIALS)],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [sys.executable, "-m", "address_delivery", "import", "--db", self.db,
             str(MATERIALS / "addresses.csv")],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )

    def _run(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "address_delivery", *args],
            cwd=ROOT, input=stdin, capture_output=True, text=True,
        )

    def test_reader_calibrate_reports_tables(self):
        reader = _reader("B", ["东", "西"], ["东", "西"])
        path = Path(self.tmp.name) / "reader.json"
        path.write_text(json.dumps({
            "reader_id": "B", "version": "eB",
            "confusion": {"paper": ["东", "西"], "output": ["东", "西"]},
        }), encoding="utf-8")
        proc = self._run("reader-calibrate", "--reader", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "calibrated")
        self.assertEqual(len(payload["tables"]), 2)

    def test_reader_confusions_lists_indistinguishable_readings(self):
        path = Path(self.tmp.name) / "reader.json"
        path.write_text(json.dumps({
            "reader_id": "B", "version": "eB",
            "confusion": {"paper": ["东", "西"], "output": ["东", "西"]},
        }), encoding="utf-8")
        proc = self._run("reader-confusions", "--reader", str(path), "东西")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["readings"], ["东西", "西东"])
        self.assertEqual(payload["pairs"], [["东西", "西东"]])

    def test_diagnose_command(self):
        path = Path(self.tmp.name) / "reader.json"
        path.write_text(json.dumps({
            "reader_id": "A", "version": "eA",
            "confusion": {"paper": ["东", "西"], "output": ["东", "西"]},
            "proofs": [{"paper": "东西", "observed": [{"exact": "东"}, {"exact": "西"}]}],
        }), encoding="utf-8")
        proc = self._run(
            "diagnose", "--db", self.db, "--reader", str(path),
            "C1", "东", "学园路", "2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "unique")
        self.assertEqual(payload["possible_streets"], ["S3"])

    def test_plan_from_committed_rows(self):
        path = Path(self.tmp.name) / "brief.json"
        path.write_text(json.dumps({
            "designated_reader_id": "A",
            "readers": [
                {"reader_id": "A", "version": "eA",
                 "confusion": {"paper": ["东", "西"], "output": ["东", "西"]},
                 "proofs": [{"paper": "东西", "observed": [{"exact": "东"}, {"exact": "西"}]}]},
            ],
            "from_committed": True,
            "max_rows": 20,
            "max_cells": 40,
        }), encoding="utf-8")
        proc = self._run(
            "plan", "--db", self.db, str(path), "--max-impressions", "8"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["total_impressions"], 0)
        self.assertEqual(payload["page_count"], 1)
        self.assertTrue(payload["receipts"]["A"][0]["ok"])

    def test_plan_infeasible_returns_six(self):
        path = Path(self.tmp.name) / "brief.json"
        path.write_text(json.dumps({
            "designated_reader_id": "A",
            "readers": [
                {"reader_id": "A", "version": "eA",
                 "confusion": {"paper": ["东"], "output": ["东"]}},
            ],
            "rows": [{
                "parcel_id": "P", "city_id": "C1", "district_id": "D1",
                "street_id": "S3", "house_no": 2,
            }],
            "max_rows": 20, "max_cells": 1,
        }), encoding="utf-8")
        proc = self._run("plan", "--db", self.db, str(path), "--max-impressions", "0")
        self.assertEqual(proc.returncode, 6)
        self.assertEqual(json.loads(proc.stdout)["error"], "infeasible")


if __name__ == "__main__":
    unittest.main()
