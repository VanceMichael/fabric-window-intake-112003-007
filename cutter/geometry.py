"""卷面坐标变换与向实际染色母卷的溯源。

分切单约定（见 README）：
- parent_ends 两个有向端点确定父卷长度范围；正向时子卷段首毫米对应父卷低端，
  倒卷时对应高端减 1（离散毫米、左闭右开）。
- trim 给父卷保留的左右坐标；flip_width 时子卷 y=0 对应右边界减 1。
- 复尺单以 supersedes 替换旧单，只更正指定的物料段；candidate_ends 是
  尚未确认的候选，只在有适用的已公开复尺时收敛。
- 子卷再分切时逐段应用两向变换，直到实际染色的母卷；检验面取末端
  母卷分切单记录的 front/back。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Cut, Rect


def shift_length(ends: tuple, span0: int, x0: int, x1: int) -> tuple:
    """子卷长度 [x0,x1) 映到父卷（span0 为 child_span 起点）。"""
    p0, p1 = ends
    if p0 < p1:  # 正向：子卷段首毫米对应父卷低端
        return (p0 + (x0 - span0), p0 + (x1 - span0))
    # 倒卷：子卷段首毫米对应父卷高端减 1
    return (p0 - (x1 - span0), p0 - (x0 - span0))


def shift_width(trim: tuple, flip: bool, y0: int, y1: int) -> tuple:
    """子卷宽度 [y0,y1) 映到父卷保留边内。"""
    left, right = trim
    if not flip:
        return (left + y0, left + y1)
    return (right - y1, right - y0)


def unshift_length(ends: tuple, span0: int, a: int, b: int) -> tuple:
    """父卷长度区间反查回子卷坐标。"""
    p0, p1 = ends
    if p0 < p1:
        return (a - p0 + span0, b - p0 + span0)
    return (span0 + p0 - b, span0 + p0 - a)


def unshift_width(trim: tuple, flip: bool, c: int, d: int) -> tuple:
    """父卷宽度区间反查回子卷坐标。"""
    left, right = trim
    if not flip:
        return (c - left, d - left)
    return (right - d, right - c)


def effective_cuts(cuts, clock: int) -> dict:
    """当前轮次生效的分切单：known 已公开，且未被已公开复尺单替换。"""
    active = {c.id: c for c in cuts if c.known <= clock}
    for cut in list(active.values()):
        if cut.supersedes and cut.supersedes in active:
            del active[cut.supersedes]
    return active


def group_by_child(cuts) -> dict:
    grouped: dict[str, list] = {}
    for cut in cuts:
        grouped.setdefault(cut.child, []).append(cut)
    for group in grouped.values():
        group.sort(key=lambda c: c.child_span[0])
    return grouped


@dataclass(frozen=True)
class Hop:
    """溯源链上的一次分切：采用的有向端点与依据。"""

    cut: Cut
    ends: tuple
    basis: str  # 确认 / 复尺 / 候选-N


@dataclass(frozen=True)
class MotherRect:
    """子卷矩形在母卷上的一种可能位置。"""

    roll: str
    rect: Rect
    face: str | None
    path: tuple  # Hop 序列，子卷 → 母卷

    @property
    def basis(self) -> str:
        return " → ".join(f"{hop.cut.id}({hop.basis})" for hop in self.path) or "母卷"

    def invert(self, rect: Rect) -> Rect:
        """把母卷上的矩形反查回工单卷坐标。"""
        x0, x1, y0, y1 = rect.x0, rect.x1, rect.y0, rect.y1
        for hop in reversed(self.path):
            cut = hop.cut
            x0, x1 = unshift_length(hop.ends, cut.child_span[0], x0, x1)
            y0, y1 = unshift_width(cut.trim, cut.flip_width, y0, y1)
        return Rect(x0, x1, y0, y1)

    def invert_point(self, x: int, y: int) -> tuple:
        """母卷上一个样位反查回工单卷坐标（所在毫米）。"""
        rect = self.invert(Rect(x, x + 1, y, y + 1))
        return (rect.x0, rect.y0)


@dataclass(frozen=True)
class SegmentMap:
    """工单卷上一段矩形的溯源结果。options 为空表示分切链缺口。"""

    child_rect: Rect
    options: tuple

    @property
    def certain(self) -> bool:
        return len(self.options) == 1


def _ends_options(cut: Cut) -> tuple:
    if cut.parent_ends is not None:
        basis = "复尺" if cut.remeasure else "确认"
        return ((cut.parent_ends, basis),)
    return tuple((ends, f"候选{i + 1}") for i, ends in enumerate(cut.candidates))


def trace(by_child: dict, roll: str, rect: Rect) -> list:
    """把 roll 上的 rect 溯源到母卷，返回覆盖 rect 的 SegmentMap 列表。

    候选切点未收敛时同一段给出多个 option；分切链缺口给出空 options。
    """
    cuts = [
        c
        for c in by_child.get(roll, ())
        if c.child_span[0] < rect.x1 and rect.x0 < c.child_span[1]
    ]
    if not cuts:
        return [SegmentMap(rect, (MotherRect(roll, rect, None, ()),))]
    segments = []
    cursor = rect.x0
    for cut in cuts:
        if cut.child_span[0] > cursor:
            gap = Rect(cursor, min(cut.child_span[0], rect.x1), rect.y0, rect.y1)
            segments.append(SegmentMap(gap, ()))
        x0 = max(rect.x0, cut.child_span[0])
        x1 = min(rect.x1, cut.child_span[1])
        if x0 >= x1:
            continue
        options = []
        for ends, basis in _ends_options(cut):
            px0, px1 = shift_length(ends, cut.child_span[0], x0, x1)
            py0, py1 = shift_width(cut.trim, cut.flip_width, rect.y0, rect.y1)
            hop = Hop(cut, ends, basis)
            for seg in trace(by_child, cut.parent, Rect(px0, px1, py0, py1)):
                for mother in seg.options:
                    face = mother.face if mother.face is not None else cut.face
                    options.append(
                        MotherRect(mother.roll, mother.rect, face, (hop,) + mother.path)
                    )
        segments.append(SegmentMap(Rect(x0, x1, rect.y0, rect.y1), tuple(options)))
        cursor = max(cursor, x1)
    if cursor < rect.x1:
        segments.append(SegmentMap(Rect(cursor, rect.x1, rect.y0, rect.y1), ()))
    return [s for s in segments if s.child_rect.x0 < s.child_rect.x1]
