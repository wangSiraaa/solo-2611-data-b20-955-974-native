#!/usr/bin/env python3
"""借阅材料端到端演示：从本地 PCAPNG 编出可独立复查的借卷单。

覆盖题目要求的两套演示与全部读机校验::

    python3 lending_demo.py

产物写入 ``./samples/lending*``，并在终端打印关键结论。原有
``demo.py``（列目录 + 流量统计）不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

from pcapng_kit import Catalog
from pcapng_kit.lending import (
    VolumeReader,
    build_segments,
    plan_lending,
    prefix_credentials,
    verify_volume_document,
)
from pcapng_kit.lending_generator import (
    build_demo_a,
    build_demo_b,
    encoding_specs,
    single_spec,
)

SAMPLE_DIR = Path(__file__).resolve().parent / "samples"


def _plan_demo_a(
    pcap: Path,
    segment_plans,
    specs,
    *,
    max_slots: int = 4,
):
    with Catalog.open(pcap) as catalog:
        segments = build_segments(catalog, segment_plans)
        plan = plan_lending(segments, specs, max_slots_per_volume=max_slots)
    return plan


def _describe(plan) -> None:
    print(f"  最少册数={plan.min_volumes} 最少总槽数={plan.min_total_slots}")
    for quotation in plan.quotations:
        print(
            f"  报价 直收字节={quotation.direct_bytes:>2} "
            f"走带={quotation.tape_travel:>2} 册数={quotation.document['min_volumes']} "
            f"槽数={quotation.document['min_total_slots']}"
        )


def _show_reader(document: dict) -> None:
    """脱离源卷的读机：逐行复原，打印正确时刻与已捕正文。"""

    from fractions import Fraction

    specs = {
        spec["spec_id"]: (
            Fraction(spec["time_unit"]),
            spec["origin"],
        )
        for spec in document["specs"]
    }
    reader = VolumeReader(document)
    for volume in document["volumes"]:
        for line_no, record in enumerate(volume["records"]):
            row = reader.reconstruct(volume["volume_id"], line_no)
            unit, origin = specs[record["spec_id"]]
            seconds = Fraction(origin) + row["tick"] * unit
            print(
                f"    册{row['volume_id']} 行{row['line']} 槽{row['slot']} "
                f"刻数={row['tick']} ({seconds} 秒) 原长={row['orig_len']} "
                f"已捕={row['captured_len']} 正文={row['body']!r} "
                f"源={row['source']['section_id']}/{row['source']['interface_id']}"
                f"/#{row['source']['packet_index']}"
            )


def demo_a() -> None:
    print("== 演示 A：两段各一包、正文相同、接口均为节内 0 ==")
    spec_halves = encoding_specs()

    normal = build_demo_a(SAMPLE_DIR, name="lending_a.pcapng")
    plan = _plan_demo_a(
        normal["path"], normal["segments"], spec_halves, max_slots=1
    )
    assert (plan.min_volumes, plan.min_total_slots) == (2, 2), (
        plan.min_volumes,
        plan.min_total_slots,
    )
    print("  两段原时间 1/2 与 1/5 秒：每册一槽 -> 两册")
    _describe(plan)
    # 每段的刻度换算：段0 -> spec0 tick1；段1 -> spec1 tick1。
    compat = {(c.segment_id, c.spec_id): c for c in plan.compatibility}
    assert compat[(0, 0)].compatible and compat[(0, 0)].ticks == (1,)
    assert compat[(1, 1)].compatible and compat[(1, 1)].ticks == (1,)
    assert not compat[(0, 1)].compatible
    assert compat[(0, 1)].evidence.check == "tick_not_integer"
    print("  段0@规约0 刻数=1；段1@规约1 刻数=1；段0@规约1 排除：刻度非整数")
    _show_reader(plan.quotations[0].document)

    calibrated = build_demo_a(
        SAMPLE_DIR, calibrate_second_to_one=True, name="lending_a_cal.pcapng"
    )
    cal_plan = _plan_demo_a(
        calibrated["path"], calibrated["segments"], spec_halves, max_slots=1
    )
    assert (cal_plan.min_volumes, cal_plan.min_total_slots) == (1, 1), (
        cal_plan.min_volumes,
        cal_plan.min_total_slots,
    )
    print("  仅把第二段校为 1 秒：一槽一册")
    _describe(cal_plan)
    _show_reader(cal_plan.quotations[0].document)

    two_slot = _plan_demo_a(
        normal["path"], normal["segments"], spec_halves, max_slots=2
    )
    assert (two_slot.min_volumes, two_slot.min_total_slots) == (1, 2), (
        two_slot.min_volumes,
        two_slot.min_total_slots,
    )
    print("  保持原卷、每册两槽：一册两槽")
    _describe(two_slot)

    truncated = build_demo_a(
        SAMPLE_DIR, add_truncated=True, name="lending_a_trunc.pcapng"
    )
    with Catalog.open(truncated["path"]) as catalog:
        third = catalog.packets[2]
        body = catalog.read_body(third)
        assert third.orig_len == 60 and third.captured_len == 20
        assert len(body) == 20 and third.truncated
        print(
            f"  真实截断记录：orig_len=60 captured_len=20 "
            f"truncated={third.truncated} body={body.hex()[:16]}..."
        )
        plan_trunc = plan_lending(
            build_segments(catalog, truncated["segments"]), spec_halves
        )
        # 截断只交已捕字节；换算刻数只看时间。
        assert plan_trunc.min_volumes >= 1
        doc = plan_trunc.quotations[0].document
        rec = next(
            r
            for v in doc["volumes"]
            for r in v["records"]
            if r["source"]["packet_index"] == 2
        )
        assert rec["captured_len"] == 20 and rec["orig_len"] == 60
        print("  截断行：已捕 20 字节入卷、线上原长 60 不改短")

    (SAMPLE_DIR / "lending_a.plan.json").write_text(
        json.dumps(plan.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (SAMPLE_DIR / "lending_a.quotation.json").write_text(
        json.dumps(plan.quotations[0].document, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def demo_b() -> None:
    print("\n== 演示 B：同段三条记录、实际四字节相同、原长 4/6/4 ==")
    sample = build_demo_b(SAMPLE_DIR, name="lending_b.pcapng")
    specs = single_spec(max_saved_bytes=4)

    with Catalog.open(sample["path"]) as catalog:
        segments = build_segments(catalog, sample["segments"])
        assert [occ.orig_len for occ in segments[0].occurrences] == [4, 6, 4]
        assert [occ.body for occ in segments[0].occurrences] == [
            b"ABCD",
            b"ABCD",
            b"ABCD",
        ]
        # 三条记录的实际四字节相同；只有一条凭据（后面的取自第 1 行）。
        creds = prefix_credentials(segments)
        later_sources = {c.later_ordinal for c in creds}
        assert later_sources == {1, 2}, creds
        print("  前缀凭据：第2、3条正文均为第1条正文的完整前缀（4 字节）")

        # 第一组点读：第 2 行一次、第 3 行三次。
        plan = plan_lending(
            segments, specs, taps=[(0, 1), (0, 2), (0, 2), (0, 2)]
        )
        assert (plan.min_volumes, plan.min_total_slots) == (1, 1)
        costs = [(q.direct_bytes, q.tape_travel) for q in plan.quotations]
        assert costs == [(4, 14), (8, 2), (12, 0)], costs
        print("  三种不可互相替代报价：(4字节,14格) (8字节,2格) (12字节,0格)")
        good = next(q for q in plan.quotations if (q.direct_bytes, q.tape_travel) == (8, 2))
        modes = [r["mode"] for r in good.document["volumes"][0]["records"]]
        refs = [
            r.get("ref_line") for r in good.document["volumes"][0]["records"]
        ]
        assert modes == ["direct", "prefix", "direct"], modes
        assert refs[1] == 0
        print("  8 字节方案：第1、3行直收，第2行指回第1行")

        # 脱离源卷的真实读机读回整行，并核第 2 行原长仍为 6。
        reader = VolumeReader(good.document)
        rows = reader.read_all()
        assert [r["body"] for r in rows] == [b"ABCD", b"ABCD", b"ABCD"]
        assert [r["orig_len"] for r in rows] == [4, 6, 4]
        assert rows[1]["orig_len"] == 6 and rows[1]["mode"] == "prefix"
        assert rows[1]["captured_len"] == 4
        sim = reader.simulate_taps([(0, 1), (0, 2), (0, 2), (0, 2)])
        assert sim["tape_travel"] == 2, sim
        print("  读机复原三段正文均为 ABCD；第2行原长=6、已捕=4；走带=2")
        _show_reader(good.document)

        # 源包校验与读机分离：独立经目录回读核对成卷。
        report = verify_volume_document(catalog, good.document)
        assert report["ok"], report
        print(f"  源包校验：{report['checked']} 行逐字节一致")

    # 只改第 3 包已捕首字节：原一槽一册照旧，最低占用改为 8 字节。
    changed = build_demo_b(SAMPLE_DIR, third_first_byte=b"X", name="lending_b_changed.pcapng")
    with Catalog.open(changed["path"]) as catalog:
        segments = build_segments(catalog, changed["segments"])
        plan = plan_lending(
            segments, specs, taps=[(0, 1), (0, 2), (0, 2), (0, 2)]
        )
        best_bytes = min(q.direct_bytes for q in plan.quotations)
        assert best_bytes == 8, best_bytes
        assert (plan.min_volumes, plan.min_total_slots) == (1, 1)
        print("  改第3包首字节后：仍一槽一册，最低直收字节升为 8")

    # 固定原采集，改老师点读：第 2 行三次、第 3 行一次。
    with Catalog.open(sample["path"]) as catalog:
        segments = build_segments(catalog, sample["segments"])
        plan = plan_lending(
            segments, specs, taps=[(0, 1), (0, 1), (0, 1), (0, 2)]
        )
        good = next(q for q in plan.quotations if q.direct_bytes == 8)
        records = good.document["volumes"][0]["records"]
        assert [r["mode"] for r in records] == ["direct", "direct", "prefix"]
        assert records[2]["ref_line"] == 1
        assert good.tape_travel == 2, good.tape_travel
        reader = VolumeReader(good.document)
        rows = reader.read_all()
        assert [r["orig_len"] for r in rows] == [4, 6, 4]
        print(
            "  改点读后 8 字节好版：直收第1、2行，第3行指回第2行；"
            "读机走带=2、第2行原长仍=6"
        )

    (SAMPLE_DIR / "lending_b.plan.json").write_text(
        json.dumps(plan.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> None:
    demo_a()
    demo_b()
    print("\n借阅演示完成；产物位于", SAMPLE_DIR)


if __name__ == "__main__":
    main()
