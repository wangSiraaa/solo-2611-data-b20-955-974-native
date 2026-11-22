"""一维直线上的精确闭区间算术。

全部端点都是 :class:`fractions.Fraction`，区间一律为闭区间；孤立点表示为
端点相同的退化区间 ``[a, a]``。本模块是计量发行物唯一的数值计算内核：

* 不允许任何浮点采样；
* 不允许“只核铭牌两端”代替整段承诺，整段签发靠区间包含关系判定；
* 端点完整保留（含孤立点），合并时只有相邻/相交才会粘成一段。
"""

from fractions import Fraction

from ..rationals import qstr


# --------------------------------------------------------------- 基础结构
class Interval:
    """闭区间 ``[lo, hi]``，``lo == hi`` 即孤立点。"""

    __slots__ = ("lo", "hi")

    def __init__(self, lo, hi):
        lo = Fraction(lo)
        hi = Fraction(hi)
        if lo > hi:
            raise ValueError(f"区间下确界 {lo} 大于上确界 {hi}")
        self.lo = lo
        self.hi = hi

    @property
    def is_point(self):
        return self.lo == self.hi

    def contains(self, value):
        value = Fraction(value)
        return self.lo <= value <= self.hi

    def intersect(self, other):
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo <= hi:
            return Interval(lo, hi)
        return None

    def __repr__(self):
        return f"Interval({qstr(self.lo)}, {qstr(self.hi)})"

    def __eq__(self, other):
        return isinstance(other, Interval) and self.lo == other.lo and self.hi == other.hi

    def __hash__(self):
        return hash((self.lo, self.hi))

    def to_json(self):
        return {"lo": qstr(self.lo), "hi": qstr(self.hi), "point": self.is_point}

    @staticmethod
    def from_json(obj):
        return Interval(Fraction(obj["lo"]), Fraction(obj["hi"]))


def merge_intervals(intervals):
    """把一组闭区间合并成互不相交、按左端点排序的最小区间序列。

    仅当两段相交或**端点相接**（前段 hi == 后段 lo，同一直线上无空隙）才合并；
    孤立点天然作为退化闭区间参与，不会被丢弃。
    """
    items = sorted((Interval(i.lo, i.hi) for i in intervals), key=lambda i: (i.lo, i.hi))
    merged = []
    for item in items:
        if not merged:
            merged.append(item)
            continue
        last = merged[-1]
        if item.lo <= last.hi:  # 相交或端点相接
            merged[-1] = Interval(last.lo, max(last.hi, item.hi))
        else:
            merged.append(item)
    return merged


def intersect_unions(unions):
    """若干“闭区间并集”的交集，结果仍是闭区间并集（可能为空）。"""
    unions = [list(u) for u in unions]
    if any(not u for u in unions):
        return []
    result = list(unions[0])
    for other in unions[1:]:
        nxt = []
        for a in result:
            for b in other:
                hit = a.intersect(b)
                if hit is not None:
                    nxt.append(hit)
        result = merge_intervals(nxt)
        if not result:
            break
    return result


# --------------------------------------------------------------- 线性不等式
def linear_band(alpha, beta, lower, upper):
    """解闭区间约束 ``lower <= alpha·g + beta <= upper``（alpha、beta、界均有理）。

    返回满足条件的 ``g`` 的闭区间（可能退化为点），无解返回 ``None``。
    对正增益线 alpha 严格为正，但本函数按一般有理 alpha 精确处理三种方向；
    约束端点与解的端点都保留为 Fraction，不做任何近似。
    """
    alpha = Fraction(alpha)
    beta = Fraction(beta)
    lower = Fraction(lower)
    upper = Fraction(upper)
    if alpha == 0:
        return Interval(Fraction(0), Fraction(0)) if lower <= beta <= upper else None
    lo_g = (lower - beta) / alpha
    hi_g = (upper - beta) / alpha
    if alpha < 0:
        lo_g, hi_g = hi_g, lo_g
    return Interval(lo_g, hi_g)


# --------------------------------------------------------------- 整段/见证
def union_contains_segment(unions, segment):
    """闭区间并集是否完整覆盖闭区间 ``segment``（整段承诺，不采样）。"""
    segment = Interval(Fraction(segment.lo), Fraction(segment.hi))
    cursor = segment.lo
    for part in merge_intervals(unions):
        if part.hi < cursor:
            continue
        if part.lo > cursor:
            return False  # 出现真空隙
        if part.hi >= segment.hi:
            return True
        cursor = part.hi
    return cursor >= segment.hi


def uncovered_witness(unions, segment):
    """在 ``segment`` 内找一个并集解释不了的精确有理增益（拒签证据）。

    三种空隙分别给精确有理点：

    * 段首空隙：直接返回段左端点（它本身未被覆盖）；
    * 两段之间的内部空隙 ``(a, b)``：返回严格内部的中点 ``(a+b)/2``，
      不能返回 ``a``——它是上一段的右端点，本身仍可解释；
    * 段尾空隙：返回最后覆盖点与段右端点的中点。

    整段被覆盖时返回 ``None``。
    """
    segment = Interval(Fraction(segment.lo), Fraction(segment.hi))
    parts = merge_intervals(unions)
    covering = [p for p in parts if p.hi >= segment.lo and p.lo <= segment.hi]
    if not covering or covering[0].lo > segment.lo:
        return segment.lo
    frontier = covering[0].hi
    if frontier >= segment.hi:
        return None
    for part in covering[1:]:
        if part.lo > frontier:  # 内部空隙 (frontier, part.lo)，取严格中点
            return (frontier + part.lo) / 2
        frontier = max(frontier, part.hi)
        if frontier >= segment.hi:
            return None
    return (frontier + segment.hi) / 2  # 段尾空隙


def union_contains_point(unions, point):
    point = Fraction(point)
    return any(part.contains(point) for part in unions)


def rational_midpoint(segment):
    """区间内部的一个精确有理点（用于可选的人工抽检，不参与签发判定）。"""
    return (Fraction(segment.lo) + Fraction(segment.hi)) / 2
