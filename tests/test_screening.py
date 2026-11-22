"""帧收集册与播放台的 unittest 套件（仅标准库）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
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
    FrameHeaderError,
    FrameStatus,
    KIND_DELTA,
    KIND_FULL,
    NO_BASE,
    ProjectorPlanner,
    TrafficStore,
    WALL_LOGO,
    build_screen_frame,
    parse_screen_header,
)
from pcapng_kit.projector import MAX_PRINT_CARDS
from pcapng_kit import frames as sf
from pcapng_kit.showgen import (
    FORWARD_DPORT,
    FORWARD_DST,
    FORWARD_SPORT,
    FORWARD_SRC,
    CapturePlan,
    build_four_frame_capture,
    build_three_frame_capture,
    build_two_blocker_capture,
    write_capture,
)

ROOT = Path(__file__).resolve().parent.parent


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._catalogs: list[Catalog] = []

    def tearDown(self) -> None:
        for catalog in self._catalogs:
            catalog.close()
        self._td.cleanup()


def scope_for(session: int) -> AlbumScope:
    return AlbumScope(0, 0, FORWARD_SRC, FORWARD_DST, FORWARD_SPORT, FORWARD_DPORT, session)


def build_album(path: Path, session: int, holder: "_Tmp | None" = None) -> FrameAlbum:
    catalog = Catalog.open(path)
    if holder is not None:
        holder._catalogs.append(catalog)
    store = TrafficStore.build(catalog)
    album = FrameAlbum.build(catalog, store, scope_for(session))
    album._keepalive = catalog       # 防止文件句柄在测试期间被回收
    return album


# ---------------------------------------------------------------------------
# 一、小型合成帧格式
# ---------------------------------------------------------------------------
class FrameFormatTests(unittest.TestCase):
    def test_roundtrip_full_and_delta(self) -> None:
        full = build_screen_frame(3, 1, KIND_FULL, b"\xAA\xBB")
        head = parse_screen_header(full, session=3)
        self.assertEqual((head.session, head.frame_no, head.kind), (3, 1, KIND_FULL))
        self.assertEqual((head.declared_len, head.base_no), (2, NO_BASE))
        self.assertEqual(sf.extract_prefix(full, head), b"\xAA\xBB")

        delta = build_screen_frame(3, 2, KIND_DELTA, b"\x01", base_no=1)
        head2 = parse_screen_header(delta, session=3)
        self.assertEqual((head2.kind, head2.base_no), (KIND_DELTA, 1))

    def test_header_rejects_bad_magic_short_payload_wrong_session(self) -> None:
        good = build_screen_frame(1, 1, KIND_FULL, b"X")
        self.assertTrue(good.startswith(b"SF"))
        with self.assertRaises(FrameHeaderError):
            parse_screen_header(b"XX" + good[2:], session=1)
        with self.assertRaises(FrameHeaderError):
            parse_screen_header(good[: sf.HEADER_SIZE - 1], session=1)
        with self.assertRaises(FrameHeaderError):
            parse_screen_header(good, session=2)

    def test_contract_body_one_to_four_bytes_and_earlier_base(self) -> None:
        with self.assertRaises(ValueError):
            build_screen_frame(1, 1, KIND_FULL, b"")
        with self.assertRaises(ValueError):
            build_screen_frame(1, 1, KIND_FULL, b"12345")
        with self.assertRaises(ValueError):
            build_screen_frame(1, 2, KIND_DELTA, b"x", base_no=2)   # 非较早帧
        with self.assertRaises(ValueError):
            build_screen_frame(1, 1, KIND_FULL, b"x", base_no=1)

    def test_xor_picture(self) -> None:
        self.assertEqual(sf.xor_picture(b"\x03", b"\x07"), b"\x04")
        with self.assertRaises(ValueError):
            sf.xor_picture(b"\x01\x02", b"\x03")


# ---------------------------------------------------------------------------
# 二、收集册：前缀、同刻生效、冲突、隔离、头不全
# ---------------------------------------------------------------------------
class AlbumEvidenceTests(_Tmp):
    def test_prefix_held_then_completed_by_retransmit(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        b0 = album.view_at(2, Fraction(0))
        self.assertEqual(b0.status, FrameStatus.HELD)
        self.assertEqual(b0.readable_len, 1)
        self.assertEqual(b0.readable_bytes, b"\xB2")
        b2 = album.view_at(2, Fraction(2))
        self.assertEqual(b2.status, FrameStatus.COMPLETE)
        self.assertEqual(b2.readable_bytes, b"\xB2\xB3")
        # 同刻整组生效：刻 0 时 A、B 前缀都已在。
        self.assertEqual(album.view_at(1, Fraction(0)).status, FrameStatus.COMPLETE)

    def test_reverse_echo_and_other_section_never_fill_frame(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        # 刻 1：只有反向返显 B 与跨节 B，正向 B 仍是 1/2 前缀。
        view = album.view_at(2, Fraction(1))
        self.assertEqual(view.status, FrameStatus.HELD)
        self.assertEqual(view.readable_len, 1)
        # 反向/跨节包没有产生任何额外副本。
        self.assertTrue(all(c.timestamp != Fraction(1) for c in album.copies[2]))

    def test_headerless_record_keeps_provenance_but_no_frame(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        self.assertEqual(len(album.headerless), 1)
        record = album.headerless[0]
        self.assertIn("header incomplete", record.reason)
        self.assertEqual(record.block_range[1] > record.block_range[0], True)
        self.assertNotIn(99, album.copies)

    def test_late_conflict_marks_frame_pending_and_never_recovers_silently(self) -> None:
        artifact = build_three_frame_capture(
            self.tmp, name="t", late_conflict_tick=5
        )
        album = build_album(artifact["pcap"], session=1, holder=self)
        self.assertEqual(album.view_at(2, Fraction(4)).status, FrameStatus.COMPLETE)
        conflicted = album.view_at(2, Fraction(5))
        self.assertEqual(conflicted.status, FrameStatus.CONFLICT)
        self.assertEqual(conflicted.readable_bytes, b"")     # 待核不给字节
        # 冲突永久保留，后续时刻不自动翻案。
        self.assertEqual(album.view_at(2, Fraction(99)).status, FrameStatus.CONFLICT)

    def test_declared_length_and_base_identity_conflicts(self) -> None:
        session = 5
        # 声明长度冲突。
        plan = CapturePlan(self.tmp / "decl.pcapng", session)
        f1 = build_screen_frame(session, 1, KIND_FULL, b"\x01\x02")
        f1_other_len = build_screen_frame(session, 1, KIND_FULL, b"\x01")
        plan.add(0, f1, label="len2")
        plan.add(1, f1_other_len, label="len1")
        write_capture(plan)
        album = build_album(plan.path, session, holder=self)
        self.assertEqual(album.view_at(1, Fraction(1)).status, FrameStatus.CONFLICT)

        # 底片身份冲突：两份副本正文逐字节相同，仅头中底片号不同。
        plan2 = CapturePlan(self.tmp / "base.pcapng", session)
        u = build_screen_frame(session, 1, KIND_FULL, b"\x07")
        w = build_screen_frame(session, 3, KIND_FULL, b"\x09")
        q1 = build_screen_frame(session, 2, KIND_DELTA, b"\x03", base_no=1)
        raw = bytearray(q1)
        raw[8:10] = (3).to_bytes(2, "big")       # 只改头里的底片号字段
        plan2.add(0, u)
        plan2.add(0, w)
        plan2.add(0, q1)
        plan2.add(1, bytes(raw))
        write_capture(plan2)
        album2 = build_album(plan2.path, session, holder=self)
        view = album2.view_at(2, Fraction(1))
        self.assertEqual(view.status, FrameStatus.CONFLICT)
        self.assertIn("base identities differ", view.reason)

    def test_same_timestamp_group_takes_effect_together(self) -> None:
        session = 6
        plan = CapturePlan(self.tmp / "group.pcapng", session)
        plan.add(0, build_screen_frame(session, 1, KIND_FULL, b"\xAA"))
        plan.add(0, build_screen_frame(session, 2, KIND_FULL, b"\xBB"))
        write_capture(plan)
        album = build_album(plan.path, session, holder=self)
        snap = album.snapshot(Fraction(0))
        self.assertEqual({v.status for v in snap.values()}, {FrameStatus.COMPLETE})
        self.assertEqual(set(album.arrival_times()), {Fraction(0)})


# ---------------------------------------------------------------------------
# 三、底片链与试印
# ---------------------------------------------------------------------------
class ChainRenderTests(_Tmp):
    def test_full_chain_render_and_blocker(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        # U=1, Q 正文3 -> 画面 2；R 正文4 -> 画面 6。
        self.assertEqual(album.chain_check(1, Fraction(0)).picture, b"\x01")
        self.assertEqual(album.chain_check(3, Fraction(0)).picture, b"\x02")
        r = album.chain_check(4, Fraction(0))
        self.assertTrue(r.ok)
        self.assertEqual(r.picture, b"\x06")
        self.assertEqual(r.chain_frames, (1, 3, 4))

    def test_chain_blocked_by_missing_and_conflict(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f", u_full_tick=1)
        album = build_album(artifact["pcap"], session=7, holder=self)
        blocked = album.chain_check(3, Fraction(0))
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.blocker, 1)          # U 未完整
        self.assertIsNone(album.chain_check(3, Fraction(0)).picture)
        # 刻 1 补齐后链通。
        self.assertTrue(album.chain_check(4, Fraction(1)).ok)

    def test_conflicted_dependency_blocks_delta_even_if_base_wall_matches(self) -> None:
        session = 8
        plan = CapturePlan(self.tmp / "dep.pcapng", session)
        u = build_screen_frame(session, 1, KIND_FULL, b"\x07")
        q = build_screen_frame(session, 2, KIND_DELTA, b"\x03", base_no=1)
        plan.add(0, u)
        plan.add(0, q)
        plan.add(2, build_screen_frame(session, 1, KIND_FULL, b"\x77"))  # U 冲突副本
        write_capture(plan)
        album = build_album(plan.path, session, holder=self)
        self.assertEqual(album.view_at(1, Fraction(2)).status, FrameStatus.CONFLICT)
        result = album.chain_check(2, Fraction(2))
        self.assertFalse(result.ok)
        self.assertEqual(result.blocker, 1)


# ---------------------------------------------------------------------------
# 四、播放台：题面给定的全部场景
# ---------------------------------------------------------------------------
class ProjectorGivenScenariosTests(_Tmp):
    def test_three_frame_base_curtain4_zero_loss_with_wait(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        plan = ProjectorPlanner(album, [1, 2, 3], {1: 1, 2: 5, 3: 1}, 4).plan()
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.total_loss, 0)
        self.assertEqual(plan.end_tick, 4)
        actions = [b.action for b in plan.beats]
        self.assertEqual(actions, [ACTION_ORIG, ACTION_WAIT, ACTION_ORIG, ACTION_ORIG])

    def test_three_frame_late_b_is_loss5_earliest_tick3(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t", b_full_tick=3)
        album = build_album(artifact["pcap"], session=1, holder=self)
        plan = ProjectorPlanner(album, [1, 2, 3], {1: 1, 2: 5, 3: 1}, 4).plan()
        self.assertEqual(plan.total_loss, 5)
        self.assertEqual(plan.end_tick, 3)
        replace = next(b for b in plan.beats if b.action == ACTION_REPLACE)
        self.assertEqual(replace.frame_no, 2)
        self.assertEqual(replace.wall_after, 1)       # 真正重复的是 A

    def test_three_frame_curtain3_also_loss5(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        plan = ProjectorPlanner(album, [1, 2, 3], {1: 1, 2: 5, 3: 1}, 3).plan()
        self.assertEqual(plan.total_loss, 5)
        self.assertEqual(plan.end_tick, 3)

    def test_four_frame_print_q_one_byte_zero_loss(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        plan = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 3, print_byte_budget=1
        ).plan()
        self.assertEqual(plan.total_loss, 0)
        self.assertEqual(plan.printed_frames, [3])
        self.assertEqual(plan.printed_bytes, 1)
        self.assertEqual([b.action for b in plan.beats],
                         [ACTION_ORIG, ACTION_PRINT, ACTION_ORIG])

    def test_four_frame_printing_r_alone_still_misses_q_base(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        # 模拟“只印 R”：把可印资源表限制为只剩 R（帧 4），Q（帧 3）不在表内。
        planner = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 3, print_byte_budget=1
        )
        planner._printable = {4: 1}
        plan = planner.plan()
        # R 卡救不了 Q 的错底：Q 拍仍只能替放，损失 5。
        self.assertEqual(plan.total_loss, 5)
        self.assertNotIn(3, plan.printed_frames)

    def test_four_frame_zero_print_curtain3_loss8(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        plan = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 3, print_byte_budget=0
        ).plan()
        self.assertEqual(plan.total_loss, 8)

    def test_four_frame_zero_print_curtain4_uses_load_u(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        plan = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 4, print_byte_budget=0
        ).plan()
        self.assertEqual(plan.total_loss, 0)
        self.assertEqual(plan.end_tick, 4)
        load = next(b for b in plan.beats if b.action == ACTION_LOAD)
        self.assertEqual(load.frame_no, 1)
        self.assertEqual(load.wall_after, 2)          # 墙面保持 V
        self.assertEqual(load.base_after, 1)

    def test_late_u_not_printable_at_rehearsal_and_curtain3_loss8(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f", u_full_tick=1)
        album = build_album(artifact["pcap"], session=7, holder=self)
        self.assertEqual(album.printable_frames(Fraction(0)), [2])
        plan = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 3, print_byte_budget=1
        ).plan()
        self.assertEqual(plan.total_loss, 8)
        self.assertEqual(plan.printed_frames, [])

    def test_two_blockers_split_card_and_load(self) -> None:
        artifact = build_two_blocker_capture(self.tmp)
        album = build_album(artifact["pcap"], session=9, holder=self)
        playlist = [2, 4, 5, 3]
        loss = {2: 30, 4: 9, 5: 7, 3: 8}
        baseline = ProjectorPlanner(album, playlist, loss, 4).plan()
        self.assertEqual(baseline.total_loss, 16)
        optimal = ProjectorPlanner(
            album, playlist, loss, 5, print_byte_budget=1
        ).plan()
        self.assertEqual(optimal.total_loss, 0)
        self.assertEqual(optimal.printed_frames, [4])          # 实物画片给 Q
        kinds = [b.action for b in optimal.beats]
        self.assertIn(ACTION_PRINT, kinds)
        load = next(b for b in optimal.beats if b.action == ACTION_LOAD)
        self.assertEqual(load.frame_no, 3)                    # 现场空拍装 W 给 S
        # 画片来历含整条链（U、Q）的源包，且去重。
        self.assertEqual(set(optimal.evidence_used["4"]), {0, 3})


# ---------------------------------------------------------------------------
# 五、状态机细则与物理不可行
# ---------------------------------------------------------------------------
class ProjectorMechanicsTests(_Tmp):
    def _album(self) -> FrameAlbum:
        artifact = build_four_frame_capture(self.tmp, name="f")
        return build_album(artifact["pcap"], session=7, holder=self)

    def test_logo_replace_keeps_no_base_and_first_beat_replace_uses_logo(self) -> None:
        album = self._album()
        # 片单从差片 Q 开始，开场无底，只能替放馆徽。
        plan = ProjectorPlanner(album, [3], {3: 5}, 1).plan()
        self.assertEqual(plan.total_loss, 5)
        beat = plan.beats[0]
        self.assertEqual(beat.action, ACTION_REPLACE)
        self.assertEqual(beat.wall_after, WALL_LOGO)
        self.assertIsNone(beat.base_after)               # 馆徽不充当底

    def test_replace_rolls_base_back_to_repeated_frame(self) -> None:
        album = self._album()
        # V 原放后底=2；Q 替放重复 V，底应回到 2。
        plan = ProjectorPlanner(
            album, [2, 3], {2: 20, 3: 5}, 2, print_byte_budget=0
        ).plan()
        q_beat = plan.beats[1]
        self.assertEqual(q_beat.action, ACTION_REPLACE)
        self.assertEqual(q_beat.base_after, 2)

    def test_wait_preserves_wall_and_base(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        plan = ProjectorPlanner(album, [1, 2, 3], {1: 1, 2: 5, 3: 1}, 4).plan()
        wait = next(b for b in plan.beats if b.action == ACTION_WAIT)
        self.assertEqual(wait.wall_before, wait.wall_after)
        self.assertEqual(wait.base_before, wait.base_after)
        self.assertIsNone(wait.list_index)

    def test_fractional_arrival_is_seen_at_next_integer_tick(self) -> None:
        # 证据在 1/2 刻到达：整数拍 0 尚不可用，拍 1 查询时已生效，
        # 正确策略是空等一拍再原放，而不是承担替放损失。
        session = 30
        plan = CapturePlan(self.tmp / "frac.pcapng", session)
        plan.add(Fraction(1, 2), build_screen_frame(session, 1, KIND_FULL, b"\x05"))
        artifact = write_capture(plan)
        album = build_album(artifact["pcap"], session=session, holder=self)
        self.assertEqual(album.view_at(1, Fraction(0)).status, FrameStatus.UNKNOWN)
        self.assertEqual(album.view_at(1, Fraction(1)).status, FrameStatus.COMPLETE)
        plan_out = ProjectorPlanner(album, [1], {1: 9}, 2).plan()
        self.assertEqual(plan_out.total_loss, 0)
        self.assertEqual(
            [b.action for b in plan_out.beats], [ACTION_WAIT, ACTION_ORIG]
        )

    def test_no_free_preloaded_base_at_tick_zero(self) -> None:
        album = self._album()
        # 开场直接放差片 Q：没有任何免费底，原放不可行，只能替放/画片。
        plan = ProjectorPlanner(
            album, [3], {3: 5}, 1, print_byte_budget=0
        ).plan()
        self.assertTrue(all(b.action != ACTION_ORIG for b in plan.beats))

    def test_conflicted_frame_can_only_be_replaced_and_is_reported_as_blocker(self) -> None:
        # 同一刻两份互斥全本：帧 1 开场即待核，永远不能原放/试印。
        session = 12
        plan = CapturePlan(self.tmp / "block.pcapng", session)
        plan.add(0, build_screen_frame(session, 1, KIND_FULL, b"\x01"))
        plan.add(0, build_screen_frame(session, 1, KIND_FULL, b"\x02"))
        write_capture(plan)
        album = build_album(plan.path, session, holder=self)
        self.assertEqual(album.view_at(1, Fraction(0)).status, FrameStatus.CONFLICT)
        self.assertFalse(album.chain_check(1, Fraction(5)).ok)
        self.assertEqual(album.chain_blocker(1, Fraction(5)), 1)

        # 散场 1 拍：唯一能完成片单的动作是替放馆徽（承担损失），不是原放。
        plan_out = ProjectorPlanner(album, [1], {1: 5}, 1).plan()
        self.assertTrue(plan_out.feasible)
        self.assertEqual(plan_out.total_loss, 5)
        self.assertEqual([b.action for b in plan_out.beats], [ACTION_REPLACE])

        # 散场 0 拍：物理上来不及。
        self.assertFalse(
            ProjectorPlanner(album, [1], {1: 5}, 0).plan().feasible
        )

    def test_curtain_too_tight_is_infeasible(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        album = build_album(artifact["pcap"], session=1, holder=self)
        # 三帧但散场 2：物理上每帧至少一拍，必然来不及。
        plan = ProjectorPlanner(album, [1, 2, 3], {1: 1, 2: 5, 3: 1}, 2).plan()
        self.assertFalse(plan.feasible)

    def test_printed_card_invalidated_by_later_conflict_in_chain(self) -> None:
        session = 11
        plan = CapturePlan(self.tmp / "inval.pcapng", session)
        v = build_screen_frame(session, 3, KIND_FULL, b"\x05")    # 片单首帧
        u = build_screen_frame(session, 1, KIND_FULL, b"\x07")
        q = build_screen_frame(session, 2, KIND_DELTA, b"\x03", base_no=1)
        plan.add(0, v)
        plan.add(0, u)
        plan.add(0, q)
        # 刻 1 U 出现冲突副本：Q 最早在刻 1 才会被使用，此时链已被污染。
        plan.add(1, build_screen_frame(session, 1, KIND_FULL, b"\x77"))
        write_capture(plan)
        album = build_album(plan.path, session, holder=self)
        # 排练截止刻 0 时 Q 可印。
        self.assertIn(2, album.printable_frames(Fraction(0)))
        planner = ProjectorPlanner(
            album, [3, 2], {3: 1, 2: 9}, 3,
            print_byte_budget=1, rehearsal_deadline=Fraction(0),
        )
        result = planner.plan()
        # 画片与原放都被冲突链挡住，Q 只能替放，损失 9。
        self.assertEqual(result.total_loss, 9)
        self.assertTrue(all(b.action != ACTION_PRINT for b in result.beats))
        self.assertTrue(all(
            not (b.action == ACTION_ORIG and b.frame_no == 2) for b in result.beats
        ))


# ---------------------------------------------------------------------------
# 六、端到端：独立元数据逐包对账 + 报告可 JSON 重读
# ---------------------------------------------------------------------------
class EndToEndTests(_Tmp):
    def test_meta_matches_catalog_timestamps_and_scope_flags(self) -> None:
        artifact = build_three_frame_capture(self.tmp, name="t")
        meta = json.loads(artifact["meta"].read_text(encoding="utf-8"))
        with Catalog.open(artifact["pcap"]) as catalog:
            # 文件按节分组写出，目录包序对应 (catalog_section_id, 组内顺序)；
            # 元数据按计划原始顺序记录，二者用 (节, 时间, 捕获长度) 对账。
            self.assertEqual(len(catalog.packets), len(meta["packets"]))
            cat_keys = sorted(
                (p.section_id, p.timestamp, p.captured_len, p.truncated)
                for p in catalog.packets
            )
            meta_keys = sorted(
                (
                    m["catalog_section_id"],
                    Fraction(m["expected_timestamp"]),
                    m["captured_len"],
                    m["truncated"],
                )
                for m in meta["packets"]
            )
            self.assertEqual(cat_keys, meta_keys)
            # 范围过滤标记：只有正向流的包入册。
            scoped = [m for m in meta["packets"] if m["in_scoped_flow"]]
            self.assertTrue(scoped)
            self.assertTrue(
                all(m["src_ip"] == FORWARD_SRC for m in scoped)
            )

    def test_query_report_and_plan_are_json_serializable(self) -> None:
        artifact = build_four_frame_capture(self.tmp, name="f")
        album = build_album(artifact["pcap"], session=7, holder=self)
        payload = album.query_report(Fraction(0))
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(json.loads(encoded)["at"], "0")
        plan = ProjectorPlanner(
            album, [2, 3, 4], {2: 20, 3: 5, 4: 3}, 3, print_byte_budget=1
        ).plan()
        json.dumps(plan.as_dict(), ensure_ascii=False)

    def test_wall_and_base_can_be_replayed_beat_by_beat(self) -> None:
        artifact = build_two_blocker_capture(self.tmp)
        album = build_album(artifact["pcap"], session=9, holder=self)
        plan = ProjectorPlanner(
            album, [2, 4, 5, 3], {2: 30, 4: 9, 5: 7, 3: 8}, 5, print_byte_budget=1
        ).plan()
        wall, base = WALL_LOGO, None
        shown_positions = []
        for beat in plan.beats:
            self.assertEqual(beat.wall_before, wall)
            self.assertEqual(beat.base_before, base)
            wall, base = beat.wall_after, beat.base_after
            if beat.list_index is not None:
                shown_positions.append(beat.list_index)
        self.assertEqual(shown_positions, list(range(4)))      # 片单按序完成


# ---------------------------------------------------------------------------
# 八、分层合同：新模块不直接碰文件，只经 catalog/traffic 取字节
# ---------------------------------------------------------------------------
class LayeringTests(unittest.TestCase):
    def test_album_and_projector_never_open_files(self) -> None:
        import inspect

        import pcapng_kit.album as album_mod
        import pcapng_kit.projector as projector_mod
        import pcapng_kit.frames as frames_mod

        for mod in (album_mod, projector_mod, frames_mod):
            source = inspect.getsource(mod)
            for forbidden in ("open(", "pathlib", "import os", "from os"):
                self.assertNotIn(forbidden, source, f"{mod.__name__}: {forbidden}")

    def test_dependency_direction_is_one_way(self) -> None:
        import inspect

        import pcapng_kit.catalog as catalog_mod
        import pcapng_kit.traffic as traffic_mod

        # catalog 不反向依赖上层业务模块。
        catalog_src = inspect.getsource(catalog_mod)
        for forbidden in ("album", "projector", "frames", "traffic"):
            self.assertNotIn(forbidden, catalog_src)
        # traffic 不依赖帧/收集册/播放台。
        traffic_src = inspect.getsource(traffic_mod)
        for forbidden in ("album", "projector", "frames"):
            self.assertNotIn(forbidden, traffic_src)


# ---------------------------------------------------------------------------
# 七、择优顺序与规模上限
# ---------------------------------------------------------------------------
class TieBreakAndLimitTests(_Tmp):
    def test_third_criterion_prefers_fewer_printed_bytes(self) -> None:
        # 原放与“画片”同损同散场时，应选择零印片的原放方案。
        session = 20
        plan = CapturePlan(self.tmp / "tie.pcapng", session)
        plan.add(0, build_screen_frame(session, 1, KIND_FULL, b"\x09"))
        artifact = write_capture(plan)
        album = build_album(artifact["pcap"], session=session, holder=self)
        result = ProjectorPlanner(
            album, [1], {1: 0}, 1, print_byte_budget=1
        ).plan()
        self.assertEqual((result.total_loss, result.end_tick), (0, 1))
        self.assertEqual(result.printed_bytes, 0)
        self.assertEqual([b.action for b in result.beats], [ACTION_ORIG])

    def test_at_most_four_cards_even_with_large_budget(self) -> None:
        # 6 帧差片各自需要独立底片才能原放；印片是零损替代，预算给足时
        # 优化器最多也只能印 4 张，剩余 2 帧只能替放。
        session = 21
        plan = CapturePlan(self.tmp / "cards.pcapng", session)
        # 6 张整片底片 + 6 张差片，底片号互不相同。
        for n in range(1, 7):
            plan.add(0, build_screen_frame(session, n, KIND_FULL, b"\x01"))
        for n in range(1, 7):
            plan.add(
                0,
                build_screen_frame(
                    session, 6 + n, KIND_DELTA, b"\x02", base_no=n
                ),
            )
        artifact = write_capture(plan)
        album = build_album(artifact["pcap"], session=session, holder=self)
        deltas = list(range(7, 13))
        result = ProjectorPlanner(
            album, deltas, {n: 1 for n in deltas}, 6, print_byte_budget=64
        ).plan()
        self.assertLessEqual(len(result.printed_frames), MAX_PRINT_CARDS)
        # 未被画片覆盖的差片走替放，损失等于未覆盖帧数。
        self.assertEqual(result.total_loss, 6 - len(result.printed_frames))

    def test_max_frames_and_beats_guarded(self) -> None:
        session = 22
        plan = CapturePlan(self.tmp / "big.pcapng", session)
        plan.add(0, build_screen_frame(session, 1, KIND_FULL, b"\x01"))
        artifact = write_capture(plan)
        album = build_album(artifact["pcap"], session=session, holder=self)
        with self.assertRaises(ValueError):
            ProjectorPlanner(album, list(range(21)), {}, 10)
        with self.assertRaises(ValueError):
            ProjectorPlanner(album, [1], {}, 41)


if __name__ == "__main__":
    unittest.main()
