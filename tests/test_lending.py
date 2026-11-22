"""借阅材料层（lending）的 unittest 测试套件。

覆盖：段—规约兼容性与第一笔证据、前缀凭据（空正文/跨段/重复出现）、
最少册数/总槽数、可选排法的报价前沿、脱离源卷的读机复原、源包校验与
读机分离、以及题目要求的两套演示数值。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from pcapng_kit import Catalog
from pcapng_kit.lending import (
    ReaderSpec,
    VolumeReader,
    build_segments,
    check_compatibility,
    compatibility_matrix,
    plan_lending,
    prefix_credentials,
    quotation_cost,
    verify_volume_document,
)
from pcapng_kit.lending_generator import (
    DEMO_A_BODY,
    build_demo_a,
    build_demo_b,
    encoding_specs,
    single_spec,
)
from pcapng_kit.blocks import LINKTYPE_ETHERNET, LINKTYPE_LINUX_SLL
from pcapng_kit.writer import InterfaceSpec, PcapNGWriter


def _spec(
    spec_id: int = 0,
    *,
    link_type: int = LINKTYPE_ETHERNET,
    max_saved: int = 256,
    unit: Fraction = Fraction(1),
    origin: int = 0,
) -> ReaderSpec:
    return ReaderSpec(
        spec_id=spec_id,
        link_type=link_type,
        max_saved_bytes=max_saved,
        time_unit=unit,
        origin=origin,
    )


class _TempFileMixin:
    def setUp(self) -> None:  # noqa: D401
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CompatibilityTests(_TempFileMixin, unittest.TestCase):
    def test_link_mismatch_is_first_evidence(self) -> None:
        target = self.tmp / "sll.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(link_type=LINKTYPE_LINUX_SLL))
            w.write_packet(0, 1, b"x")
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0])])
            result = check_compatibility(segments[0], _spec(link_type=LINKTYPE_ETHERNET))
            self.assertFalse(result.compatible)
            self.assertEqual(result.evidence.check, "link_type")
            self.assertEqual(result.evidence.occurrence_position, 0)

    def test_captured_len_exceeds_max_saved(self) -> None:
        target = self.tmp / "cap.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, b"abcdefgh")
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0])])
            result = check_compatibility(segments[0], _spec(max_saved=4))
            self.assertFalse(result.compatible)
            self.assertEqual(result.evidence.check, "captured_len")
            self.assertEqual(result.evidence.actual, "8")

    def test_tick_not_integer_negative_and_overflow(self) -> None:
        target = self.tmp / "ticks.pcapng"
        with PcapNGWriter(target) as w:
            # 十进制 10^-1：raw 3 -> 3/10 秒。
            w.start_section("little")
            w.write_interface(InterfaceSpec(ts_base=10, ts_exponent=1))
            w.write_packet(0, 3, b"a")
            w.write_packet(0, 0, b"b")
            w.write_packet(0, 700000, b"c")
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0, 1, 2])])
            base = _spec(unit=Fraction(1, 1))
            result = check_compatibility(segments[0], base)
            self.assertEqual(result.evidence.check, "tick_not_integer")
            # 用 1/10 秒刻度：3/10 -> 3 刻；0 -> 0 刻；700000/10 -> 70000 刻。
            tenth = _spec(unit=Fraction(1, 10))
            result2 = check_compatibility(segments[0], tenth)
            self.assertFalse(result2.compatible)
            self.assertEqual(result2.evidence.check, "tick_overflow")
            self.assertEqual(result2.evidence.occurrence_position, 2)
            # 加一个原点的规约能让负刻出现：原点 1 秒时第 1 包 (0.3 秒) 为负。
            origin_one = _spec(unit=Fraction(1, 10), origin=1)
            neg = check_compatibility(segments[0], origin_one)
            self.assertEqual(neg.evidence.check, "tick_negative")
            self.assertEqual(neg.evidence.occurrence_position, 0)

    def test_ticks_are_exact_fractions(self) -> None:
        target = self.tmp / "exact.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(ts_base=2, ts_exponent=3))
            w.write_packet(0, 5, b"a")     # 5/8 秒
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0])])
            result = check_compatibility(segments[0], _spec(unit=Fraction(1, 8)))
            self.assertTrue(result.compatible)
            self.assertEqual(result.ticks, (5,))
            result2 = check_compatibility(segments[0], _spec(unit=Fraction(1, 2)))
            self.assertEqual(result2.evidence.check, "tick_not_integer")

    def test_matrix_is_complete_and_json_serializable(self) -> None:
        sample = build_demo_a(self.tmp, name="m.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            rows = compatibility_matrix(segments, encoding_specs())
            self.assertEqual(len(rows), 2 * 2)
            payload = json.dumps([r.as_dict() for r in rows])
            self.assertIn("first_evidence", payload)


class CredentialTests(_TempFileMixin, unittest.TestCase):
    def test_prefix_credentials_across_segments_and_empty_body(self) -> None:
        target = self.tmp / "creds.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 1, b"ABC")
            w.write_packet(0, 2, b"")          # 空正文，可为任何更早正文的前缀
            w.start_section("big")
            w.write_interface(InterfaceSpec())
            w.write_packet(0, 3, b"AB")        # 跨段、为 ABC 的前缀
            w.write_packet(0, 4, b"ZZ")
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0, 1]), (1, 0, [2, 3])])
            creds = prefix_credentials(segments)
            pairs = {(c.earlier_ordinal, c.later_ordinal) for c in creds}
            # 空正文（出现 1）是更早 ABC 的完整前缀（长度 0）。
            self.assertIn((0, 1), pairs)   # ABC <- 空（同段）
            self.assertIn((0, 2), pairs)   # ABC <- AB（跨段）
            self.assertNotIn((2, 1), pairs)  # 后面的不能当前面的
            self.assertNotIn((0, 3), pairs)  # ZZ 不是 ABC 的前缀
            empty = next(c for c in creds if (c.earlier_ordinal, c.later_ordinal) == (0, 1))
            self.assertEqual(empty.byte_range, (0, 0))
            abc_ab = next(c for c in creds if (c.earlier_ordinal, c.later_ordinal) == (0, 2))
            self.assertEqual(abc_ab.byte_range, (0, 2))
            self.assertEqual(abc_ab.later_source[0], 1)  # 不同节

    def test_repeated_occurrence_keeps_source_identity_but_distinct_position(self) -> None:
        sample = build_demo_b(self.tmp, name="rep.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, [(0, 0, [0, 0, 1])])
            occ = segments[0].occurrences
            self.assertEqual(occ[0].source_identity, occ[1].source_identity)
            self.assertEqual((occ[0].position, occ[1].position), (0, 1))
            self.assertEqual(occ[0].body, occ[1].body)

    def test_same_packet_can_appear_in_two_segments_with_same_source(self) -> None:
        sample = build_demo_a(self.tmp, name="rep2.pcapng")
        with Catalog.open(sample["path"]) as cat:
            # 同一个目录包 0 在两个段里各出现一次。
            segments = build_segments(cat, [(0, 0, [0]), (0, 0, [0])])
            a = segments[0].occurrences[0]
            b = segments[1].occurrences[0]
            self.assertEqual(a.source_identity, b.source_identity)
            self.assertNotEqual(a.occurrence_id, b.occurrence_id)
            # 两个段重合，教材顺序里两处都保留。
            creds = prefix_credentials(segments)
            self.assertTrue(
                any(c.earlier_ordinal == 0 and c.later_ordinal == 1 for c in creds)
            )

    def test_incompatible_segment_raises_with_evidence(self) -> None:
        target = self.tmp / "sll2.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(link_type=LINKTYPE_LINUX_SLL))
            w.write_packet(0, 1, b"x")
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0])])
            with self.assertRaises(ValueError) as ctx:
                plan_lending(segments, [_spec(link_type=LINKTYPE_ETHERNET)])
            self.assertIn("link_type", str(ctx.exception))

    def test_hard_caps_are_enforced(self) -> None:
        target = self.tmp / "caps.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(ts_base=10, ts_exponent=0))
            for k in range(20):
                w.write_packet(0, k + 1, b"x")
        with Catalog.open(target) as cat:
            # 16 条记录允许，17 条拒绝。
            build_segments(cat, [(0, 0, list(range(16)))])
            with self.assertRaises(ValueError):
                build_segments(cat, [(0, 0, list(range(17)))])
            # 16 段超出 15 段上限。
            with self.assertRaises(ValueError):
                build_segments(cat, [(0, 0, [k]) for k in range(16)])
            segments = build_segments(cat, [(0, 0, [0])])
            # 6 种规约允许，7 种拒绝。
            six = [_spec(i, unit=Fraction(1)) for i in range(6)]
            plan_lending(segments, six)
            with self.assertRaises(ValueError):
                plan_lending(segments, six + [_spec(6)])
            # 每册 4 槽上限。
            with self.assertRaises(ValueError):
                plan_lending(segments, [_spec()], max_slots_per_volume=5)


class PackingTests(_TempFileMixin, unittest.TestCase):
    def test_demo_a_two_volumes_one_slot_each(self) -> None:
        sample = build_demo_a(self.tmp, name="a.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(segments, encoding_specs(), max_slots_per_volume=1)
        self.assertEqual((plan.min_volumes, plan.min_total_slots), (2, 2))
        self.assertEqual(
            [(q.direct_bytes, q.tape_travel) for q in plan.quotations], [(42, 0)]
        )

    def test_demo_a_calibrated_second_to_one_needs_one_volume(self) -> None:
        sample = build_demo_a(
            self.tmp, calibrate_second_to_one=True, name="ac.pcapng"
        )
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(segments, encoding_specs())
        self.assertEqual((plan.min_volumes, plan.min_total_slots), (1, 1))
        # 1/2 与 1 秒共用 1/2 刻度即可；同一槽。
        ticks = {}
        for q in plan.quotations:
            doc = q.document
            for v in doc["volumes"]:
                for r in v["records"]:
                    ticks[(r["segment_id"], r["tick"])] = r["slot"]
        self.assertEqual(ticks[(0, 1)], ticks[(1, 2)])

    def test_demo_a_two_slots_original_volume_one_volume_two_slots(self) -> None:
        sample = build_demo_a(self.tmp, name="a2.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(segments, encoding_specs(), max_slots_per_volume=2)
        self.assertEqual((plan.min_volumes, plan.min_total_slots), (1, 2))
        doc = plan.quotations[0].document
        self.assertEqual(doc["volumes"][0]["slot_count"], 2)
        slots = {r["segment_id"]: r["slot"] for r in doc["volumes"][0]["records"]}
        self.assertEqual(len(set(slots.values())), 2)

    def test_same_segment_must_share_one_slot(self) -> None:
        # 一个段内所有记录用同一规约（因此同一槽），即使两规约都兼容。
        target = self.tmp / "share.pcapng"
        with PcapNGWriter(target) as w:
            w.start_section("little")
            w.write_interface(InterfaceSpec(ts_base=10, ts_exponent=0))
            w.write_packet(0, 2, b"a")   # 2 秒
            w.write_packet(0, 4, b"b")   # 4 秒
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0, 1])])
            plan = plan_lending(
                segments,
                [_spec(0, unit=Fraction(1)), _spec(1, unit=Fraction(1, 2))],
            )
        doc = plan.quotations[0].document
        slots = {r["slot"] for r in doc["volumes"][0]["records"]}
        specs_used = {r["spec_id"] for r in doc["volumes"][0]["records"]}
        self.assertEqual(len(slots), 1)
        self.assertEqual(len(specs_used), 1)

    def test_capacity_forces_more_volumes(self) -> None:
        # 3 个互不兼容（不同链路）的段，每册只能 1 槽 -> 至少 3 册。
        target = self.tmp / "cap3.pcapng"
        links = [LINKTYPE_ETHERNET, LINKTYPE_LINUX_SLL, 228]
        with PcapNGWriter(target) as w:
            for link in links:
                w.start_section("little")
                w.write_interface(
                    InterfaceSpec(link_type=link, ts_base=10, ts_exponent=0)
                )
                w.write_packet(0, 1, b"x")
        specs = [_spec(i, link_type=link) for i, link in enumerate(links)]
        with Catalog.open(target) as cat:
            segments = build_segments(cat, [(0, 0, [0]), (1, 0, [1]), (2, 0, [2])])
            plan = plan_lending(segments, specs, max_slots_per_volume=1)
            self.assertEqual(plan.min_volumes, 3)
            self.assertEqual(plan.min_total_slots, 3)
            # 放宽到 3 槽：仍因链路互斥须 3 槽，但可并成 1 册。
            plan2 = plan_lending(segments, specs, max_slots_per_volume=3)
            self.assertEqual(plan2.min_volumes, 1)
            self.assertEqual(plan2.min_total_slots, 3)


class QuotationFrontierTests(_TempFileMixin, unittest.TestCase):
    def test_demo_b_exact_frontier_and_modes(self) -> None:
        sample = build_demo_b(self.tmp, name="b.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(
                segments,
                single_spec(max_saved_bytes=4),
                taps=[(0, 1), (0, 2), (0, 2), (0, 2)],
            )
        self.assertEqual(
            [(q.direct_bytes, q.tape_travel) for q in plan.quotations],
            [(4, 14), (8, 2), (12, 0)],
        )
        eight = next(q for q in plan.quotations if q.direct_bytes == 8)
        records = eight.document["volumes"][0]["records"]
        self.assertEqual([r["mode"] for r in records], ["direct", "prefix", "direct"])
        self.assertEqual(records[1]["ref_line"], 0)

    def test_changed_third_packet_raises_min_direct_bytes_to_eight(self) -> None:
        sample = build_demo_b(
            self.tmp, third_first_byte=b"X", name="bc.pcapng"
        )
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(
                segments,
                single_spec(max_saved_bytes=4),
                taps=[(0, 1), (0, 2), (0, 2), (0, 2)],
            )
        self.assertEqual((plan.min_volumes, plan.min_total_slots), (1, 1))
        self.assertEqual(min(q.direct_bytes for q in plan.quotations), 8)

    def test_changed_taps_move_the_eight_byte_choice(self) -> None:
        sample = build_demo_b(self.tmp, name="bt.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(
                segments,
                single_spec(max_saved_bytes=4),
                taps=[(0, 1), (0, 1), (0, 1), (0, 2)],
            )
        eight = next(q for q in plan.quotations if q.direct_bytes == 8)
        records = eight.document["volumes"][0]["records"]
        self.assertEqual([r["mode"] for r in records], ["direct", "direct", "prefix"])
        self.assertEqual(records[2]["ref_line"], 1)
        self.assertEqual(eight.tape_travel, 2)

    def test_quotation_cost_recomputes_from_document(self) -> None:
        sample = build_demo_b(self.tmp, name="bcost.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(
                segments,
                single_spec(max_saved_bytes=4),
                taps=[(0, 1), (0, 2), (0, 2), (0, 2)],
            )
        for quote in plan.quotations:
            self.assertEqual(
                quotation_cost(quote.document),
                (quote.direct_bytes, quote.tape_travel),
            )

    def test_frontier_has_no_dominated_pair(self) -> None:
        sample = build_demo_b(self.tmp, name="bdom.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            plan = plan_lending(
                segments,
                single_spec(max_saved_bytes=4),
                taps=[(0, 1), (0, 2), (0, 2), (0, 2)],
            )
        costs = [(q.direct_bytes, q.tape_travel) for q in plan.quotations]
        for a in costs:
            for b in costs:
                if a is b:
                    continue
                dominated = b[0] <= a[0] and b[1] <= a[1] and b != a
                self.assertFalse(dominated, f"{a} dominated by {b}")


class ReaderTests(_TempFileMixin, unittest.TestCase):
    def _eight_doc(self) -> tuple[Catalog, dict]:
        sample = build_demo_b(self.tmp, name="rd.pcapng")
        cat = Catalog.open(sample["path"])
        segments = build_segments(cat, sample["segments"])
        plan = plan_lending(
            segments,
            single_spec(max_saved_bytes=4),
            taps=[(0, 1), (0, 2), (0, 2), (0, 2)],
        )
        return cat, next(q for q in plan.quotations if q.direct_bytes == 8).document

    def test_reader_reconstructs_without_source_file(self) -> None:
        cat, doc = self._eight_doc()
        try:
            reader = VolumeReader(doc)
            rows = reader.read_all()
            self.assertEqual([r["body"] for r in rows], [b"ABCD"] * 3)
            self.assertEqual([r["orig_len"] for r in rows], [4, 6, 4])
            self.assertEqual([r["captured_len"] for r in rows], [4, 4, 4])
            # 第 2 行原长仍为 6、被替代包靠前缀复原。
            self.assertEqual(rows[1]["orig_len"], 6)
            self.assertEqual(rows[1]["mode"], "prefix")
        finally:
            cat.close()

    def test_reader_rejects_cross_volume_and_chained_reference(self) -> None:
        cat, doc = self._eight_doc()
        try:
            # 篡改成链式回指：第 3 行指回第 2 行（非直收）。
            bad = json.loads(json.dumps(doc))
            recs = bad["volumes"][0]["records"]
            recs[2]["mode"] = "prefix"
            recs[2]["ref_line"] = 1
            recs[2]["prefix_len"] = 4
            with self.assertRaises(ValueError):
                VolumeReader(bad).read_all()
        finally:
            cat.close()

    def test_reader_rejects_forward_reference(self) -> None:
        cat, doc = self._eight_doc()
        try:
            bad = json.loads(json.dumps(doc))
            recs = bad["volumes"][0]["records"]
            recs[0]["mode"] = "prefix"
            recs[0]["ref_line"] = 1
            recs[0]["prefix_len"] = 4
            del recs[0]["bytes_hex"]
            with self.assertRaises(ValueError):
                VolumeReader(bad).read_all()
        finally:
            cat.close()

    def test_verify_volume_document_matches_catalog(self) -> None:
        cat, doc = self._eight_doc()
        try:
            report = verify_volume_document(cat, doc)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["checked"], 3)
        finally:
            cat.close()

    def test_simulate_taps_travel(self) -> None:
        cat, doc = self._eight_doc()
        try:
            reader = VolumeReader(doc)
            sim = reader.simulate_taps([(0, 1), (0, 2), (0, 2), (0, 2)])
            self.assertEqual(sim["tape_travel"], 2)
            self.assertEqual(
                [e["body_hex"] for e in sim["events"]], ["41424344"] * 4
            )
        finally:
            cat.close()

    def test_reader_module_never_opens_source_files(self) -> None:
        # 读机只消费成卷 JSON，源码里不得出现文件入口。
        import inspect

        import pcapng_kit.lending as lending_mod

        source = inspect.getsource(lending_mod.VolumeReader)
        for forbidden in ("open(", "pathlib", "import os", "from os", "read_body"):
            self.assertNotIn(forbidden, source)


class DemoATruncationTests(_TempFileMixin, unittest.TestCase):
    def test_truncated_record_keeps_orig_len_and_captured_body(self) -> None:
        sample = build_demo_a(self.tmp, add_truncated=True, name="at.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            third = segments[2].occurrences[0]
            self.assertEqual(third.orig_len, 60)
            self.assertEqual(third.captured_len, 20)
            self.assertTrue(third.truncated)
            self.assertEqual(len(third.body), 20)
            plan = plan_lending(segments, encoding_specs(), max_slots_per_volume=4)
            doc = plan.quotations[0].document
            rec = next(
                r
                for v in doc["volumes"]
                for r in v["records"]
                if r["source"]["packet_index"] == 2
            )
            self.assertEqual(rec["orig_len"], 60)
            self.assertEqual(rec["captured_len"], 20)
            self.assertTrue(rec["truncated"])

    def test_demo_a_shared_body_identical(self) -> None:
        sample = build_demo_a(self.tmp, name="ab.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            self.assertEqual(segments[0].occurrences[0].body, DEMO_A_BODY)
            self.assertEqual(segments[1].occurrences[0].body, DEMO_A_BODY)
            creds = prefix_credentials(segments)
            self.assertTrue(
                any(c.earlier_ordinal == 0 and c.later_ordinal == 1 for c in creds)
            )

    def test_same_interface_id_in_distinct_sections_not_merged(self) -> None:
        # 两段都是节内接口 0，但源节不同；源身份必须区分节号。
        sample = build_demo_a(self.tmp, name="ab2.pcapng")
        with Catalog.open(sample["path"]) as cat:
            segments = build_segments(cat, sample["segments"])
            first = segments[0].occurrences[0]
            second = segments[1].occurrences[0]
            self.assertEqual((first.section_id, first.interface_id), (0, 0))
            self.assertEqual((second.section_id, second.interface_id), (1, 0))
            self.assertNotEqual(first.source_identity, second.source_identity)


if __name__ == "__main__":
    unittest.main()
