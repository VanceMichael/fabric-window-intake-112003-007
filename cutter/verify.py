"""工艺网格核对与放行单元。

每个相交格在请求范围内应有各项目至少一份有效观测；位置、面别和按样位的
替代关系均须符合。同格存在不合格的有效观测时，不能用其他合格样点覆盖。
资料充分但超阈值（明确不符）与资料不足（缺证）分别记录；候选切点未收敛时
保留该段证据的不确定性，能确定的其他段继续走。
"""
from __future__ import annotations

from dataclasses import dataclass

from .geometry import MotherRect, trace
from .model import Order, Rect

PASS = "pass"
FAIL = "fail"
LACK = "insufficient"
UNCERTAIN = "uncertain"

# 原因分类（供工艺员反查）
MISSING = "missing_original"  # 少领了原件
SPARSE = "sparse"  # 样点仍不够
NO_COVER = "no_coverage"  # 目录无声明窗口
NO_ROUTE = "no_route"  # 分切链缺口
FAILING = "failing"  # 明确不符
SPLIT = "candidate_split"  # 候选分歧


@dataclass
class Cause:
    kind: str
    item: str | None
    mother: MotherRect | None
    detail: dict


@dataclass
class CellReport:
    cell: Rect  # 工艺网格格（工单坐标）
    region: Rect  # 格 ∩ 条带
    status: str
    items: dict  # 项目 → 状态
    causes: list
    segments: tuple  # 溯源 SegmentMap，供反查


@dataclass
class ReleaseUnit:
    """放行单元：不可拆条带整段一个单元，可拆范围逐格一个单元。"""

    kind: str  # indivisible / separable
    xrange: tuple
    cells: list
    status: str  # released / pending / blocked


@dataclass
class OrderReport:
    order: Order
    clock: int
    cells: list
    units: list

    @property
    def status(self) -> str:
        if any(u.status == "blocked" for u in self.units):
            return "blocked"
        if any(u.status == "pending" for u in self.units):
            return "pending"
        return "released"

    def problem_cells(self) -> list:
        return [c for c in self.cells if c.status != PASS]


def grid_cells(order: Order, track: Rect) -> list:
    """以工单原点和 cells_mm 为基准、与条带相交的格。"""
    cx, cy = order.cells_mm
    out = []
    i = track.x0 // cx
    while i * cx < track.x1:
        j = track.y0 // cy
        while j * cy < track.y1:
            out.append(Rect(i * cx, (i + 1) * cx, j * cy, (j + 1) * cy))
            j += 1
        i += 1
    return out


def threshold_text(order: Order, item: str) -> str:
    if item == "delta_e":
        return f"delta_e≤{order.delta_e_max}"
    return "light_pass=1"


def _voided_in_rect(store, mother: MotherRect, item: str, windows, clock) -> list:
    by = store.voided_by(windows, clock)
    out = []
    for o in store.observations(mother.roll, mother.face, item):
        key = (o.document, o.sample, o.item)
        if key in by and mother.rect.contains(o.x, o.y):
            out.append((o.sample, by[key]))
    return out


def assess_item(order: Order, item: str, mother: MotherRect, store, windows, clock):
    """单个母卷范围、单个项目：返回 (状态, 原因列表)。

    判定次序：有不合格有效观测即明确不符；目录声明覆盖到本范围的窗口
    必须已取到，否则记少领原件（不能用同格其他合格样点替代未取的声明
    覆盖）；已取齐且至少一份有效观测才合格；取齐但范围内无样位记样点
    不够；目录无声明记无覆盖。
    """
    valid = store.valid_observations(
        mother.roll, mother.face, item, mother.rect, windows, clock
    )
    failing = [o for o in valid if order.violates(item, o.value)]
    if failing:
        return FAIL, [
            Cause(
                FAILING, item, mother,
                {
                    "samples": [
                        (o.sample, str(o.value), o.document, o.x, o.y) for o in failing
                    ],
                    "threshold": threshold_text(order, item),
                },
            )
        ]
    declared = [
        w
        for w in windows
        if w.roll == mother.roll
        and w.face == mother.face
        and w.item == item
        and w.catalog_turn <= clock
        and w.rect.intersect(mother.rect)
    ]
    missing = [w for w in declared if not store.has(w.id)]
    voided = _voided_in_rect(store, mother, item, windows, clock)
    if missing:
        return LACK, [
            Cause(
                MISSING, item, mother,
                {
                    "windows": [w.id for w in missing],
                    "retrievable": [w.id for w in missing if w.retrievable(clock)],
                    "uploading": [w.id for w in missing if clock < w.ready],
                    "expired": [w.id for w in missing if clock >= w.expires],
                    "voided": voided,
                },
            )
        ]
    if valid:
        return PASS, []
    if declared:
        return LACK, [
            Cause(SPARSE, item, mother, {"windows": [w.id for w in declared], "voided": voided})
        ]
    return LACK, [Cause(NO_COVER, item, mother, {"voided": voided})]


def _combine_parts(statuses) -> str:
    """不同空间段 / 不同项目的合成：明确不符 > 候选未定 > 资料不足 > 合格。"""
    for status in (FAIL, UNCERTAIN, LACK):
        if status in statuses:
            return status
    return PASS


def _cause_key(cause: Cause):
    mother = cause.mother
    return (
        cause.kind,
        cause.item,
        mother.roll if mother else None,
        str(mother.rect) if mother else None,
        repr(sorted(cause.detail.items())),
    )


def evaluate_cell(order, cell, region, by_child, store, windows, clock) -> CellReport:
    segments = trace(by_child, order.roll, region)
    item_status = {}
    causes = []
    for item in order.required_items:
        seg_status = []
        for seg in segments:
            if not seg.options:
                seg_status.append(LACK)
                causes.append(Cause(NO_ROUTE, item, None, {"rect": str(seg.child_rect)}))
                continue
            assessed = [assess_item(order, item, m, store, windows, clock) for m in seg.options]
            statuses = {s for s, _ in assessed}
            if len(statuses) == 1:
                # 各候选结论一致（含无候选），该段可确定
                seg_status.append(statuses.pop())
                for _, cs in assessed:
                    causes.extend(cs)
            else:
                # 不同候选指到不同母卷或不同检测覆盖：保留不确定性
                seg_status.append(UNCERTAIN)
                causes.append(
                    Cause(
                        SPLIT, item, None,
                        {
                            "rect": str(seg.child_rect),
                            "options": [
                                {
                                    "basis": m.basis,
                                    "roll": m.roll,
                                    "rect": str(m.rect),
                                    "status": s,
                                }
                                for m, (s, _) in zip(seg.options, assessed)
                            ],
                        },
                    )
                )
                for _, cs in assessed:
                    causes.extend(cs)
        item_status[item] = _combine_parts(seg_status)
    status = _combine_parts(list(item_status.values()))
    deduped, seen = [], set()
    for cause in causes:
        key = _cause_key(cause)
        if key not in seen:
            seen.add(key)
            deduped.append(cause)
    return CellReport(cell, region, status, item_status, deduped, tuple(segments))


def _unit_status(cells) -> str:
    statuses = [c.status for c in cells]
    if FAIL in statuses:
        return "blocked"
    if LACK in statuses or UNCERTAIN in statuses:
        return "pending"
    return "released"


def release_units(order: Order, cells: list) -> list:
    """不可拆条带某一位置缺证或不符，整条待确认/阻断；可拆范围逐格放行。"""
    units = []
    used = set()
    for a, b in order.indivisible:
        members = [c for c in cells if c.cell.x0 < b and a < c.cell.x1]
        if not members:
            continue
        used.update(id(c) for c in members)
        units.append(ReleaseUnit("indivisible", (a, b), members, _unit_status(members)))
    for c in cells:
        if id(c) in used:
            continue
        units.append(
            ReleaseUnit("separable", (c.cell.x0, c.cell.x1), [c], _unit_status([c]))
        )
    return units


def evaluate_order(order, by_child, store, windows, clock) -> OrderReport:
    cells = []
    for track in order.tracks:
        for cell in grid_cells(order, track):
            region = cell.intersect(track)
            if region is not None:
                cells.append(
                    evaluate_cell(order, cell, region, by_child, store, windows, clock)
                )
    return OrderReport(order, clock, cells, release_units(order, cells))
