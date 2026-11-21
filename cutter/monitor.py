"""单轮编排：同步单据 → 领件 → 网格核对 → 台账核对。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import effective_cuts, group_by_child, trace
from .model import Cut, LedgerEntry, Order, Rect, WindowMeta
from .planner import execute_needs, plan_needs
from .verify import evaluate_order
from .warehouse import evaluate_ledger


@dataclass
class TurnReport:
    clock: int
    fetch_log: list
    order_reports: dict
    ledger: list
    windows: list
    new_cuts: list  # 本轮新生效的分切单（含复尺）


def _bbox(a: Rect, b: Rect) -> Rect:
    return Rect(min(a.x0, b.x0), max(a.x1, b.x1), min(a.y0, b.y0), max(a.y1, b.y1))


class Monitor:
    def __init__(self, client, store):
        self.client = client
        self.store = store
        self._seen_cuts: set = set()

    def run_turn(self) -> TurnReport:
        clock = self.client.clock()
        orders = [Order.from_row(r) for r in self.client.orders()]
        cuts = [Cut.from_row(r) for r in self.client.cuts()]
        entries = [LedgerEntry.from_row(r) for r in self.client.warehouse()]
        active = effective_cuts(cuts, clock)
        by_child = group_by_child(active.values())
        new_cuts = [c for c in active.values() if c.id not in self._seen_cuts]
        new_cuts.sort(key=lambda c: c.id)
        self._seen_cuts = set(active)
        windows = self._fetch_catalog(orders, by_child)
        needs = plan_needs(orders, by_child, self.store, windows, clock)
        fetch_log = execute_needs(self.client, self.store, needs, clock)
        reports = {
            o.id: evaluate_order(o, by_child, self.store, windows, clock) for o in orders
        }
        ledger = evaluate_ledger(
            entries, {o.id: o for o in orders}, reports, clock
        )
        return TurnReport(clock, fetch_log, reports, ledger, windows, new_cuts)

    def _fetch_catalog(self, orders, by_child) -> list:
        """按各工单条带映射到的 (母卷, 面别) 查询目录声明。"""
        spots: dict[tuple, Rect] = {}
        for order in orders:
            for track in order.tracks:
                for seg in trace(by_child, order.roll, track):
                    for mother in seg.options:
                        if mother.face is None:
                            continue
                        key = (mother.roll, mother.face)
                        spots[key] = (
                            mother.rect if key not in spots else _bbox(spots[key], mother.rect)
                        )
        windows: dict[str, WindowMeta] = {}
        for (roll, face), rect in sorted(spots.items()):
            for row in self.client.catalog(roll, face, rect):
                meta = WindowMeta.from_row(row)
                windows[meta.id] = meta
        return list(windows.values())
