"""小型合成展演帧（ScreenFrame）格式合同（纯函数，不碰文件）。

一个展演帧封装在单个 UDP 正文里，**题内每包都从该帧首字节开始承载**，
因此不存在跨包拼接，截断包只能证实一个正文前缀。固定头 10 字节、
大端解释：

========  ======  =====================================================
偏移      长度    字段
========  ======  =====================================================
0..1      2       帧魔数 ``SF``（0x5346）
2..3      2       场次号（u16）
4..5      2       帧号（u16，场内唯一；底片号必须小于本帧号）
6         1       种类：0=整片，1=差片
7         1       声明正文字节数（本题合同只允许 1..4）
8..9      2       唯一底片号；整片固定为 0xFFFF（无底），差片为较早帧号
========  ======  =====================================================

头之后紧接 ``body_len`` 个正文字节；差片画面 = 正文逐字节异或底片画面，
整片画面即正文本身。任何填充（以太网/PCAPNG 对齐）都不算正文。
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_MAGIC = b"SF"
FRAME_MAGIC_VALUE = 0x5346
HEADER_SIZE = 10
KIND_FULL = 0
KIND_DELTA = 1
KIND_NAMES = {KIND_FULL: "full", KIND_DELTA: "delta"}
NO_BASE = 0xFFFF
MIN_BODY_BYTES = 1
MAX_BODY_BYTES = 4          # 本题小片正文只占 1..4 字节


class FrameHeaderError(ValueError):
    """固定头不完整或字段违反合同；该记录保留出处但不能归给任何帧。"""


@dataclass(frozen=True, slots=True)
class ScreenHeader:
    """完整识别头：场次、帧号、种类、声明正文字节数与唯一底片号。"""

    session: int
    frame_no: int
    kind: int
    declared_len: int
    base_no: int

    @property
    def is_full(self) -> bool:
        return self.kind == KIND_FULL

    @property
    def is_delta(self) -> bool:
        return self.kind == KIND_DELTA


def build_screen_frame(
    session: int,
    frame_no: int,
    kind: int,
    body: bytes,
    base_no: int = NO_BASE,
) -> bytes:
    """按合同构造一个完整帧（头 + 正文），供合成捕获档使用。"""

    if not 0 <= session <= 0xFFFF:
        raise ValueError("session must fit in u16")
    if not 0 < frame_no <= 0xFFFF:
        raise ValueError("frame_no must fit in u16 and be positive")
    if kind not in (KIND_FULL, KIND_DELTA):
        raise ValueError(f"unknown frame kind {kind}")
    if not MIN_BODY_BYTES <= len(body) <= MAX_BODY_BYTES:
        raise ValueError("body must occupy 1..4 bytes under this contract")
    if kind == KIND_FULL:
        if base_no != NO_BASE:
            raise ValueError("full frame must not name a base")
    else:
        if not 0 < base_no < frame_no:
            # 底片必须是同场较早帧；编号关系本身就是头合同的一部分。
            raise ValueError("delta base must be an earlier frame in the same session")
    head = (
        FRAME_MAGIC_VALUE.to_bytes(2, "big")
        + session.to_bytes(2, "big")
        + frame_no.to_bytes(2, "big")
        + bytes([kind, len(body)])
        + base_no.to_bytes(2, "big")
    )
    return head + body


def parse_screen_header(payload: bytes, *, session: int) -> ScreenHeader:
    """只依据已捕获的 UDP 正文字节识别固定头。

    字节不足、魔数不对、种类/长度非法、底片关系违约或场次不符都抛
    :class:`FrameHeaderError`；调用方负责保留出处且不得把该记录归给帧。
    """

    if len(payload) < HEADER_SIZE:
        raise FrameHeaderError(
            f"screen header incomplete: {len(payload)} < {HEADER_SIZE} payload bytes"
        )
    if payload[0:2] != FRAME_MAGIC:
        raise FrameHeaderError(f"bad frame magic {payload[0:2].hex()}")
    sid = int.from_bytes(payload[2:4], "big")
    if sid != session:
        raise FrameHeaderError(f"session {sid} != scoped session {session}")
    frame_no = int.from_bytes(payload[4:6], "big")
    kind = payload[6]
    declared_len = payload[7]
    base_no = int.from_bytes(payload[8:10], "big")
    if kind not in (KIND_FULL, KIND_DELTA):
        raise FrameHeaderError(f"unknown frame kind {kind}")
    if not MIN_BODY_BYTES <= declared_len <= MAX_BODY_BYTES:
        raise FrameHeaderError(f"declared body length {declared_len} outside 1..4")
    if kind == KIND_FULL and base_no != NO_BASE:
        raise FrameHeaderError("full frame must not name a base")
    if kind == KIND_DELTA and base_no == NO_BASE:
        raise FrameHeaderError("delta frame must name a base frame")
    # 注意：差片底片“必须是较早帧”属于帧间证据关系，不在单包头解析阶段
    # 拒绝——否则两份同帧、底片声明不同的副本会被当成坏头而绕过冲突待核。
    return ScreenHeader(sid, frame_no, kind, declared_len, base_no)


def extract_prefix(payload: bytes, header: ScreenHeader) -> bytes:
    """从一份已捕获正文里切出能证实的帧正文前缀（填充永不进入）。"""

    available = len(payload) - HEADER_SIZE
    if available < 0:
        available = 0
    return payload[HEADER_SIZE: HEADER_SIZE + min(available, header.declared_len)]


def xor_picture(body: bytes, base_picture: bytes) -> bytes:
    """差片试印：正文与底片画面逐字节异或；长度不一致不允许猜。"""

    if len(body) != len(base_picture):
        raise ValueError(
            f"delta body {len(body)} bytes != base picture {len(base_picture)} bytes"
        )
    return bytes(a ^ b for a, b in zip(body, base_picture))
