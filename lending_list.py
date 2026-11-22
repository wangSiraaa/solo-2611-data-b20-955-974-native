#!/usr/bin/env python3
"""独立借卷单工具：从本地 PCAPNG 与一份段计划 JSON 编出借阅材料。

用法::

    python3 lending_list.py <file.pcapng> <plan.json> [--pretty]

``plan.json`` 结构::

    {
      "segments": [{"section_id":0,"interface_id":0,"packets":[0,1]}, ...],
      "specs": [{"spec_id":0,"link_type":1,"max_saved_bytes":256,
                 "time_unit":"1/2","origin":0}, ...],
      "taps": [[0,1],[0,2],[0,2]],           # 可省略
      "max_slots_per_volume": 4              # 可省略
    }

输出为完整借卷单（段—规约清单、前缀凭据、最少册数/槽数、报价前沿）。
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

from pcapng_kit.catalog import Catalog
from pcapng_kit.lending import ReaderSpec, build_segments, plan_lending


def _load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan a lending order from PCAPNG")
    parser.add_argument("pcapng")
    parser.add_argument("plan")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    plan_data = _load_plan(Path(args.plan))
    specs = [
        ReaderSpec(
            spec_id=item["spec_id"],
            link_type=item["link_type"],
            max_saved_bytes=item["max_saved_bytes"],
            time_unit=Fraction(item["time_unit"]),
            origin=item.get("origin", 0),
        )
        for item in plan_data["specs"]
    ]
    segment_plans = [
        (item["section_id"], item["interface_id"], item["packets"])
        for item in plan_data["segments"]
    ]
    taps = (
        [tuple(t) for t in plan_data["taps"]]
        if plan_data.get("taps") is not None
        else None
    )
    max_slots = plan_data.get("max_slots_per_volume", 4)

    with Catalog.open(args.pcapng) as catalog:
        segments = build_segments(catalog, segment_plans)
        plan = plan_lending(
            segments, specs, taps=taps, max_slots_per_volume=max_slots
        )
    print(
        json.dumps(
            plan.as_dict(), indent=2 if args.pretty else None, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
