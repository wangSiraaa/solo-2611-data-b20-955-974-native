"""帧收集册：在 UDP 事实仓之上按指定真实接口的一条单向流复原逐帧证据。

收集册只消费 :class:`~pcapng_kit.catalog.Catalog` 与
:class:`~pcapng_kit.traffic.TrafficStore`：

* 范围严格限定为构造时给定的（节号, 节内接口号, 五元组）。反向返显包是
  另一条 :class:`FlowKey`，另一节同号接口的节号不同，二者天然进不来；
* 题内每包从帧首字节开始承载，因此截断包只能证实一个正文前缀，填充字节
  （以太网补齐 / PCAPNG 对齐）永远不进正文；
* 固定头不全或违约的记录只保留出处，不能归给任何帧；
* 同一帧的各份前缀必须互为前缀且声明长度/种类/底片号一致；任何冲突都把
  该帧判为“待核”，绝不从矛盾副本里挑一份；
* 所有查询只看到达时刻 ``<= t`` 的证据，同一时刻的整组证据一起生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Iterator

from . import frames as sf
from .catalog import Catalog
from .frames import FrameHeaderError, xor_picture
from .traffic import FactStatus, FlowKey, PacketFact, TrafficStore

ETH_HEADER_LEN = 14
UDP_HEADER_LEN = 8


class FrameStatus(str, Enum):
    """某一刻对某帧能下的最强结论。"""

    UNKNOWN = "unknown"      # 尚无任何可归属证据
    HELD = "held"            # 证据一致但只有前缀，未达声明长度
    COMPLETE = "complete"    # 证据一致且已有一份载满声明正文
    CONFLICT = "conflict"    # 证据互相矛盾，待核，不得择一使用


@dataclass(frozen=True, slots=True)
class AlbumScope:
    """一条单向展演流的真实身份：节/接口 + 五元组 + 场次号。"""

    section_id: int
    interface_id: int
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    session: int

    @property
    def flow_key(self) -> FlowKey:
        return FlowKey(
            self.section_id,
            self.interface_id,
            self.src_ip,
            self.dst_ip,
            self.src_port,
            self.dst_port,
        )


@dataclass(frozen=True, slots=True)
class CopyEvidence:
    """一份可归属到某帧的旁录副本（可能只承载正文前缀）。"""

    packet_index: int
    timestamp: Fraction
    frame_no: int
    kind: int
    declared_len: int
    base_no: int
    prefix: bytes
    complete: bool                       # 前缀是否已载满声明正文
    truncated: bool
    block_range: tuple[int, int]
    body_range: tuple[int, int]

    @property
    def readable_len(self) -> int:
        return len(self.prefix)


@dataclass(frozen=True, slots=True)
class HeaderlessRecord:
    """头不全/无法识别的记录：保留出处，不归属任何帧。"""

    packet_index: int
    timestamp: Fraction
    reason: str
    available_payload_bytes: int
    block_range: tuple[int, int]
    body_range: tuple[int, int]


@dataclass(slots=True)
class FrameView:
    """某一刻某帧的可读结论（仅使用当时已到的证据）。"""

    frame_no: int
    status: FrameStatus
    kind: int | None
    declared_len: int | None
    base_no: int | None
    readable_len: int
    readable_bytes: bytes
    reason: str
    evidence_indices: list[int]

    def as_dict(self) -> dict:
        return {
            "frame_no": self.frame_no,
            "status": self.status.value,
            "kind": None if self.kind is None else sf.KIND_NAMES[self.kind],
            "declared_len": self.declared_len,
            "base_no": self.base_no,
            "readable_len": self.readable_len,
            "readable_bytes_hex": self.readable_bytes.hex(),
            "reason": self.reason,
            "evidence_packet_indices": list(self.evidence_indices),
        }


@dataclass(frozen=True, slots=True)
class RenderResult:
    """整条底片链试印结果；失败时 ``blocker`` 给出阻断帧与原因。"""

    ok: bool
    picture: bytes | None
    blocker: int | None
    reason: str
    chain_frames: tuple[int, ...] = ()


class FrameAlbum:
    """逐帧证据册：构建一次，随后按任意时刻反复查询。"""

    def __init__(self, catalog: Catalog, store: TrafficStore, scope: AlbumScope) -> None:
        self.catalog = catalog
        self.store = store
        self.scope = scope
        self.headerless: list[HeaderlessRecord] = []
        self.copies: dict[int, list[CopyEvidence]] = {}
        self._timeline: list[dict] = []
        self._build()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls, catalog: Catalog, store: TrafficStore, scope: AlbumScope
    ) -> "FrameAlbum":
        return cls(catalog, store, scope)

    def _in_scope(self, fact: PacketFact) -> bool:
        """节/接口必须精确命中；流键可定时比五元组，截断包比可得字段。"""

        if (fact.section_id, fact.interface_id) != (
            self.scope.section_id,
            self.scope.interface_id,
        ):
            return False
        if fact.flow is not None:
            return fact.flow == self.scope.flow_key
        # 截断到流键无法确定时，只接受所有可得地址字段都不与范围矛盾的包。
        if fact.src_ip is not None and fact.src_ip != self.scope.src_ip:
            return False
        if fact.dst_ip is not None and fact.dst_ip != self.scope.dst_ip:
            return False
        if fact.udp_src_port is not None and fact.udp_src_port != self.scope.src_port:
            return False
        if fact.udp_dst_port is not None and fact.udp_dst_port != self.scope.dst_port:
            return False
        return True

    def _payload_bytes(self, fact: PacketFact) -> bytes | None:
        """经目录 API 回读捕获正文，再切出 UDP 正文起点之后的实得字节。"""

        body = self.catalog.read_body(fact.record)
        if fact.ip_header_len is None:
            return None
        payload_start = ETH_HEADER_LEN + fact.ip_header_len + UDP_HEADER_LEN
        if len(body) <= payload_start:
            return b""
        captured_tail = body[payload_start:]
        if fact.status is FactStatus.OK and fact.udp_payload_len is not None:
            # 完整包：按 UDP 长度字段精确切，以太网补齐字节不算正文。
            return captured_tail[: fact.udp_payload_len]
        return captured_tail

    def _build(self) -> None:
        events: list[tuple[Fraction, int, object]] = []
        for fact in self.store:
            if not self._in_scope(fact):
                continue
            provenance = (
                fact.record.block_offset,
                fact.record.block_end,
                fact.record.body_offset,
                fact.record.body_end,
            )
            payload = self._safe_payload(fact)
            if payload is None:
                self.headerless.append(
                    HeaderlessRecord(
                        fact.index, fact.timestamp,
                        f"payload location undeterminable: {fact.reason}",
                        fact.captured_len,
                        provenance[0:2], provenance[2:4],
                    )
                )
                events.append((fact.timestamp, fact.index, ("headerless", fact.index)))
                continue
            try:
                header = sf.parse_screen_header(payload, session=self.scope.session)
            except FrameHeaderError as exc:
                self.headerless.append(
                    HeaderlessRecord(
                        fact.index, fact.timestamp, str(exc), len(payload),
                        provenance[0:2], provenance[2:4],
                    )
                )
                events.append((fact.timestamp, fact.index, ("headerless", fact.index)))
                continue
            prefix = sf.extract_prefix(payload, header)
            evidence = CopyEvidence(
                packet_index=fact.index,
                timestamp=fact.timestamp,
                frame_no=header.frame_no,
                kind=header.kind,
                declared_len=header.declared_len,
                base_no=header.base_no,
                prefix=prefix,
                complete=len(prefix) == header.declared_len,
                truncated=fact.record.truncated,
                block_range=provenance[0:2],
                body_range=provenance[2:4],
            )
            self.copies.setdefault(header.frame_no, []).append(evidence)
            events.append((fact.timestamp, fact.index, ("copy", evidence)))

        # 同一真实时刻的证据整组生效：先按（时刻, 源包序号）排序再分组。
        events.sort(key=lambda item: (item[0], item[1]))
        grouped: dict[Fraction, list[object]] = {}
        for ts, _idx, event in events:
            grouped.setdefault(ts, []).append(event)
        for ts in sorted(grouped):
            self._timeline.append({"timestamp": ts, "events": grouped[ts]})

    def _safe_payload(self, fact: PacketFact) -> bytes | None:
        if fact.status is FactStatus.READ_ERROR:
            return None
        return self._payload_bytes(fact)

    # ------------------------------------------------------------------
    # 时刻查询
    # ------------------------------------------------------------------
    def _evidence_at(self, frame_no: int, at: Fraction) -> list[CopyEvidence]:
        return [
            copy
            for copy in self.copies.get(frame_no, [])
            if copy.timestamp <= at
        ]

    @staticmethod
    def _prefix_compatible(a: bytes, b: bytes) -> bool:
        """互为前缀：短者必须与长者的同长开头逐字节相同。"""

        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        return long_.startswith(short)

    def view_at(self, frame_no: int, at: Fraction) -> FrameView:
        evidence = self._evidence_at(frame_no, at)
        if not evidence:
            return FrameView(
                frame_no, FrameStatus.UNKNOWN, None, None, None,
                0, b"", "no attributable copy has arrived by this time", [],
            )
        indices = [copy.packet_index for copy in evidence]
        first = evidence[0]
        kinds = {copy.kind for copy in evidence}
        declared = {copy.declared_len for copy in evidence}
        bases = {copy.base_no for copy in evidence}

        # 声明字段冲突：种类、声明长度、头里的底片身份都是旁录证据。
        if len(kinds) > 1:
            return self._conflict_view(frame_no, evidence, "frame kind declarations differ")
        if len(declared) > 1:
            return self._conflict_view(
                frame_no, evidence, "declared body lengths differ across copies"
            )
        if len(bases) > 1:
            return self._conflict_view(
                frame_no, evidence, "header base identities differ across copies"
            )

        # 前缀互为前缀校验；同长度副本内容不同也在此暴露。
        longest = first.prefix
        for copy in evidence[1:]:
            if not self._prefix_compatible(longest, copy.prefix):
                return self._conflict_view(
                    frame_no, evidence,
                    "copy prefixes disagree at the same byte position",
                )
            if len(copy.prefix) > len(longest):
                longest = copy.prefix

        declared_len = first.declared_len
        if len(longest) == declared_len:
            return FrameView(
                frame_no, FrameStatus.COMPLETE, first.kind, declared_len,
                first.base_no, declared_len, longest,
                f"consistent copies cover all {declared_len} declared body byte(s)",
                indices,
            )
        return FrameView(
            frame_no, FrameStatus.HELD, first.kind, declared_len, first.base_no,
            len(longest), longest,
            f"only {len(longest)}/{declared_len} declared body byte(s) confirmed; "
            "tail not yet received",
            indices,
        )

    def _conflict_view(
        self, frame_no: int, evidence: list[CopyEvidence], reason: str
    ) -> FrameView:
        first = evidence[0]
        return FrameView(
            frame_no, FrameStatus.CONFLICT, first.kind, first.declared_len,
            first.base_no, 0, b"",
            f"evidence conflict ({reason}); frame held for review, no copy favoured",
            [copy.packet_index for copy in evidence],
        )

    def snapshot(self, at: Fraction) -> dict[int, FrameView]:
        """该时刻所有出现过的帧的结论。"""

        frame_nos = sorted(
            frame_no
            for frame_no, copies in self.copies.items()
            if any(copy.timestamp <= at for copy in copies)
        )
        return {frame_no: self.view_at(frame_no, at) for frame_no in frame_nos}

    def is_clean_complete(self, frame_no: int, at: Fraction) -> bool:
        return self.view_at(frame_no, at).status is FrameStatus.COMPLETE

    # ------------------------------------------------------------------
    # 底片链与试印
    # ------------------------------------------------------------------
    def chain_check(self, frame_no: int, at: Fraction) -> RenderResult:
        """沿头中底片号递归检查整条链并试印真实画面。

        只认收集册自身证据：反向返显包从未入册，不可能被拿来补底。
        """

        return self._render(frame_no, at, set())

    def _render(
        self, frame_no: int, at: Fraction, stack: set[int]
    ) -> RenderResult:
        if frame_no in stack:
            return RenderResult(
                False, None, frame_no, "base chain cycles back", tuple(stack),
            )
        view = self.view_at(frame_no, at)
        if view.status is FrameStatus.CONFLICT:
            return RenderResult(
                False, None, frame_no,
                "frame evidence is in conflict and pending review",
                tuple(stack) + (frame_no,),
            )
        if view.status is not FrameStatus.COMPLETE:
            return RenderResult(
                False, None, frame_no,
                f"frame is {view.status.value}, not fully confirmed",
                tuple(stack) + (frame_no,),
            )
        chain = tuple(stack) + (frame_no,)
        if view.kind == sf.KIND_FULL:
            return RenderResult(
                True, view.readable_bytes, None, "full frame", chain_frames=chain
            )
        base_no = view.base_no
        if base_no is None or base_no >= frame_no:
            return RenderResult(
                False, None, frame_no, "header does not name an earlier base",
                chain_frames=chain,
            )
        # 底片链独立递归：它的链由自己的 stack 决定，不混入本帧以上的栈。
        base_result = self._render(base_no, at, set())
        if not base_result.ok:
            return base_result
        # 完整底片链 = 底片的链（按由早到晚）再接本帧。
        full_chain = base_result.chain_frames + (frame_no,)
        try:
            picture = xor_picture(view.readable_bytes, base_result.picture)
        except ValueError as exc:
            return RenderResult(False, None, frame_no, str(exc), chain_frames=chain)
        return RenderResult(
            True, picture, None, "delta chain rendered", chain_frames=full_chain
        )

    def picture_at(self, frame_no: int, at: Fraction) -> RenderResult:
        return self.chain_check(frame_no, at)

    def chain_blocker(self, frame_no: int, at: Fraction) -> int | None:
        result = self.chain_check(frame_no, at)
        return None if result.ok else result.blocker

    # ------------------------------------------------------------------
    # 排练印片
    # ------------------------------------------------------------------
    def printable_frames(self, deadline: Fraction) -> list[int]:
        """排练截止时整条链完整且无冲突的帧，才有资格做成实物画片。"""

        result = []
        for frame_no in sorted(self.copies):
            rendered = self.chain_check(frame_no, deadline)
            if rendered.ok:
                result.append(frame_no)
        return result

    def printed_bytes(self, frame_nos: set[int], deadline: Fraction) -> int:
        """实际所印帧字节数（画面与正文等长；来历作为元数据附带）。"""

        total = 0
        for frame_no in frame_nos:
            view = self.view_at(frame_no, deadline)
            if view.status is not FrameStatus.COMPLETE:
                raise ValueError(f"frame {frame_no} is not printable at deadline")
            total += view.declared_len
        return total

    def card_provenance(self, frame_nos: set[int], deadline: Fraction) -> dict[int, list[int]]:
        """每张画片整条来历上各帧用到的源包序号（采集证据）。"""

        provenance: dict[int, list[int]] = {}
        for frame_no in sorted(frame_nos):
            rendered = self.chain_check(frame_no, deadline)
            if not rendered.ok:
                raise ValueError(
                    f"frame {frame_no} chain blocked at {rendered.blocker}"
                )
            packets: list[int] = []
            for chained in rendered.chain_frames:
                packets.extend(
                    copy.packet_index
                    for copy in self._evidence_at(chained, deadline)
                )
            provenance[frame_no] = sorted(set(packets))
        return provenance

    # ------------------------------------------------------------------
    # 确认过程时间线与序列化
    # ------------------------------------------------------------------
    def arrival_times(self) -> list[Fraction]:
        return [entry["timestamp"] for entry in self._timeline]

    def timeline(self) -> list[dict]:
        """按真实时间列出每组证据到达后各相关帧的确认过程。"""

        report: list[dict] = []
        for entry in self._timeline:
            ts = entry["timestamp"]
            touched: set[int] = set()
            headerless_indices: list[int] = []
            for event in entry["events"]:
                kind, payload = event
                if kind == "copy":
                    touched.add(payload.frame_no)
                else:
                    headerless_indices.append(payload)
            report.append(
                {
                    "timestamp": str(ts),
                    "headerless_packet_indices": headerless_indices,
                    "frames": {
                        frame_no: self.view_at(frame_no, ts).as_dict()
                        for frame_no in sorted(touched)
                    },
                }
            )
        return report

    def headerless_listing(self) -> list[dict]:
        return [
            {
                "packet_index": record.packet_index,
                "timestamp": str(record.timestamp),
                "reason": record.reason,
                "available_payload_bytes": record.available_payload_bytes,
                "block_range": list(record.block_range),
                "body_range": list(record.body_range),
            }
            for record in self.headerless
        ]

    def query_report(self, at: Fraction) -> dict:
        """某一查询时刻的完整可读报告。"""

        return {
            "at": str(at),
            "frames": {
                str(frame_no): view.as_dict()
                for frame_no, view in sorted(self.snapshot(at).items())
            },
            "headerless_records": self.headerless_listing(),
        }

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.copies))

    def __len__(self) -> int:
        return len(self.copies)
