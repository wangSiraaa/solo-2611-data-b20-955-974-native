"""借阅材料编排层：把本地 PCAPNG 的观察段无损换成学生读机的时间刻度。

本模块只消费 :class:`~pcapng_kit.catalog.Catalog` 的公开事实与
``read_body``，不重新扫描文件，也不改变目录/流量仓的行为。它负责四件事：

1. **读机接口规约**（:class:`ReaderSpec`）：唯一编号、链路类型、最大可
   保存字节数、正有理时间单位、整数秒原点。一个观察段兼容某规约，当且
   仅当链路类型相同、每包的已捕字节都装得下、每个精确时间减原点再除以
   时间单位都是 ``[0, 65535]`` 内的整数。
2. **段—规约清单**：逐段列出每个规约是否兼容、换算后的刻数，或第一笔
   不符证据（哪个出现位置、哪项检查、实际值）。
3. **前缀凭据**：只凭回读到的真实捕获字节，认定一笔正文是另一笔（更早
   出现的）正文的完整前缀，才出具可替代的源包对与可取字节区间。
4. **分册与报价**：段间分册、同段同槽、每册容量约束下先求最少册数再求
   最少总槽数；在这些排法上枚举“直接收录 / 指回更早直收行”，给出未被
   同时省字节和省走带支配的报价，并为每一对代价产出一套可实读的册。

行序（册内记录顺序）始终等于教材顺序：段序 -> 段内出现序。源节、源接口、
源包身份与原正文范围全部保留。时间换算全程使用 :class:`fractions.Fraction`，
绝不经过浮点。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from typing import Iterable, Sequence

from .catalog import Catalog, PacketRecord

TICK_MIN = 0
TICK_MAX = 65535

# 全单硬上限：最多十五段、十六条包记录、六种规约、每册四槽。
MAX_SEGMENTS = 15
MAX_PACKET_RECORDS = 16
MAX_SPECS = 6
MAX_SLOTS_PER_VOLUME = 4

# 兼容性排除证据里的检查名（确定性顺序：链路 -> 已捕字节 -> 刻数）。
CHECK_LINK_TYPE = "link_type"
CHECK_CAPTURED_LEN = "captured_len"
CHECK_TICK_NOT_INTEGER = "tick_not_integer"
CHECK_TICK_NEGATIVE = "tick_negative"
CHECK_TICK_OVERFLOW = "tick_overflow"


# ---------------------------------------------------------------------------
# 读机接口规约
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReaderSpec:
    """学生读机的一条接口规约。``time_unit`` 是每刻的秒数（正有理数）。"""

    spec_id: int
    link_type: int
    max_saved_bytes: int
    time_unit: Fraction
    origin: int = 0

    def __post_init__(self) -> None:
        unit = Fraction(self.time_unit)
        if unit <= 0:
            raise ValueError("reader spec time_unit must be a positive rational")
        if self.max_saved_bytes < 0:
            raise ValueError("reader spec max_saved_bytes must be non-negative")
        object.__setattr__(self, "time_unit", unit)

    def tick_of(self, timestamp: Fraction) -> Fraction:
        """精确时间减原点再除以时间单位（仍是精确有理数）。"""

        return (Fraction(timestamp) - Fraction(self.origin)) / self.time_unit

    def as_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "link_type": self.link_type,
            "max_saved_bytes": self.max_saved_bytes,
            "time_unit": str(self.time_unit),
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    """一笔不符证据：定位到段内出现位置与源包身份。"""

    occurrence_position: int
    source_identity: tuple[int, int, int]
    check: str
    actual: str
    expected: str

    def as_dict(self) -> dict:
        return {
            "occurrence_position": self.occurrence_position,
            "source": {
                "section_id": self.source_identity[0],
                "interface_id": self.source_identity[1],
                "packet_index": self.source_identity[2],
            },
            "check": self.check,
            "actual": self.actual,
            "expected": self.expected,
        }


@dataclass(frozen=True, slots=True)
class Compatibility:
    """段与规约的兼容结论；不兼容时给出第一笔不符证据。"""

    segment_id: int
    spec_id: int
    compatible: bool
    ticks: tuple[int, ...] | None
    evidence: Evidence | None

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "spec_id": self.spec_id,
            "compatible": self.compatible,
            "ticks": list(self.ticks) if self.ticks is not None else None,
            "first_evidence": (
                self.evidence.as_dict() if self.evidence is not None else None
            ),
        }


# ---------------------------------------------------------------------------
# 观察段与出现位置
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Occurrence:
    """某源包在教材中的一个出现位置；重复出现共享源身份、位置各自独立。"""

    segment_id: int
    position: int                 # 段内出现序（0 起）
    record_index: int             # 目录里的全文件包序号
    section_id: int
    interface_id: int
    timestamp: Fraction
    link_type: int
    captured_len: int
    orig_len: int
    truncated: bool
    body_offset: int
    body_end: int
    body: bytes                   # 真正回读到的捕获字节

    @property
    def occurrence_id(self) -> tuple[int, int]:
        return (self.segment_id, self.position)

    @property
    def source_identity(self) -> tuple[int, int, int]:
        return (self.section_id, self.interface_id, self.record_index)

    @property
    def body_range(self) -> tuple[int, int]:
        return (self.body_offset, self.body_end)

    def as_source_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "position": self.position,
            "section_id": self.section_id,
            "interface_id": self.interface_id,
            "packet_index": self.record_index,
            "body_range": [self.body_offset, self.body_end],
        }


@dataclass(slots=True)
class Segment:
    """一个原观察段：段内所有出现都来自同一节、同一接口。"""

    segment_id: int
    section_id: int
    interface_id: int
    occurrences: list[Occurrence] = field(default_factory=list)

    @property
    def link_type(self) -> int:
        return self.occurrences[0].link_type

    @property
    def packet_count(self) -> int:
        return len(self.occurrences)

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "section_id": self.section_id,
            "interface_id": self.interface_id,
            "link_type": self.link_type,
            "packet_count": self.packet_count,
        }


def build_segments(
    catalog: Catalog,
    plans: Sequence[tuple[int, int, Sequence[int]]],
) -> list[Segment]:
    """按 ``(section_id, interface_id, [目录包序号...])`` 建观察段。

    段内每个出现都必须属于指定的节和接口；同一目录包序号可按多次出现
    重复列出，各出现位置独立保留，源身份不变。正文全部经
    ``catalog.read_body`` 真正回读。
    """

    if len(plans) > MAX_SEGMENTS:
        raise ValueError(
            f"lending order allows at most {MAX_SEGMENTS} segments, got {len(plans)}"
        )
    segments: list[Segment] = []
    total_records = 0
    for segment_id, (section_id, interface_id, record_indices) in enumerate(plans):
        occurrences: list[Occurrence] = []
        for position, record_index in enumerate(record_indices):
            if not 0 <= record_index < len(catalog.packets):
                raise ValueError(f"packet index {record_index} out of range")
            record: PacketRecord = catalog.packets[record_index]
            if (record.section_id, record.interface_id) != (
                section_id,
                interface_id,
            ):
                raise ValueError(
                    f"packet {record_index} belongs to section/interface "
                    f"{(record.section_id, record.interface_id)}, not "
                    f"{(section_id, interface_id)}"
                )
            body = catalog.read_body(record)
            if len(body) != record.captured_len:
                raise ValueError(
                    f"read_body returned {len(body)} bytes, expected "
                    f"{record.captured_len} for packet {record_index}"
                )
            occurrences.append(
                Occurrence(
                    segment_id=segment_id,
                    position=position,
                    record_index=record_index,
                    section_id=record.section_id,
                    interface_id=record.interface_id,
                    timestamp=record.timestamp,
                    link_type=record.link_type,
                    captured_len=record.captured_len,
                    orig_len=record.orig_len,
                    truncated=record.truncated,
                    body_offset=record.body_offset,
                    body_end=record.body_end,
                    body=body,
                )
            )
        total_records += len(occurrences)
        if total_records > MAX_PACKET_RECORDS:
            raise ValueError(
                f"lending order allows at most {MAX_PACKET_RECORDS} packet "
                f"records, got {total_records}"
            )
        segments.append(
            Segment(segment_id, section_id, interface_id, occurrences)
        )
    return segments


# ---------------------------------------------------------------------------
# 段—规约兼容性
# ---------------------------------------------------------------------------
def _first_evidence(
    occurrence: Occurrence, spec: ReaderSpec
) -> tuple[Evidence | None, int | None]:
    """按确定顺序找第一笔不符；返回 (证据, 刻数)。"""

    if occurrence.link_type != spec.link_type:
        return (
            Evidence(
                occurrence.position,
                occurrence.source_identity,
                CHECK_LINK_TYPE,
                str(occurrence.link_type),
                str(spec.link_type),
            ),
            None,
        )
    if occurrence.captured_len > spec.max_saved_bytes:
        return (
            Evidence(
                occurrence.position,
                occurrence.source_identity,
                CHECK_CAPTURED_LEN,
                str(occurrence.captured_len),
                f"<= {spec.max_saved_bytes}",
            ),
            None,
        )
    tick = spec.tick_of(occurrence.timestamp)
    if tick.denominator != 1:
        return (
            Evidence(
                occurrence.position,
                occurrence.source_identity,
                CHECK_TICK_NOT_INTEGER,
                str(tick),
                "integer",
            ),
            None,
        )
    value = tick.numerator
    if value < TICK_MIN:
        return (
            Evidence(
                occurrence.position,
                occurrence.source_identity,
                CHECK_TICK_NEGATIVE,
                str(value),
                f">= {TICK_MIN}",
            ),
            None,
        )
    if value > TICK_MAX:
        return (
            Evidence(
                occurrence.position,
                occurrence.source_identity,
                CHECK_TICK_OVERFLOW,
                str(value),
                f"<= {TICK_MAX}",
            ),
            None,
        )
    return None, value


def check_compatibility(segment: Segment, spec: ReaderSpec) -> Compatibility:
    """判断一个观察段是否兼容某规约，并给出全部换算刻数或第一笔证据。"""

    ticks: list[int] = []
    for occurrence in segment.occurrences:
        evidence, tick = _first_evidence(occurrence, spec)
        if evidence is not None:
            return Compatibility(
                segment.segment_id, spec.spec_id, False, None, evidence
            )
        assert tick is not None
        ticks.append(tick)
    return Compatibility(
        segment.segment_id, spec.spec_id, True, tuple(ticks), None
    )


def compatibility_matrix(
    segments: Sequence[Segment], specs: Sequence[ReaderSpec]
) -> list[Compatibility]:
    """逐段逐规约的完整清单，可独立复查每一处排除。"""

    _validate_specs(specs)
    return [
        check_compatibility(segment, spec)
        for segment in segments
        for spec in specs
    ]


def _validate_specs(specs: Sequence[ReaderSpec]) -> None:
    if len(specs) > MAX_SPECS:
        raise ValueError(
            f"lending order allows at most {MAX_SPECS} specs, got {len(specs)}"
        )
    ids = [spec.spec_id for spec in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("reader spec ids must be unique")


# ---------------------------------------------------------------------------
# 教材顺序、前缀凭据
# ---------------------------------------------------------------------------
def textbook_occurrences(segments: Sequence[Segment]) -> list[Occurrence]:
    """按教材顺序（段序 -> 段内出现序）展开全部出现。"""

    return [occ for segment in segments for occ in segment.occurrences]


def _is_complete_prefix(small: bytes, big: bytes) -> bool:
    """``small`` 是否为 ``big`` 的完整前缀（空正文按真实长度 0 处理）。"""

    return len(small) <= len(big) and big[: len(small)] == small


@dataclass(frozen=True, slots=True)
class PrefixCredential:
    """一笔可替代凭据：较晚记录的正文字节等于较早记录正文的开头一段。

    仅当真正回读到的捕获字节满足“较晚正文是较早正文的完整前缀”才签发。
    ``byte_range`` 是在较早记录**直接收录字节**上的可取区间 ``[0, len)``。
    """

    earlier_ordinal: int
    later_ordinal: int
    earlier_source: tuple[int, int, int]
    later_source: tuple[int, int, int]
    byte_range: tuple[int, int]

    def as_dict(self) -> dict:
        return {
            "earlier_ordinal": self.earlier_ordinal,
            "later_ordinal": self.later_ordinal,
            "earlier_source": {
                "section_id": self.earlier_source[0],
                "interface_id": self.earlier_source[1],
                "packet_index": self.earlier_source[2],
            },
            "later_source": {
                "section_id": self.later_source[0],
                "interface_id": self.later_source[1],
                "packet_index": self.later_source[2],
            },
            "byte_range": list(self.byte_range),
        }


def prefix_credentials(segments: Sequence[Segment]) -> list[PrefixCredential]:
    """按教材顺序检查每条记录能否取自某条较早记录的字节。

    同一段内与跨段都检查；只依据回读到的捕获正文做完整前缀判定。
    """

    flat = textbook_occurrences(segments)
    credentials: list[PrefixCredential] = []
    for later_ordinal, later in enumerate(flat):
        for earlier_ordinal in range(later_ordinal):
            earlier = flat[earlier_ordinal]
            if _is_complete_prefix(later.body, earlier.body):
                credentials.append(
                    PrefixCredential(
                        earlier_ordinal=earlier_ordinal,
                        later_ordinal=later_ordinal,
                        earlier_source=earlier.source_identity,
                        later_source=later.source_identity,
                        byte_range=(0, len(later.body)),
                    )
                )
    return credentials


# ---------------------------------------------------------------------------
# 分册：段间切分 + 每册槽位最少化
# ---------------------------------------------------------------------------
def _compatible_spec_ids(segment: Segment, specs: Sequence[ReaderSpec]) -> list[int]:
    return [
        spec.spec_id
        for spec in specs
        if check_compatibility(segment, spec).compatible
    ]


def _min_spec_cover(
    group: Sequence[int],
    feasible: Sequence[list[int]],
    max_slots: int,
) -> tuple[int, dict[int, int], list[int]] | None:
    """覆盖一组段所需的最少不同规约（=槽位）数。

    同一段只能选一个兼容规约；同册里共用同一规约的段共享一个槽。
    """

    candidate = sorted({spec_id for s in group for spec_id in feasible[s]})
    for size in range(1, min(max_slots, len(candidate)) + 1):
        for subset in combinations(candidate, size):
            chosen = set(subset)
            if all(set(feasible[s]) & chosen for s in group):
                assignment = {
                    s: min(set(feasible[s]) & chosen) for s in group
                }
                distinct = sorted(set(assignment.values()))
                return len(distinct), assignment, distinct
    return None


def _contiguous_partitions(n: int) -> Iterable[list[tuple[int, int]]]:
    """把 ``range(n)`` 切成若干连续非空区间（只能在段间分册）。"""

    if n == 0:
        yield []
        return
    for end in range(1, n + 1):
        for rest in _contiguous_partitions(n - end):
            yield [(0, end)] + [(a + end, b + end) for a, b in rest]


def _optimal_packings(
    feasible: Sequence[list[int]],
    max_slots: int,
) -> tuple[int, int, list[tuple[list[tuple[int, int]], list]]]:
    """先最少册数，再最少总槽数；返回全部最优排法。"""

    n = len(feasible)
    best_volumes: int | None = None
    best_slots: int | None = None
    best: list[tuple[list[tuple[int, int]], list]] = []
    for part in _contiguous_partitions(n):
        covers = []
        total_slots = 0
        feasible_part = True
        for start, end in part:
            group = list(range(start, end))
            cover = _min_spec_cover(group, feasible, max_slots)
            if cover is None:
                feasible_part = False
                break
            covers.append((group, cover))
            total_slots += cover[0]
        if not feasible_part:
            continue
        volumes = len(part)
        if (
            best_volumes is None
            or volumes < best_volumes
            or (volumes == best_volumes and total_slots < best_slots)
        ):
            best_volumes = volumes
            best_slots = total_slots
            best = [(part, covers)]
        elif volumes == best_volumes and total_slots == best_slots:
            best.append((part, covers))
    if best_volumes is None:
        raise ValueError("no volume packing can cover all segments")
    return best_volumes, best_slots, best


# ---------------------------------------------------------------------------
# 正文策略：直收 / 回指更早直收行
# ---------------------------------------------------------------------------
def _volume_lines(
    segments: Sequence[Segment], group: Sequence[int]
) -> list[Occurrence]:
    return [occ for s in group for occ in segments[s].occurrences]


def _enumerate_volume_choices(
    lines: Sequence[Occurrence],
    tap_counts: dict[tuple[int, int], int],
) -> dict[tuple[int, int], list[int | None]]:
    """枚举一册内每行的选择：直收（None）或回指更早直收行（局部行号）。

    行号使用册内 0 基编号。对同一行，若有多个可回指的较早直收行，只需
    取最近者：字节代价相同而走带更少，更远者必被支配。
    """

    n = len(lines)
    prefix_ok = [[False] * n for _ in range(n)]
    for k in range(n):
        for i in range(k):
            if _is_complete_prefix(lines[k].body, lines[i].body):
                prefix_ok[i][k] = True

    results: dict[tuple[int, int], list[int | None]] = {}

    def walk(
        k: int,
        direct_mask: int,
        bytes_acc: int,
        travel_acc: int,
        choices: list[int | None],
    ) -> None:
        if k == n:
            key = (bytes_acc, travel_acc)
            if key not in results:
                results[key] = list(choices)
            return
        occurrence = lines[k]
        # 直收：计入已捕字节，不增加走带。
        choices.append(None)
        walk(
            k + 1,
            direct_mask | (1 << k),
            bytes_acc + occurrence.captured_len,
            travel_acc,
            choices,
        )
        choices.pop()
        # 回指最近的、可作完整前缀的较早直收行。
        target = None
        for i in range(k - 1, -1, -1):
            if (direct_mask >> i) & 1 and prefix_ok[i][k]:
                target = i
                break
        if target is not None:
            taps = tap_counts.get(occurrence.occurrence_id, 0)
            choices.append(target)
            walk(
                k + 1,
                direct_mask,
                bytes_acc,
                travel_acc + taps * 2 * (k - target),
                choices,
            )
            choices.pop()

    walk(0, 0, 0, 0, [])
    return results


def _pareto_frontier(
    costs: Iterable[tuple[int, int]],
) -> list[tuple[int, int]]:
    """保留未被“同时不省字节、不省走带”支配的代价对。"""

    unique = sorted(set(costs))
    frontier: list[tuple[int, int]] = []
    for index, (byte_a, travel_a) in enumerate(unique):
        dominated = False
        for other_index, (byte_b, travel_b) in enumerate(unique):
            if other_index == index:
                continue
            if (
                byte_b <= byte_a
                and travel_b <= travel_a
                and (byte_b < byte_a or travel_b < travel_a)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append((byte_a, travel_a))
    return frontier


# ---------------------------------------------------------------------------
# 借卷计划
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Quotation:
    """一套可实读的报价（一份 JSON 册）及其两项代价。"""

    direct_bytes: int
    tape_travel: int
    document: dict

    def as_dict(self) -> dict:
        return {
            "direct_bytes": self.direct_bytes,
            "tape_travel": self.tape_travel,
            "volume_set": self.document,
        }


@dataclass(slots=True)
class LendingPlan:
    """完整借卷单：段—规约清单、前缀凭据、最优排法与报价前沿。"""

    segments: list[Segment]
    specs: list[ReaderSpec]
    compatibility: list[Compatibility]
    credentials: list[PrefixCredential]
    min_volumes: int
    min_total_slots: int
    quotations: list[Quotation]
    tap_counts: dict[tuple[int, int], int]

    def as_dict(self) -> dict:
        return {
            "specs": [spec.as_dict() for spec in self.specs],
            "segments": [segment.as_dict() for segment in self.segments],
            "compatibility": [row.as_dict() for row in self.compatibility],
            "prefix_credentials": [c.as_dict() for c in self.credentials],
            "min_volumes": self.min_volumes,
            "min_total_slots": self.min_total_slots,
            "quotations": [q.as_dict() for q in self.quotations],
        }


def _build_quotation_document(    segments: Sequence[Segment],
    specs_by_id: dict[int, ReaderSpec],
    packing: tuple[list[tuple[int, int]], list],
    volume_choices: Sequence[dict[int, int | None]],
    direct_bytes: int,
    tape_travel: int,
    tap_counts: dict[tuple[int, int], int],
) -> dict:
    part, covers = packing
    volumes: list[dict] = []
    for volume_id, ((group, cover), choices) in enumerate(zip(covers, volume_choices)):
        _size, assignment, distinct = cover
        # 段 -> 槽号：同册内共用同一规约的段共享槽。
        slot_of_spec = {spec_id: slot for slot, spec_id in enumerate(distinct)}
        lines = _volume_lines(segments, group)
        records: list[dict] = []
        for line_no, occurrence in enumerate(lines):
            spec = specs_by_id[assignment[occurrence.segment_id]]
            tick = spec.tick_of(occurrence.timestamp)
            assert tick.denominator == 1, "incompatible segment leaked into packing"
            record = {
                "line": line_no,
                "slot": slot_of_spec[assignment[occurrence.segment_id]],
                "spec_id": spec.spec_id,
                "tick": tick.numerator,
                "segment_id": occurrence.segment_id,
                "position": occurrence.position,
                "source": {
                    "section_id": occurrence.section_id,
                    "interface_id": occurrence.interface_id,
                    "packet_index": occurrence.record_index,
                },
                "body_range": [occurrence.body_offset, occurrence.body_end],
                "orig_len": occurrence.orig_len,
                "captured_len": occurrence.captured_len,
                "truncated": occurrence.truncated,
            }
            choice = choices[line_no]
            if choice is None:
                record["mode"] = "direct"
                record["bytes_hex"] = occurrence.body.hex()
            else:
                target = lines[choice]
                assert choice < line_no
                record["mode"] = "prefix"
                record["ref_line"] = choice
                record["prefix_len"] = occurrence.captured_len
                record["prefix_credential"] = {
                    "earlier_source": {
                        "section_id": target.section_id,
                        "interface_id": target.interface_id,
                        "packet_index": target.record_index,
                    },
                    "later_source": {
                        "section_id": occurrence.section_id,
                        "interface_id": occurrence.interface_id,
                        "packet_index": occurrence.record_index,
                    },
                    "byte_range": [0, occurrence.captured_len],
                }
            records.append(record)
        volumes.append(
            {
                "volume_id": volume_id,
                "slot_count": len(distinct),
                "segments": list(group),
                "records": records,
            }
        )
    return {
        "min_volumes": len(part),
        "min_total_slots": sum(len(cover[2]) for _g, cover in covers),
        "direct_bytes": direct_bytes,
        "tape_travel": tape_travel,
        "tap_counts": [
            {"segment_id": seg, "position": pos, "count": count}
            for (seg, pos), count in sorted(tap_counts.items())
        ],
        "specs": [specs_by_id[s].as_dict() for s in sorted(specs_by_id)],
        "volumes": volumes,
    }


def quotation_cost(document: dict) -> tuple[int, int]:
    """只从成卷 JSON 本身重算两项代价：直收字节总数、点读增加总走带。

    不依赖编制期的任何内部状态，供独立复查一份报价。
    """

    tap_index = {
        (item["segment_id"], item["position"]): item["count"]
        for item in document.get("tap_counts", [])
    }
    direct_bytes = 0
    tape_travel = 0
    for volume in document["volumes"]:
        records = volume["records"]
        for record in records:
            if record["mode"] == "direct":
                direct_bytes += record["captured_len"]
                continue
            ref_line = record["ref_line"]
            if not 0 <= ref_line < record["line"]:
                raise ValueError("prefix reference must point to an earlier line")
            if records[ref_line]["mode"] != "direct":
                raise ValueError("chained back-reference is forbidden")
            taps = tap_index.get((record["segment_id"], record["position"]), 0)
            tape_travel += taps * 2 * (record["line"] - ref_line)
    return direct_bytes, tape_travel


def _validate_quotation(document: dict, max_slots: int) -> None:
    """编译期自检：成卷代价必须与记录一致，且册内行号/槽位自洽。"""

    recomputed = quotation_cost(document)
    declared = (document["direct_bytes"], document["tape_travel"])
    if recomputed != declared:
        raise AssertionError(
            f"quotation cost mismatch: document says {declared}, "
            f"records recompute {recomputed}"
        )
    for volume in document["volumes"]:
        if volume["slot_count"] > max_slots:
            raise AssertionError("volume exceeds slot capacity")
        seen_slots = {record["slot"] for record in volume["records"]}
        if len(seen_slots) > volume["slot_count"]:
            raise AssertionError("record uses a slot beyond slot_count")
    VolumeReader(document).read_all()  # 构造并复原一遍，检查回指合法。


def plan_lending(
    segments: Sequence[Segment],
    specs: Sequence[ReaderSpec],
    *,
    taps: Sequence[tuple[int, int]] | None = None,
    max_slots_per_volume: int = MAX_SLOTS_PER_VOLUME,
) -> LendingPlan:
    """编制借卷单。

    ``taps`` 是老师给的点读行序列，用原包在教材中的出现身份
    ``(segment_id, position)`` 标明，可重复；缺省为每个出现各点一次。
    """

    _validate_specs(specs)
    if not segments:
        raise ValueError("at least one segment is required")
    if max_slots_per_volume < 1:
        raise ValueError("max_slots_per_volume must be positive")
    if max_slots_per_volume > MAX_SLOTS_PER_VOLUME:
        raise ValueError(
            f"lending order allows at most {MAX_SLOTS_PER_VOLUME} slots per "
            f"volume, got {max_slots_per_volume}"
        )

    specifications = {spec.spec_id: spec for spec in specs}
    compatibility = compatibility_matrix(segments, specs)
    feasible = [
        _compatible_spec_ids(segment, specs) for segment in segments
    ]
    for segment, options in zip(segments, feasible):
        if not options:
            first = next(
                row
                for row in compatibility
                if row.segment_id == segment.segment_id
            )
            raise ValueError(
                f"segment {segment.segment_id} is incompatible with every spec; "
                f"first evidence: {first.evidence.as_dict() if first.evidence else None}"
            )

    if taps is None:
        taps = [
            (segment.segment_id, occurrence.position)
            for segment in segments
            for occurrence in segment.occurrences
        ]
    known = {
        (segment.segment_id, occurrence.position)
        for segment in segments
        for occurrence in segment.occurrences
    }
    tap_counts: dict[tuple[int, int], int] = dict(Counter(taps))
    for identity in tap_counts:
        if identity not in known:
            raise ValueError(f"tap identity {identity} is not in the textbook order")

    min_volumes, min_total_slots, packings = _optimal_packings(
        feasible, max_slots_per_volume
    )

    # 收集所有最优排法下的 (直收字节, 走带) -> 一套代表赋值。
    candidates: dict[
        tuple[int, int],
        tuple[
            tuple[list[tuple[int, int]], list],
            list[dict[int, int | None]],
        ],
    ] = {}
    for packing in packings:
        part, covers = packing
        # 逐册累加并按 Pareto 前沿剪枝：被支配的中间代价不可能进入全局前沿。
        merged: dict[tuple[int, int], list[dict[int, int | None]]] = {
            (0, 0): []
        }
        for group, _cover in covers:
            lines = _volume_lines(segments, group)
            local = _enumerate_volume_choices(lines, tap_counts)
            extended: dict[tuple[int, int], list[dict[int, int | None]]] = {}
            for (byte_a, travel_a), choices_a in merged.items():
                for (byte_b, travel_b), choice_b in local.items():
                    key = (byte_a + byte_b, travel_a + travel_b)
                    if key not in extended:
                        extended[key] = choices_a + [choice_b]
            merged = {
                cost: extended[cost] for cost in _pareto_frontier(extended.keys())
            }
        for cost, volume_choices in merged.items():
            if cost not in candidates:
                candidates[cost] = (packing, volume_choices)

    frontier = _pareto_frontier(candidates.keys())
    quotations: list[Quotation] = []
    for cost in frontier:
        packing, volume_choices = candidates[cost]
        document = _build_quotation_document(
            segments,
            specifications,
            packing,
            volume_choices,
            cost[0],
            cost[1],
            tap_counts,
        )
        _validate_quotation(document, max_slots_per_volume)
        quotations.append(Quotation(cost[0], cost[1], document))
    quotations.sort(key=lambda q: (q.direct_bytes, q.tape_travel))

    return LendingPlan(
        segments=list(segments),
        specs=list(specs),
        compatibility=compatibility,
        credentials=prefix_credentials(segments),
        min_volumes=min_volumes,
        min_total_slots=min_total_slots,
        quotations=quotations,
        tap_counts=tap_counts,
    )


# ---------------------------------------------------------------------------
# 脱离源卷的学生读机
# ---------------------------------------------------------------------------
class VolumeReader:
    """只按成卷 JSON 实际复原正文的学生读机。

    绝不接触原采集文件：直收行使用卷内字节；回指行严格回到本册更早的
    直收行，取开头 ``prefix_len`` 字节。不得链式回指、不得跨册取数。
    """

    def __init__(self, document: dict) -> None:
        self.document = document
        self._volumes = {v["volume_id"]: v for v in document["volumes"]}
        self._lookup: dict[tuple[int, int], tuple[int, int]] = {}
        for volume in document["volumes"]:
            for record in volume["records"]:
                self._lookup[(record["segment_id"], record["position"])] = (
                    volume["volume_id"],
                    record["line"],
                )

    def _record(self, volume_id: int, line_no: int) -> dict:
        volume = self._volumes[volume_id]
        if not 0 <= line_no < len(volume["records"]):
            raise ValueError(f"line {line_no} out of range in volume {volume_id}")
        return volume["records"][line_no]

    def reconstruct(self, volume_id: int, line_no: int) -> dict:
        """复原一行：返回正文、刻数、原长、已捕长度与源身份。"""

        record = self._record(volume_id, line_no)
        if record["mode"] == "direct":
            body = bytes.fromhex(record["bytes_hex"])
        elif record["mode"] == "prefix":
            ref_line = record["ref_line"]
            if ref_line >= line_no:
                raise ValueError("prefix reference must point to an earlier line")
            target = self._record(volume_id, ref_line)
            if target["mode"] != "direct":
                raise ValueError("chained back-reference is forbidden")
            target_bytes = bytes.fromhex(target["bytes_hex"])
            length = record["prefix_len"]
            if length > len(target_bytes):
                raise ValueError("prefix length exceeds referenced direct bytes")
            body = target_bytes[:length]
        else:
            raise ValueError(f"unknown record mode {record['mode']!r}")
        if len(body) != record["captured_len"]:
            raise ValueError(
                f"reconstructed {len(body)} bytes but record declares "
                f"captured_len {record['captured_len']}"
            )
        return {
            "volume_id": volume_id,
            "line": line_no,
            "slot": record["slot"],
            "tick": record["tick"],
            "orig_len": record["orig_len"],
            "captured_len": record["captured_len"],
            "truncated": record["truncated"],
            "source": dict(record["source"]),
            "segment_id": record["segment_id"],
            "position": record["position"],
            "body": body,
            "mode": record["mode"],
        }

    def read_volume(self, volume_id: int) -> list[dict]:
        volume = self._volumes[volume_id]
        return [
            self.reconstruct(volume_id, line_no)
            for line_no in range(len(volume["records"]))
        ]

    def read_all(self) -> list[dict]:
        rows: list[dict] = []
        for volume_id in sorted(self._volumes):
            rows.extend(self.read_volume(volume_id))
        return rows

    def simulate_taps(
        self, taps: Sequence[tuple[int, int]]
    ) -> dict:
        """按老师点读序列冷启动复原；返回每次点读的正文与增加的走带。"""

        events: list[dict] = []
        total_travel = 0
        for identity in taps:
            if identity not in self._lookup:
                raise ValueError(f"tap identity {identity} is not in this volume set")
            volume_id, line_no = self._lookup[identity]
            row = self.reconstruct(volume_id, line_no)
            if row["mode"] == "direct":
                travel = 0
            else:
                record = self._record(volume_id, line_no)
                travel = 2 * (line_no - record["ref_line"])
            total_travel += travel
            events.append(
                {
                    "segment_id": row["segment_id"],
                    "position": row["position"],
                    "volume_id": volume_id,
                    "line": line_no,
                    "tick": row["tick"],
                    "orig_len": row["orig_len"],
                    "captured_len": row["captured_len"],
                    "body_hex": row["body"].hex(),
                    "mode": row["mode"],
                    "travel": travel,
                }
            )
        return {"events": events, "tape_travel": total_travel}


# ---------------------------------------------------------------------------
# 源包校验（与读机分离的另一个使用者）
# ---------------------------------------------------------------------------
def verify_volume_document(catalog: Catalog, document: dict) -> dict:
    """独立核对：成卷复原是否与目录回读的源包逐字节一致。

    这是“源包校验”使用者，与 :class:`VolumeReader` 的学生读机相互独立：
    它重新经目录 ``read_body`` 取得源包字节，并核对原长、已捕长度、截断
    标记与刻数。
    """

    specs = {spec["spec_id"]: ReaderSpec(
        spec_id=spec["spec_id"],
        link_type=spec["link_type"],
        max_saved_bytes=spec["max_saved_bytes"],
        time_unit=Fraction(spec["time_unit"]),
        origin=spec["origin"],
    ) for spec in document.get("specs", [])}

    reader = VolumeReader(document)
    problems: list[str] = []
    checked = 0
    for volume in document["volumes"]:
        for record in volume["records"]:
            checked += 1
            packet_index = record["source"]["packet_index"]
            if not 0 <= packet_index < len(catalog.packets):
                problems.append(f"packet {packet_index} missing from catalog")
                continue
            source = catalog.packets[packet_index]
            expected_body = catalog.read_body(source)
            reconstructed = reader.reconstruct(volume["volume_id"], record["line"])
            if reconstructed["body"] != expected_body:
                problems.append(
                    f"volume {volume['volume_id']} line {record['line']}: body mismatch"
                )
            if record["orig_len"] != source.orig_len:
                problems.append(
                    f"volume {volume['volume_id']} line {record['line']}: orig_len mismatch"
                )
            if record["captured_len"] != source.captured_len:
                problems.append(
                    f"volume {volume['volume_id']} line {record['line']}: "
                    "captured_len mismatch"
                )
            if record["truncated"] != source.truncated:
                problems.append(
                    f"volume {volume['volume_id']} line {record['line']}: "
                    "truncation flag mismatch"
                )
            if record["tick"] != specs[record["spec_id"]].tick_of(source.timestamp):
                problems.append(
                    f"volume {volume['volume_id']} line {record['line']}: tick mismatch"
                )
    return {"checked": checked, "ok": not problems, "problems": problems}
