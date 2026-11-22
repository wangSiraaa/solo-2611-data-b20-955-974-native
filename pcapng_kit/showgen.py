"""可重读的展演小捕获档生成（测试/演示专用）。

所有时间仍走 PCAPNG 的真实时间戳（默认 10^-6 秒分辨率），生成器同时写
出独立元数据 JSON：每包的期望到达刻、方向、归属与截断长度，供测试与
收集册结果逐包对账。

文件里刻意混入三类**不得**入册的干扰包：

1. 反向返显包（源/目的对调，另一条 FlowKey）；
2. 另一节同号接口上同帧号的全本（节号不同）；
3. 头不全/魔数损坏的记录（保留出处但不能归给帧）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import frames as sf
from .generator import build_udp_frame
from .writer import InterfaceSpec, PcapNGWriter

DEFAULT_RESOLUTION_EXPONENT = 6          # 微秒：刻 t -> raw = t * 10^6
FORWARD_SRC = "198.51.100.10"
FORWARD_DST = "198.51.100.20"
FORWARD_SPORT = 4100
FORWARD_DPORT = 4200


@dataclass(slots=True)
class ShowPacket:
    """一张展演包的合成指令。"""

    tick: Fraction
    frame: bytes | None                    # 完整帧字节；None 表示头不全的碎片
    section_id: int = 0
    interface_id: int = 0
    src_ip: str = FORWARD_SRC
    dst_ip: str = FORWARD_DST
    src_port: int = FORWARD_SPORT
    dst_port: int = FORWARD_DPORT
    truncate_to: int | None = None         # 模拟漏收尾部：只保留前 N 个帧字节
    label: str = ""
    raw_override: bytes | None = None      # 故意写错魔数等坏头

    @property
    def is_forward(self) -> bool:
        return (
            self.section_id == 0
            and self.interface_id == 0
            and (self.src_ip, self.dst_ip, self.src_port, self.dst_port)
            == (FORWARD_SRC, FORWARD_DST, FORWARD_SPORT, FORWARD_DPORT)
        )


@dataclass(slots=True)
class CapturePlan:
    path: Path
    session: int
    packets: list[ShowPacket] = field(default_factory=list)

    def add(
        self,
        tick: int | Fraction,
        frame: bytes | None,
        *,
        truncate_to: int | None = None,
        label: str = "",
        **kwargs,
    ) -> ShowPacket:
        packet = ShowPacket(
            Fraction(tick), frame, truncate_to=truncate_to, label=label, **kwargs
        )
        self.packets.append(packet)
        return packet


def write_capture(plan: CapturePlan, *, ts_exponent: int = DEFAULT_RESOLUTION_EXPONENT) -> dict:
    """按计划写 PCAPNG（每节一个接口，节内接口 0）并落独立元数据。

    同节号的包在文件里成组落到同一 SHB（计划顺序在组内保持不变），目录
    节号按节号首次出现的顺序分配；元数据仍按计划原始顺序记录并标明
    ``catalog_section_id``，便于逐包对账。
    """

    path = plan.path
    path.parent.mkdir(parents=True, exist_ok=True)
    section_order: dict[int, int] = {}
    for packet in plan.packets:
        if packet.section_id not in section_order:
            section_order[packet.section_id] = len(section_order)
    grouped: dict[int, list[int]] = {sid: [] for sid in section_order}
    for order, packet in enumerate(plan.packets):
        grouped[packet.section_id].append(order)
    write_order = [order for sid in section_order for order in grouped[sid]]
    meta_by_order: dict[int, dict] = {}

    with PcapNGWriter(path) as writer:
        current_section: int | None = None
        for order in write_order:
            packet = plan.packets[order]
            if packet.section_id != current_section:
                current_section = packet.section_id
                writer.start_section("little")
                writer.write_interface(
                    InterfaceSpec(
                        ts_base=10,
                        ts_exponent=ts_exponent,
                        ts_offset=0,
                        name=f"screen-section-{current_section}",
                    )
                )
            catalog_section = section_order[packet.section_id]
            payload = packet.raw_override if packet.raw_override is not None else (
                packet.frame if packet.frame is not None else b""
            )
            udp_frame = build_udp_frame(
                packet.src_ip,
                packet.dst_ip,
                packet.src_port,
                packet.dst_port,
                payload,
            )
            raw_ts = int(packet.tick * 10**ts_exponent)
            info = writer.write_packet(
                packet.interface_id,
                raw_ts,
                udp_frame,
                truncate_to=_eth_truncate(packet, udp_frame),
            )
            meta_by_order[order] = {
                "order": order,
                "label": packet.label,
                "section_id": packet.section_id,
                "catalog_section_id": catalog_section,
                "interface_id": packet.interface_id,
                "src_ip": packet.src_ip,
                "dst_ip": packet.dst_ip,
                "src_port": packet.src_port,
                "dst_port": packet.dst_port,
                "tick": str(packet.tick),
                "ts_raw": raw_ts,
                "expected_timestamp": str(Fraction(raw_ts, 10**ts_exponent)),
                "in_scoped_flow": packet.is_forward,
                "truncated": info["truncated"],
                "captured_len": info["captured_len"],
                "orig_len": info["orig_len"],
                "block_range": info["block_range"],
                "body_range": info["body_range"],
            }
    meta = [meta_by_order[order] for order in range(len(plan.packets))]
    meta_path = path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps(
            {
                "capture": path.name,
                "session": plan.session,
                "scope": {
                    "section_id": 0,
                    "interface_id": 0,
                    "src_ip": FORWARD_SRC,
                    "dst_ip": FORWARD_DST,
                    "src_port": FORWARD_SPORT,
                    "dst_port": FORWARD_DPORT,
                },
                "packets": meta,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {"pcap": path, "meta": meta_path}


def _eth_truncate(packet: ShowPacket, udp_frame: bytes) -> int | None:
    """把“只保留帧前 N 字节”的语义换算成以太网帧捕获长度。"""

    if packet.truncate_to is None:
        return None
    # 以太网(14) + IPv4(20) + UDP(8) 之后才是展演帧。
    return 14 + 20 + 8 + packet.truncate_to


# ---------------------------------------------------------------------------
# 剧目一：三帧 A/B/C 整片（题面第一组例子）
# ---------------------------------------------------------------------------
def build_three_frame_capture(
    out_dir: str | Path,
    *,
    name: str = "three_frame",
    b_full_tick: int = 2,
    late_conflict_tick: int = 5,
) -> dict:
    out_dir = Path(out_dir)
    session = 1
    plan = CapturePlan(out_dir / f"{name}.pcapng", session)

    frame_a = sf.build_screen_frame(session, 1, sf.KIND_FULL, b"\xA1")
    frame_b = sf.build_screen_frame(session, 2, sf.KIND_FULL, b"\xB2\xB3")
    frame_c = sf.build_screen_frame(session, 3, sf.KIND_FULL, b"\xC4")

    # 刻 0：A 全本；B 只剩前半段（头 + 1/2 正文）。
    plan.add(0, frame_a, label="A full @0")
    plan.add(0, frame_b, truncate_to=sf.HEADER_SIZE + 1, label="B prefix @0")
    # 刻 2（或改期）：B 的重复全本。
    plan.add(b_full_tick, frame_b, label=f"B full retransmit @{b_full_tick}")
    # 刻 2：C 全本。
    plan.add(2, frame_c, label="C full @2")

    # 反向返显：同帧号 B 的全本，方向相反——不得补片。
    plan.add(
        1, frame_b,
        src_ip=FORWARD_DST, dst_ip=FORWARD_SRC,
        src_port=FORWARD_DPORT, dst_port=FORWARD_SPORT,
        label="reverse echo B (must be ignored)",
    )
    # 另一节同号接口上的 B 全本——节号不同，不得合并。
    plan.add(1, frame_b, section_id=1, interface_id=0,
             label="other-section same iface B (must be ignored)")
    # 头不全的碎片：保留出处，不能归给帧。
    plan.add(1, None, raw_override=b"SF\x00", label="headerless fragment")
    # 迟到的冲突副本：B 的另一份“全本”但正文不同，到达即待核。
    frame_b_alt = sf.build_screen_frame(session, 2, sf.KIND_FULL, b"\xB2\xFF")
    plan.add(late_conflict_tick, frame_b_alt,
             label=f"late conflicting B copy @{late_conflict_tick}")

    return write_capture(plan)


# ---------------------------------------------------------------------------
# 剧目二：四帧 V/U/Q/R（题面第二组例子）
# ---------------------------------------------------------------------------
def build_four_frame_capture(
    out_dir: str | Path,
    *,
    name: str = "four_frame",
    u_full_tick: int = 0,
) -> dict:
    """刻 0 默认全部收全；``u_full_tick=1`` 把 U 的完整副本挪到开场后。"""

    out_dir = Path(out_dir)
    session = 7
    plan = CapturePlan(out_dir / f"{name}.pcapng", session)

    full_u = sf.build_screen_frame(session, 1, sf.KIND_FULL, b"\x01")   # U=1
    full_v = sf.build_screen_frame(session, 2, sf.KIND_FULL, b"\x04")   # V=4
    delta_q = sf.build_screen_frame(
        session, 3, sf.KIND_DELTA, b"\x03", base_no=1
    )                                                                  # 底 U，正文3
    delta_r = sf.build_screen_frame(
        session, 4, sf.KIND_DELTA, b"\x04", base_no=3
    )                                                                  # 底 Q，正文4

    plan.add(0, full_v, label="V full @0")
    if u_full_tick == 0:
        plan.add(0, full_u, label="U full @0")
    else:
        # 排练截止前 U 只有前缀，开场后才补齐。
        plan.add(0, full_u, truncate_to=sf.HEADER_SIZE,
                label="U header-only prefix @0")
        plan.add(u_full_tick, full_u, label=f"U full moved to @{u_full_tick}")
    plan.add(0, delta_q, label="Q delta @0")
    plan.add(0, delta_r, label="R delta @0")

    # 干扰：反向返显 Q 全本，绝不能拿来补底。
    plan.add(
        0, delta_q,
        src_ip=FORWARD_DST, dst_ip=FORWARD_SRC,
        src_port=FORWARD_DPORT, dst_port=FORWARD_SPORT,
        label="reverse echo Q (must be ignored)",
    )
    return write_capture(plan)


# ---------------------------------------------------------------------------
# 剧目三：两处不同错底、一张画片、一拍现场装底的小节目
# ---------------------------------------------------------------------------
def build_two_blocker_capture(out_dir: str | Path, *, name: str = "two_blocker") -> dict:
    """帧：整片 U/V/W；差片 Q（底 U）、差片 S（底 W）。片单 V,Q,W,S。"""

    out_dir = Path(out_dir)
    session = 9
    plan = CapturePlan(out_dir / f"{name}.pcapng", session)

    full_u = sf.build_screen_frame(session, 1, sf.KIND_FULL, b"\x07")   # U=7
    full_v = sf.build_screen_frame(session, 2, sf.KIND_FULL, b"\x02")   # V=2
    full_w = sf.build_screen_frame(session, 3, sf.KIND_FULL, b"\x05")   # W=5
    delta_q = sf.build_screen_frame(
        session, 4, sf.KIND_DELTA, b"\x03", base_no=1
    )   # Q 画面 = 3 xor 7 = 4
    delta_s = sf.build_screen_frame(
        session, 5, sf.KIND_DELTA, b"\x06", base_no=3
    )   # S 画面 = 6 xor 5 = 3

    for tick, frame, label in [
        (0, full_u, "U full"),
        (0, full_v, "V full"),
        (0, full_w, "W full"),
        (0, delta_q, "Q delta base U"),
        (0, delta_s, "S delta base W"),
    ]:
        plan.add(tick, frame, label=label)

    # 反向返显 S：验证不能借返显补底。
    plan.add(
        0, delta_s,
        src_ip=FORWARD_DST, dst_ip=FORWARD_SRC,
        src_port=FORWARD_DPORT, dst_port=FORWARD_SPORT,
        label="reverse echo S (must be ignored)",
    )
    return write_capture(plan)
