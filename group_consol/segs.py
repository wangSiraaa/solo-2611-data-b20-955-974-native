"""件号连续段（闭区间）运算。

单票可达百万件，航次只登记连续段，因此任何件集合都用若干闭区间
``(lo, hi)``（含两端）表示，绝不为逐件展开。这里提供规范合并、差集、
交集、子集判断与计数。
"""

from __future__ import annotations

from typing import Iterable

Segments = list[tuple[int, int]]


def normalize(segments: Iterable) -> Segments:
    """排序并合并重叠/相邻区间，返回规范的不相交升序区间表。"""
    segs = sorted((int(lo), int(hi)) for lo, hi in segments)
    out: Segments = []
    for lo, hi in segs:
        if lo > hi:
            raise ValueError(f"非法区间 [{lo}, {hi}]")
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def has_overlap(segments: Iterable) -> bool:
    """区间之间是否有公共件（相邻不算重叠）。"""
    segs = sorted((int(lo), int(hi)) for lo, hi in segments)
    for i in range(1, len(segs)):
        if segs[i][0] <= segs[i - 1][1]:
            return True
    return False


def count(segments: Iterable) -> int:
    return sum(hi - lo + 1 for lo, hi in normalize(segments))


def subtract(a: Iterable, b: Iterable) -> Segments:
    """a \\ b：从 a 中去掉 b 覆盖的件。"""
    b_norm = normalize(b)
    out: Segments = []
    for lo, hi in normalize(a):
        cur = lo
        for blo, bhi in b_norm:
            if bhi < cur:
                continue
            if blo > hi:
                break
            if blo > cur:
                out.append((cur, min(hi, blo - 1)))
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append((cur, hi))
    return out


def intersect(a: Iterable, b: Iterable) -> Segments:
    b_norm = normalize(b)
    out: Segments = []
    for lo, hi in normalize(a):
        for blo, bhi in b_norm:
            if bhi < lo:
                continue
            if blo > hi:
                break
            left, right = max(lo, blo), min(hi, bhi)
            if left <= right:
                out.append((left, right))
    return normalize(out)


def is_subset(a: Iterable, b: Iterable) -> bool:
    """a 的每一件是否都被 b 覆盖。"""
    b_norm = normalize(b)
    for lo, hi in normalize(a):
        if not any(blo <= lo and hi <= bhi for blo, bhi in b_norm):
            return False
    return True


def within(segments: Iterable, lo: int, hi: int) -> bool:
    """是否全部落在 [lo, hi] 内。"""
    return all(lo <= a and b <= hi for a, b in normalize(segments))
