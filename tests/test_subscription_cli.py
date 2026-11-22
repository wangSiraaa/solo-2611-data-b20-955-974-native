"""Smoke tests for the subscription CLI subcommands."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from address_delivery.cli import main
from support import MATERIALS


class CliSubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "cli.db")
        main(["init", "--db", self.db, "--materials", str(MATERIALS)])
        main(["submit", "--db", self.db, "NP1", "C1", "东", "学园路", "2"])

    def _run(self, *argv: str) -> dict:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(argv))
        self.assertEqual(code, 0, buffer.getvalue())
        return json.loads(buffer.getvalue())

    def test_full_cli_workflow_and_optimal_schedule(self):
        for box, label in (("BX0", "老户箱"), ("BXA", "甲箱"), ("BXB", "乙箱")):
            self._run("register-box", "--db", self.db, box, "S3", "2", label, "1")
        anchor = self._run(
            "anchor", "--db", self.db, "R1", "OLD", "NP1", "BX0", "--share", "1"
        )
        self.assertEqual(anchor["street_id"], "S3")

        split_doc = Path(self.tmp.name) / "split.json"
        split_doc.write_text(
            json.dumps({
                "batch_id": "B1",
                "effective_period": 1,
                "entries": [
                    {"old_household": "OLD", "new_household": "A",
                     "ratio": "2/3", "box_id": "BXA"},
                    {"old_household": "OLD", "new_household": "B",
                     "ratio": "1/3", "box_id": "BXB"},
                ],
            }),
            encoding="utf-8",
        )
        batch = self._run("split", "--db", self.db, str(split_doc))
        self.assertEqual(batch["batch_id"], "B1")

        entitlements = self._run("entitlements", "--db", self.db, "3")
        shares = {
            (a["period"], a["household_id"]): a["share"]
            for a in entitlements["allocations"]
        }
        self.assertEqual(shares[(2, "A")], "2/3")

        plan = self._run("schedule", "--db", self.db, "3")
        self.assertEqual(plan["max_deviation"], "1/3")
        self.assertEqual([i["recipient"] for i in plan["issues"]], ["A", "B", "A"])

    def test_cli_rejects_anchor_without_successful_parcel(self):
        self._run("register-box", "--db", self.db, "BX9", "S3", "2", "箱", "1")
        # A never-submitted parcel fails with the subscription error code.
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["anchor", "--db", self.db, "RX", "H", "GHOST", "BX9"])
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(buffer.getvalue())["error"], "subscription")


if __name__ == "__main__":
    unittest.main()
