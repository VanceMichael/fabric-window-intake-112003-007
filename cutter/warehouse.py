"""装卸台账核对。

台账记录（已备料、已离库、退回存放位置）是事实，不能用后来重算的卷位
替换；实物标识 piece 不随坐标复尺改变。新复尺或新证据使先前放行的条带
不合格时：尚未发的停止备料；已经离库的从原交接号找到实际裁床与范围，
并留住当时放行证据。实际退回的料按后续工单重新核查。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .verify import FAIL, LACK, PASS, UNCERTAIN

ACTION_TEXT = {
    "ready": "备料维持",
    "hold": "待确认（暂缓备料）",
    "stop_prep": "停止备料",
    "clear": "离库维持",
    "watch": "离库观察（证据待确认）",
    "recall": "追回",
    "eligible": "有资格再发",
    "awaiting": "等补证",
    "unfit": "不符隔离",
    "no_track": "无法核对",
}


@dataclass
class EntryReport:
    ref: str
    piece: str
    roll: str
    state: str
    order_id: str | None  # 用于复核的工单（退回段为验收工单）
    action: str
    span: tuple
    problems: list = field(default_factory=list)  # 问题格 CellReport
    kept_evidence: tuple = ((), ())  # 放行时的检测原件与分切单（保留）
    destination: str | None = None
    location: str | None = None
    still_out: list = field(default_factory=list)  # 离库未回的子卷段
    notes: list = field(default_factory=list)


def subtract_span(span, parts) -> list:
    """从 [a,b) 中扣掉若干已退回段，得到仍未回仓的段。"""
    remain = [tuple(span)]
    for c, d in parts:
        nxt = []
        for a, b in remain:
            if d <= a or b <= c:
                nxt.append((a, b))
            else:
                if a < c:
                    nxt.append((a, c))
                if d < b:
                    nxt.append((d, b))
        remain = nxt
    return remain


def _merge(ranges) -> list:
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def evaluate_entry(entry, entries, orders, reports, clock) -> EntryReport:
    verifying = entry.accepted_order if entry.state == "returned" else entry.order
    report = EntryReport(
        ref=entry.ref,
        piece=entry.piece,
        roll=entry.roll,
        state=entry.state,
        order_id=verifying,
        action="no_track",
        span=entry.span,
        kept_evidence=(tuple(entry.evidence), tuple(entry.cuts)),
        destination=entry.destination,
        location=entry.location,
    )
    order_report = reports.get(verifying) if verifying else None
    if order_report is None:
        report.notes.append(f"工单 {verifying or '?'} 尚未公开，无法核对")
        return report
    cells = [
        c
        for c in order_report.cells
        if c.cell.x0 < entry.span[1] and entry.span[0] < c.cell.x1
    ]
    if not cells:
        report.notes.append("该段不在工单核查条带内")
        return report
    failed = [c for c in cells if c.status == FAIL]
    pending = [c for c in cells if c.status in (LACK, UNCERTAIN)]
    report.problems = failed + pending
    if entry.state == "staged":
        report.action = "stop_prep" if failed else "hold" if pending else "ready"
    elif entry.state == "sent":
        report.action = "recall" if failed else "watch" if pending else "clear"
    else:  # returned：按后续工单重核
        report.action = "unfit" if failed else "awaiting" if pending else "eligible"
    if entry.state == "sent":
        returned = [
            r for r in entries if r.part_of == entry.ref and r.piece == entry.piece
        ]
        report.still_out = subtract_span(entry.span, [r.span for r in returned])
        if failed:
            fail_x = _merge((c.cell.x0, c.cell.x1) for c in failed)
            for a, b in fail_x:
                back = [r for r in returned if r.span[0] < b and a < r.span[1]]
                if back:
                    refs = "、".join(f"{r.ref}（{r.location}）" for r in back)
                    report.notes.append(f"问题段 x[{a},{b}) 已随 {refs}回仓")
                for c, d in report.still_out:
                    if c < b and a < d:
                        report.notes.append(
                            f"问题段 x[{max(a, c)},{min(b, d)}) 仍在 {entry.destination}，"
                            f"按交接号 {entry.ref} 追回"
                        )
    return report


def evaluate_ledger(entries, orders, reports, clock) -> list:
    return [evaluate_entry(e, entries, orders, reports, clock) for e in entries]
