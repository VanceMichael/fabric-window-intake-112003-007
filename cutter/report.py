"""中文报告：工单结论、台账动作与工艺反查。

工艺员可从当前结果反查母卷和坐标，并区分是少领了原件、样点仍不够、
明确不符，还是候选切点未收敛。
"""
from __future__ import annotations

from .verify import (
    FAILING,
    LACK,
    MISSING,
    NO_COVER,
    NO_ROUTE,
    PASS,
    SPARSE,
    SPLIT,
    UNCERTAIN,
    FAIL,
)
from .warehouse import ACTION_TEXT

STATUS_TEXT = {
    PASS: "合格",
    FAIL: "明确不符",
    LACK: "资料不足",
    UNCERTAIN: "候选未定",
}
ORDER_TEXT = {"released": "可放行", "pending": "待确认", "blocked": "阻断"}
UNIT_TEXT = {"released": "放行", "pending": "待确认", "blocked": "阻断"}


def fmt_rect(rect) -> str:
    return f"[{rect.x0},{rect.x1})x[{rect.y0},{rect.y1})"


def chain_text(roll: str, mother) -> str:
    """从工单卷坐标反查母卷与坐标：R20[...] → C1(复尺) → M10/front [...]。"""
    child = mother.invert(mother.rect)
    return (
        f"{roll}{fmt_rect(child)} → {mother.basis} → "
        f"{mother.roll}/{mother.face}{fmt_rect(mother.rect)}"
    )


def cause_lines(roll: str, cause) -> list:
    lines = []
    mother = cause.mother
    if mother is not None:
        lines.append(f"反查：{chain_text(roll, mother)}")
    if cause.kind == FAILING:
        for sample, value, doc, x, y in cause.detail["samples"]:
            point = ""
            if mother is not None:
                cx, cy = mother.invert_point(x, y)
                point = f"，对应 {roll}({cx},{cy})"
            lines.append(
                f"明确不符：样位 {sample} 实测 {value}，阈值 {cause.detail['threshold']}"
                f"（原件 {doc}{point}）"
            )
    elif cause.kind == MISSING:
        detail = cause.detail
        parts = []
        if detail["retrievable"]:
            parts.append(f"可补领 {'、'.join(detail['retrievable'])}")
        if detail["uploading"]:
            parts.append(f"上传中 {'、'.join(detail['uploading'])}")
        if detail["expired"]:
            parts.append(f"已过期不可补 {'、'.join(detail['expired'])}")
        line = f"少领原件：窗口 {'、'.join(detail['windows'])}（{'；'.join(parts)}）"
        if detail["voided"]:
            line += "；已作废样位 " + "、".join(
                f"{s}（被 {d} 复测替换）" for s, d in detail["voided"]
            )
        lines.append(line)
    elif cause.kind == SPARSE:
        line = f"样点仍不够：已取 {'、'.join(cause.detail['windows'])} 但该范围无有效样位"
        if cause.detail["voided"]:
            line += "；已作废样位 " + "、".join(
                f"{s}（被 {d} 复测替换）" for s, d in cause.detail["voided"]
            )
        lines.append(line)
    elif cause.kind == NO_COVER:
        lines.append("目录无声明窗口覆盖该范围（资料不足）")
    elif cause.kind == NO_ROUTE:
        lines.append(f"分切链缺口：{cause.detail['rect']} 无生效分切单")
    elif cause.kind == SPLIT:
        lines.append(f"候选未收敛：{cause.detail['rect']}")
        for opt in cause.detail["options"]:
            lines.append(
                f"  {opt['basis']} → {opt['roll']}{opt['rect']}：{STATUS_TEXT[opt['status']]}"
            )
    return lines


def format_order(report) -> str:
    order = report.order
    lines = [
        f"[工单 {order.id}]（{order.purpose}，卷 {order.roll}）：{ORDER_TEXT[report.status]}"
    ]
    groups = {}
    for unit in report.units:
        if unit.status == "released":
            continue
        key = (unit.kind, unit.xrange, unit.status)
        ys = groups.setdefault(key, [])
        for cell in unit.cells:
            span = (cell.cell.y0, cell.cell.y1)
            if span not in ys:
                ys.append(span)
    for (kind, (a, b), status), ys in groups.items():
        label = "不可拆条带" if kind == "indivisible" else "可拆段"
        ytext = "" if kind == "indivisible" else (
            "（" + "、".join(f"y[{y0},{y1})" for y0, y1 in ys) + "）"
        )
        lines.append(f"  {label} x[{a},{b}){ytext}：{UNIT_TEXT[status]}")
    for cell in report.problem_cells():
        items = "、".join(f"{k}={STATUS_TEXT[v]}" for k, v in cell.items.items())
        lines.append(f"  格 {fmt_rect(cell.cell)}：{STATUS_TEXT[cell.status]}（{items}）")
        for cause in cell.causes:
            lines.extend("    " + line for line in cause_lines(order.roll, cause))
    return "\n".join(lines)


def format_entry(entry) -> str:
    lines = [
        f"[台账 {entry.ref}] 实物 {entry.piece} 状态 {entry.state} "
        f"段 x[{entry.span[0]},{entry.span[1]})（复核工单 {entry.order_id or '?'}）"
        f"→ {ACTION_TEXT[entry.action]}"
    ]
    if entry.destination:
        lines.append(f"  交接裁床：{entry.destination}")
    if entry.location:
        lines.append(f"  退回库位：{entry.location}")
    originals, cuts = entry.kept_evidence
    if originals or cuts:
        kept = []
        if originals:
            kept.append(f"原件 {'、'.join(originals)}")
        if cuts:
            kept.append(f"分切单 {'、'.join(cuts)}")
        lines.append(f"  放行证据（保留）：{'；'.join(kept)}")
    for cell in entry.problems:
        lines.append(f"  问题格 {fmt_rect(cell.cell)}：{STATUS_TEXT[cell.status]}")
        for cause in cell.causes:
            lines.extend("    " + line for line in cause_lines(entry.roll, cause))
    for note in entry.notes:
        lines.append(f"  {note}")
    if entry.still_out:
        spans = "、".join(f"x[{a},{b})" for a, b in entry.still_out)
        lines.append(f"  仍未回仓段：{spans}（按原交接追踪）")
    return "\n".join(lines)


def format_turn(turn) -> str:
    lines = [f"===== 轮次 {turn.clock} ====="]
    for cut in turn.new_cuts:
        tag = "复尺" if cut.remeasure else "分切"
        extra = f"，替换 {cut.supersedes}" if cut.supersedes else ""
        ends = cut.parent_ends or "、".join(str(list(e)) for e in cut.candidates)
        lines.append(
            f"[{tag}] {cut.id}：{cut.child}{list(cut.child_span)} ← {cut.parent} "
            f"端点 {ends}（{cut.face}{extra}）"
        )
    lines.extend(turn.fetch_log)
    for order_id in sorted(turn.order_reports):
        lines.append(format_order(turn.order_reports[order_id]))
    for entry in turn.ledger:
        lines.append(format_entry(entry))
    return "\n".join(lines)
