"""借阅演示用的合成 PCAPNG（复用母项目写包能力）。

两套样例严格对应题目要求：

* :func:`build_demo_a` —— 两个节内接口都是 0 的段，各一包、正文相同；
  第一段 1/2 秒、第二段 1/5 秒。``calibrate_second_to_one`` 只把第二段
  源时间校为 1 秒。另有 ``truncated`` 选项追加一个线上原长大于已捕长度
  的真实记录。
* :func:`build_demo_b` —— 同一段内三条记录，实际捕获的四字节相同，线上
  原长分别 4/6/4；``third_first_byte`` 只改第 3 包已捕首字节。

所有帧都用 :class:`~pcapng_kit.writer.PcapNGWriter` 写出，时间戳由原始
整数与接口合同直接决定，便于独立对账。
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from .blocks import LINKTYPE_ETHERNET
from .lending import ReaderSpec
from .writer import InterfaceSpec, PcapNGWriter

DEMO_A_BODY = b"synthetic-shared-body"
DEMO_B_CAPTURED = b"ABCD"          # 三包实际捕获的四字节完全相同
DEMO_B_SECOND_FULL = b"ABCDEF"     # 第 2 包线上原长 6，仅捕获前 4 字节


def _write_segment_header(writer: PcapNGWriter, *, ts_base: int, ts_exponent: int) -> None:
    writer.start_section("little")
    writer.write_interface(
        InterfaceSpec(
            link_type=LINKTYPE_ETHERNET,
            snaplen=0,
            ts_base=ts_base,
            ts_exponent=ts_exponent,
            ts_offset=0,
            name="if0",
        )
    )


def build_demo_a(
    out_dir: str | Path,
    *,
    calibrate_second_to_one: bool = False,
    add_truncated: bool = False,
    name: str = "lending_a.pcapng",
) -> dict:
    """两段各一包、正文相同；接口均在各自节内为 0。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name

    # 第一段：2^-1 秒分辨率，raw=1 -> 1/2 秒。
    # 第二段：10^-1 秒分辨率，raw=2 -> 1/5 秒；校准后 raw=10 -> 1 秒。
    second_raw = 10 if calibrate_second_to_one else 2
    with PcapNGWriter(path) as writer:
        _write_segment_header(writer, ts_base=2, ts_exponent=1)
        first = writer.write_packet(0, 1, DEMO_A_BODY)
        _write_segment_header(writer, ts_base=10, ts_exponent=1)
        second = writer.write_packet(0, second_raw, DEMO_A_BODY)
        truncated = None
        if add_truncated:
            # 第 3 段：线上原长 60、实际只捕 20 字节；时间仍为 1/2 秒。
            _write_segment_header(writer, ts_base=2, ts_exponent=1)
            long_body = bytes(range(60))
            truncated = writer.write_packet(
                0, 1, long_body, orig_len=60, truncate_to=20
            )

    second_timestamp = Fraction(1) if calibrate_second_to_one else Fraction(1, 5)
    return {
        "path": path,
        "segments": [(0, 0, [0]), (1, 0, [1])]
        + ([(2, 0, [2])] if add_truncated else []),
        "first_timestamp": Fraction(1, 2),
        "second_timestamp": second_timestamp,
        "shared_body": DEMO_A_BODY,
        "truncated": truncated,
        "packets": {"first": first, "second": second},
    }


def build_demo_b(
    out_dir: str | Path,
    *,
    third_first_byte: bytes | None = None,
    name: str = "lending_b.pcapng",
) -> dict:
    """同一段内三条记录，实际捕获四字节相同；原长分别 4/6/4。"""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    third_captured = DEMO_B_CAPTURED if third_first_byte is None else (
        third_first_byte + DEMO_B_CAPTURED[1:]
    )
    if len(third_captured) != 4:
        raise ValueError("third_first_byte must be a single byte")

    with PcapNGWriter(path) as writer:
        _write_segment_header(writer, ts_base=10, ts_exponent=0)
        p0 = writer.write_packet(0, 1, DEMO_B_CAPTURED)               # 原长 4
        p1 = writer.write_packet(0, 2, DEMO_B_SECOND_FULL, truncate_to=4)  # 原长 6
        p2 = writer.write_packet(0, 3, third_captured)                # 原长 4
    return {
        "path": path,
        "segments": [(0, 0, [0, 1, 2])],
        "captured_bodies": [
            DEMO_B_CAPTURED,
            DEMO_B_CAPTURED,
            third_captured,
        ],
        "orig_lens": [4, 6, 4],
        "packets": [p0, p1, p2],
    }


def encoding_specs() -> list:
    """Demo A 的两个原点为零规约：刻度分别 1/2 秒与 1/5 秒。"""

    return [
        ReaderSpec(
            spec_id=0,
            link_type=LINKTYPE_ETHERNET,
            max_saved_bytes=256,
            time_unit=Fraction(1, 2),
            origin=0,
        ),
        ReaderSpec(
            spec_id=1,
            link_type=LINKTYPE_ETHERNET,
            max_saved_bytes=256,
            time_unit=Fraction(1, 5),
            origin=0,
        ),
    ]


def single_spec(*, max_saved_bytes: int = 256, time_unit: Fraction = Fraction(1)) -> list:
    return [
        ReaderSpec(
            spec_id=0,
            link_type=LINKTYPE_ETHERNET,
            max_saved_bytes=max_saved_bytes,
            time_unit=time_unit,
            origin=0,
        )
    ]


def build_plan_json(
    segments: list[tuple[int, int, list[int]]],
    *,
    taps: list[tuple[int, int]] | None = None,
    max_slots_per_volume: int = 4,
    specs_json: list[dict] | None = None,
) -> dict:
    """把段计划转成可供 ``lending_list.py`` 读取的 JSON 结构。"""

    return {
        "segments": [
            {"section_id": s, "interface_id": i, "packets": list(p)}
            for s, i, p in segments
        ],
        "specs": specs_json
        if specs_json is not None
        else [
            {
                "spec_id": 0,
                "link_type": LINKTYPE_ETHERNET,
                "max_saved_bytes": 256,
                "time_unit": "1/2",
                "origin": 0,
            }
        ],
        "taps": [list(t) for t in taps] if taps is not None else None,
        "max_slots_per_volume": max_slots_per_volume,
    }
