#!/usr/bin/env python3
"""校园展馆逐帧放映恢复：合成捕获 -> 帧收集册 -> 时刻查询 -> 播放台排片。

运行::

    python3 screening_demo.py

产物位于 ``./samples/screening/``：三个剧目的 PCAPNG、独立元数据、
各查询时刻的收集册报告与最优排片 JSON。
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from pcapng_kit import (
    ACTION_LOAD,
    ACTION_ORIG,
    ACTION_PRINT,
    ACTION_REPLACE,
    ACTION_WAIT,
    AlbumScope,
    Catalog,
    FrameAlbum,
    ProjectorPlanner,
    TrafficStore,
)
from pcapng_kit.showgen import (
    FORWARD_DPORT,
    FORWARD_DST,
    FORWARD_SPORT,
    FORWARD_SRC,
    build_four_frame_capture,
    build_three_frame_capture,
    build_two_blocker_capture,
)

OUT_DIR = Path(__file__).resolve().parent / "samples" / "screening"


def open_album(pcap_path: Path, session: int) -> tuple[Catalog, TrafficStore, FrameAlbum]:
    catalog = Catalog.open(pcap_path)
    store = TrafficStore.build(catalog)
    scope = AlbumScope(
        section_id=0,
        interface_id=0,
        src_ip=FORWARD_SRC,
        dst_ip=FORWARD_DST,
        src_port=FORWARD_SPORT,
        dst_port=FORWARD_DPORT,
        session=session,
    )
    return catalog, store, FrameAlbum.build(catalog, store, scope)


def dump(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def show_three_frame() -> None:
    print("=" * 70)
    print("剧目一：三整片 A/B/C（含反向返显、跨节同号接口、头不全、迟到冲突）")
    print("=" * 70)
    base = build_three_frame_capture(OUT_DIR, name="three_frame")
    late = build_three_frame_capture(
        OUT_DIR, name="three_frame_late_b", b_full_tick=3, late_conflict_tick=6
    )

    for tag, artifact in (("base", base), ("late_b", late)):
        catalog, _store, album = open_album(artifact["pcap"], session=1)
        report = {
            "capture": artifact["pcap"].name,
            "headerless": album.headerless_listing(),
            "timeline": album.timeline(),
            "queries": {
                str(t): album.query_report(Fraction(t)) for t in (0, 1, 2, 3, 4, 5, 6)
            },
        }
        dump(OUT_DIR / f"three_frame_{tag}_album.json", report)
        print(f"\n[{tag}] 头不全记录（不归帧）: "
              f"{[r['packet_index'] for r in report['headerless']]}")
        for t in (0, 1, 2, 3):
            views = album.snapshot(Fraction(t))
            line = ", ".join(
                f"{no}:{v.status.value}/读{v.readable_len}B" for no, v in sorted(views.items())
            )
            print(f"  刻 {t} -> {line}")

        # 迟到冲突到达后：B 永久待核，不得择副本。
        conflict_t = 5 if tag == "base" else 6
        b_after = album.view_at(2, Fraction(conflict_t))
        print(f"  刻 {conflict_t} 冲突副本到达后 B: {b_after.status.value} "
              f"({b_after.reason})")

        # 片单 A,B,C；损失 1/5/1。
        loss = {1: 1, 2: 5, 3: 1}
        for curtain in (4, 3):
            planner = ProjectorPlanner(
                album, [1, 2, 3], loss, curtain, print_byte_budget=0
            )
            plan = planner.plan()
            dump(
                OUT_DIR / f"three_frame_{tag}_curtain{curtain}_plan.json",
                plan.as_dict(),
            )
            print(
                f"  [curtain={curtain}] 可行={plan.feasible} "
                f"总损失={plan.total_loss} 散场刻={plan.end_tick}"
            )
            for beat in plan.beats:
                print("    " + _beat_line(beat))
        catalog.close()


def show_four_frame() -> None:
    print("\n" + "=" * 70)
    print("剧目二：V/U/Q/R（整片 U,V；差片 Q←U，R←Q）")
    print("=" * 70)
    base = build_four_frame_capture(OUT_DIR, name="four_frame")
    late_u = build_four_frame_capture(
        OUT_DIR, name="four_frame_late_u", u_full_tick=1
    )
    frame_nos = {"V": 2, "U": 1, "Q": 3, "R": 4}
    loss = {2: 20, 3: 5, 4: 3}
    playlist = [2, 3, 4]

    for tag, artifact in (("base", base), ("late_u", late_u)):
        catalog, _store, album = open_album(artifact["pcap"], session=7)
        dump(OUT_DIR / f"four_frame_{tag}_album.json", album.query_report(Fraction(0)))
        print(f"\n[{tag}] 刻0可印帧: {album.printable_frames(Fraction(0))}")
        for budget, label in ((1, "1B"), (0, "0B")):
            planner = ProjectorPlanner(
                album, playlist, loss, 3, print_byte_budget=budget
            )
            plan = planner.plan()
            dump(OUT_DIR / f"four_frame_{tag}_budget{label}_curtain3.json", plan.as_dict())
            print(
                f"  印片额度={label} 散场3: 可行={plan.feasible} "
                f"损失={plan.total_loss} 散场={plan.end_tick} "
                f"印={plan.printed_frames}({plan.printed_bytes}B)"
            )
            for beat in plan.beats:
                print("    " + _beat_line(beat))
        # 零印片、散场 4：允许一拍现场装底。
        planner = ProjectorPlanner(album, playlist, loss, 4, print_byte_budget=0)
        plan = planner.plan()
        dump(OUT_DIR / f"four_frame_{tag}_budget0B_curtain4.json", plan.as_dict())
        print(f"  印片额度=0 散场4: 损失={plan.total_loss} 散场={plan.end_tick}")
        for beat in plan.beats:
            print("    " + _beat_line(beat))
        catalog.close()


def show_two_blocker() -> None:
    print("\n" + "=" * 70)
    print("剧目三：两处不同错底（Q 缺 U、S 缺 W），一张画片 + 一拍现场装底")
    print("=" * 70)
    artifact = build_two_blocker_capture(OUT_DIR)
    catalog, _store, album = open_album(artifact["pcap"], session=9)
    # 片单 V(2), Q(4), S(5), W(3)：W 排在 S 之后，因此 S 之前无法现场装 W 底。
    playlist = [2, 4, 5, 3]
    loss = {2: 30, 4: 9, 5: 7, 3: 8}

    # 零资源基线：两处错底都只能替放（Q 损失 9、S 损失 7，共 16）。
    planner = ProjectorPlanner(album, playlist, loss, 4, print_byte_budget=0)
    baseline = planner.plan()
    dump(OUT_DIR / "two_blocker_baseline.json", baseline.as_dict())
    print(f"零资源 散场4: 损失={baseline.total_loss} 散场={baseline.end_tick}")
    for beat in baseline.beats:
        print("    " + _beat_line(beat))

    # 只给画片不给装底拍（散场 4、一卡）：画片救 Q；S 缺 W 且 W 尚未上墙，
    # 只能替放 -> 损失 7。
    planner = ProjectorPlanner(album, playlist, loss, 4, print_byte_budget=1)
    card_only = planner.plan()
    dump(OUT_DIR / "two_blocker_card_only.json", card_only.as_dict())
    print(f"仅一画片 散场4: 损失={card_only.total_loss} 印帧={card_only.printed_frames}")
    for beat in card_only.beats:
        print("    " + _beat_line(beat))

    # 只给装底拍不给画片（散场 5、零印片）：现场只能装 U 救 Q；S 在 W 之前，
    # 装不到 W，S 仍替放 -> 损失 7。
    planner = ProjectorPlanner(album, playlist, loss, 5, print_byte_budget=0)
    load_only = planner.plan()
    dump(OUT_DIR / "two_blocker_load_only.json", load_only.as_dict())
    print(f"仅一装底拍 散场5: 损失={load_only.total_loss} 散场={load_only.end_tick}")
    for beat in load_only.beats:
        print("    " + _beat_line(beat))

    # 两种救场操作分给不同片段：实物画片救 Q，现场空拍装 W 底救 S。
    planner = ProjectorPlanner(
        album, playlist, loss, 5, print_byte_budget=1
    )
    plan = planner.plan()
    dump(OUT_DIR / "two_blocker_optimal.json", plan.as_dict())
    print(f"一画片+一装底拍 散场5: 损失={plan.total_loss} 散场={plan.end_tick} "
          f"印帧={plan.printed_frames} 字节={plan.printed_bytes}")
    for beat in plan.beats:
        print("    " + _beat_line(beat))
    print(f"  画片整条来历所用源包: {plan.evidence_used}")
    catalog.close()


def _beat_line(beat) -> str:
    names = {
        ACTION_ORIG: "原放", ACTION_PRINT: "画片", ACTION_REPLACE: "替放",
        ACTION_WAIT: "空等", ACTION_LOAD: "装底",
    }
    extra = ""
    if beat.action == ACTION_REPLACE and beat.frame_no is not None:
        extra = f"（真正重复: {beat.wall_after}，损失 {beat.loss}）"
    return (
        f"刻{beat.tick} {names[beat.action]} 帧={beat.frame_no} "
        f"墙:{beat.wall_before}->{beat.wall_after} "
        f"底:{beat.base_before}->{beat.base_after}{extra}"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    show_three_frame()
    show_four_frame()
    show_two_blocker()
    print("\n全部报告已写入", OUT_DIR)


if __name__ == "__main__":
    main()
