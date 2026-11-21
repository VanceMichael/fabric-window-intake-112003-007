"""窗口领取计划与执行。

一次工单的领取名额有限：只为会映射到本工单条带的窗口花额度（不按整卷
下载）；同一窗口可服务多个工单；即将不可领（expires 临近）的窗口优先，
在复尺出来前保住会影响该工单的证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import trace


@dataclass
class WindowNeed:
    window: object
    orders: list = field(default_factory=list)  # 需要它的工单（按 deadline 升序）
    spots: list = field(default_factory=list)  # 影响到的位置（说明用）


def plan_needs(orders, by_child, store, windows, clock) -> list:
    """各工单条带映射到的、尚未取得的窗口，按到期先后排序。"""
    needs: dict[str, WindowNeed] = {}
    ranked = sorted(orders, key=lambda o: (o.deadline is None, o.deadline or 0, o.id))
    for order in ranked:
        for track in order.tracks:
            for seg in trace(by_child, order.roll, track):
                for mother in seg.options:
                    if mother.face is None:
                        continue
                    for item in order.required_items:
                        for w in windows:
                            if (w.roll, w.face, w.item) != (mother.roll, mother.face, item):
                                continue
                            if w.catalog_turn > clock or not w.rect.intersect(mother.rect):
                                continue
                            if store.has(w.id):
                                continue
                            need = needs.setdefault(w.id, WindowNeed(w))
                            if order.id not in need.orders:
                                need.orders.append(order.id)
                            spot = f"{order.id}:{mother.roll}{mother.rect}"
                            if spot not in need.spots:
                                need.spots.append(spot)
    # 即将不可领的优先；同级按就绪轮次与编号稳定排序
    return sorted(needs.values(), key=lambda n: (n.window.expires, n.window.ready, n.window.id))


def execute_needs(client, store, needs, clock) -> list:
    """按计划取票下载，返回中文日志。名额满（429）只停该工单该母卷本轮。"""
    log = []
    blocked = set()  # (工单, 母卷) 本轮名额已满
    for need in needs:
        w = need.window
        if clock >= w.expires:
            log.append(f"[过期] {w.id} 已不可领（expires={w.expires}），涉及 {'、'.join(need.orders)}")
            continue
        if clock < w.ready:
            log.append(f"[待传] {w.id} 上传中（ready={w.ready}），下轮再试")
            continue
        got = False
        for order_id in need.orders:
            if (order_id, w.roll) in blocked:
                continue
            outcome = fetch_window(client, store, order_id, w, clock, log)
            if outcome == "ok":
                got = True
                break
            if outcome == "quota":
                blocked.add((order_id, w.roll))
                continue
            break  # expired / gone / error / uploading：换工单也无济于事
        if not got:
            log.append(f"[未得] {w.id} 本轮未取到（{w.roll}/{w.face}/{w.item}）")
    return log


def fetch_window(client, store, order_id, w, clock, log) -> str:
    """领一个窗口：先查既有票（相同领件号只能原样重试），再按需新领。"""
    request = store.current_request_id(order_id, w.id)
    if request:
        code, record = client.ticket_lookup(request)
        if (
            code == 200
            and record.get("order") == order_id
            and record.get("id") == w.id
        ):
            if clock < record.get("until", 0):
                return _download(client, store, w, record["href"], order_id, request, log, clock, "沿用既有票")
            request = store.mint_request_id(order_id, w.id)  # 旧票已过有效期，换新领件号
    else:
        request = store.mint_request_id(order_id, w.id)
    code, record = client.ticket_request(order_id, w.id, request)
    if code in (200, 201):
        if clock < record.get("until", 0):
            return _download(client, store, w, record["href"], order_id, request, log, clock, "新领")
        log.append(f"[异常] {w.id} 票已过有效期")
        return "gone"
    if code == 429:
        log.append(f"[名额] 工单 {order_id} 对 {w.roll} 本轮领取名额已满")
        return "quota"
    if code == 425:
        log.append(f"[待传] {w.id} 上传中（ready={w.ready}）")
        return "uploading"
    if code == 410:
        log.append(f"[过期] {w.id} 已不可领")
        return "expired"
    log.append(f"[异常] {w.id} 取票返回 {code}: {record}")
    return "error"


def _download(client, store, w, href, order_id, request, log, clock, how) -> str:
    code, _, content = client.download(href)
    if code != 200:
        log.append(f"[异常] {w.id} 下载返回 {code}")
        return "gone" if code == 410 else "error"
    store.put(w, content, clock)  # 内部按目录声明校验 SHA-256
    log.append(
        f"[领取] {w.id}（{w.roll}/{w.face}/{w.item} "
        f"{list(w.span)}x{list(w.width)}）工单 {order_id} 领件号 {request}（{how}）"
    )
    return "ok"
