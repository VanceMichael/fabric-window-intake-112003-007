"""裁床客户端测试：坐标溯源、证据作废、网格核对、领取计划与台账动作。"""
import json
import unittest
from pathlib import Path
from threading import Thread

from source_service import create_source

from cutter.client import SourceClient
from cutter.geometry import effective_cuts, group_by_child, trace
from cutter.model import Cut, Order, Rect, WindowMeta
from cutter.monitor import Monitor
from cutter.planner import plan_needs
from cutter.store import EvidenceStore
from cutter.verify import (
    FAIL,
    LACK,
    PASS,
    UNCERTAIN,
    evaluate_order,
)

ROOT = Path(__file__).resolve().parents[1]
CUTS = [Cut.from_row(r) for r in json.loads((ROOT / "data" / "cuts.json").read_text())]
ORDERS = [Order.from_row(r) for r in json.loads((ROOT / "data" / "orders.json").read_text())]


def by_child_at(clock):
    return group_by_child(effective_cuts(CUTS, clock).values())


def only_option(segments):
    assert len(segments) == 1 and len(segments[0].options) == 1
    return segments[0].options[0]


class GeometryTest(unittest.TestCase):
    def test_reversed_length_and_flipped_width(self):
        # C1 倒卷：子卷段首毫米对应父卷高端减 1；宽度翻转时 y=0 对应右边界减 1
        mother = only_option(trace(by_child_at(0), "R20", Rect(0, 800, 0, 600)))
        self.assertEqual((mother.roll, mother.face), ("M10", "front"))
        self.assertEqual(mother.rect.as_tuple() if hasattr(mother.rect, "as_tuple") else
                         (mother.rect.x0, mother.rect.x1, mother.rect.y0, mother.rect.y1),
                         (1000, 1800, 100, 700))
        corner = only_option(trace(by_child_at(0), "R20", Rect(0, 1, 0, 1)))
        self.assertEqual((corner.rect.x0, corner.rect.x1, corner.rect.y0, corner.rect.y1),
                         (1799, 1800, 699, 700))

    def test_remeasure_corrects_only_its_segment(self):
        # RR1 第 2 轮替换 C1：R20[0,800) 改指 M10[1050,1850)，其余段不变
        before = only_option(trace(by_child_at(1), "R20", Rect(0, 800, 0, 600)))
        self.assertEqual((before.rect.x0, before.rect.x1), (1000, 1800))
        after = only_option(trace(by_child_at(2), "R20", Rect(0, 800, 0, 600)))
        self.assertEqual((after.rect.x0, after.rect.x1), (1050, 1850))
        untouched = only_option(trace(by_child_at(2), "R20", Rect(800, 1200, 0, 600)))
        self.assertEqual((untouched.roll, untouched.rect.x0, untouched.rect.x1),
                         ("M11", 0, 400))

    def test_candidates_stay_open_until_remeasure(self):
        # C3 两个候选切点：复尺 RR2 公开前保留两种母卷位置
        options = trace(by_child_at(0), "R20", Rect(1200, 1600, 0, 600))[0].options
        self.assertEqual(len(options), 2)
        self.assertEqual({(o.rect.x0, o.rect.x1) for o in options},
                         {(100, 500), (150, 550)})
        self.assertTrue(all(o.roll == "M12" and o.face == "back" for o in options))
        converged = only_option(trace(by_child_at(3), "R20", Rect(1200, 1600, 0, 600)))
        self.assertEqual((converged.rect.x0, converged.rect.x1), (150, 550))

    def test_sub_roll_traces_to_dyed_mother_roll(self):
        # R30 是 R20 的再分切：必须追到实际染色的母卷，不能靠最近卷号结案
        first = only_option(trace(by_child_at(0), "R30", Rect(0, 200, 0, 600)))
        self.assertEqual(first.roll, "M10")
        self.assertEqual([hop.cut.id for hop in first.path], ["C4", "C1"])
        second = only_option(trace(by_child_at(0), "R30", Rect(200, 400, 0, 600)))
        self.assertEqual(second.roll, "M11")
        self.assertEqual([hop.cut.id for hop in second.path], ["C5", "C2"])

    def test_invert_roundtrip(self):
        mother = only_option(trace(by_child_at(0), "R20", Rect(0, 800, 0, 600)))
        back = mother.invert(mother.rect)
        self.assertEqual((back.x0, back.x1, back.y0, back.y1), (0, 800, 0, 600))
        self.assertEqual(mother.invert_point(1799, 699), (0, 0))


def make_window(window_id, roll, face, span, width, item, turn=0, ready=0, expires=9,
                replaces=None):
    return WindowMeta(
        id=window_id, roll=roll, face=face, span=tuple(span), width=tuple(width),
        catalog_turn=turn, ready=ready, expires=expires, item=item,
        media="application/json" if item == "delta_e" else "text/csv",
        sha256="", replaces_document=(replaces or {}).get("document"),
        replaces_samples=tuple((replaces or {}).get("samples", ())),
    )


class StoreTest(unittest.TestCase):
    def test_parse_and_digest_guard(self):
        meta = make_window("E1", "M10", "front", (1000, 1800), (100, 700), "delta_e")
        store = EvidenceStore()
        store.put(meta, (ROOT / "originals" / "mill" / "E1.json").read_bytes(), 0)
        self.assertEqual(len(store.observations("M10", "front", "delta_e")), 8)
        bad = make_window("BAD", "M10", "front", (0, 1), (0, 1), "delta_e")
        bad = WindowMeta(**{**bad.__dict__, "sha256": "0" * 64})
        with self.assertRaises(Exception):
            store.put(bad, b'{"rows": []}', 0)

    def test_disk_roundtrip_keeps_bytes_and_requests(self):
        # 已取字节与领件号落盘：客户端重开后仍能核对，不依赖变动后的目录
        import tempfile
        meta = make_window("E1", "M10", "front", (1000, 1800), (100, 700), "delta_e")
        content = (ROOT / "originals" / "mill" / "E1.json").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(tmp)
            store.put(meta, content, 0)
            store.mint_request_id("A20", "E1")
            reopened = EvidenceStore(tmp)
            self.assertTrue(reopened.has("E1"))
            self.assertEqual(len(reopened.observations("M10", "front", "delta_e")), 8)
            self.assertEqual(reopened.current_request_id("A20", "E1"), "A20~E1")

    def test_replacement_voids_only_declared_samples_and_item(self):
        e3 = make_window("E3", "M11", "front", (0, 400), (0, 600), "delta_e")
        e3l = make_window("E3L", "M11", "front", (0, 400), (0, 600), "light")
        e5 = make_window("E5", "M11", "front", (50, 100), (100, 200), "delta_e",
                         turn=1, ready=3, expires=7,
                         replaces={"document": "E3", "samples": ["M11-75-150"]})
        store = EvidenceStore()
        store.put(e3, (ROOT / "originals" / "mill" / "E3.json").read_bytes(), 0)
        store.put(e3l, (ROOT / "originals" / "mill" / "E3L.csv").read_bytes(), 0)
        windows = [e3, e3l, e5]
        rect = Rect(0, 200, 0, 300)
        # 第 0 轮：E5 未公开，E3 的样位有效
        valid = store.valid_observations("M11", "front", "delta_e", rect, windows, 0)
        self.assertEqual({o.sample for o in valid}, {"M11-75-150"})
        # 第 1 轮：E5 声明作废该样位的色差项目，照度不受影响
        valid = store.valid_observations("M11", "front", "delta_e", rect, windows, 1)
        self.assertEqual(valid, [])
        light = store.valid_observations("M11", "front", "light", rect, windows, 1)
        self.assertEqual({o.sample for o in light}, {"M11-75-150"})


def make_order(order_id="T1", delta="1.0", cells=(100, 600), track=(0, 100, 0, 600)):
    return Order.from_row({
        "id": order_id, "roll": "R1", "purpose": "测试", "length": 100, "width": 600,
        "cells_mm": list(cells),
        "items": {"delta_e_max": delta, "light_pass": False},
        "indivisible": [], "known": 0,
        "tracks": [{"span": [track[0], track[1]], "width": [track[2], track[3]]}],
    })


class VerifyTest(unittest.TestCase):
    def test_failing_observation_cannot_be_covered_by_passing_one(self):
        # 同格一份合格、一份不合格的有效观测：不能用合格样点覆盖
        cut = Cut.from_row({
            "id": "K1", "child": "R1", "child_span": [0, 100], "parent": "M1",
            "parent_ends": [0, 100], "trim": [0, 600], "flip_width": False,
            "face": "front", "known": 0,
        })
        by_child = group_by_child([cut])
        good = make_window("G", "M1", "front", (0, 100), (0, 600), "delta_e")
        bad = make_window("B", "M1", "front", (0, 100), (0, 600), "delta_e")
        store = EvidenceStore()
        store.put(good, json.dumps({"rows": [
            {"sample": "M1-50-300", "x": 50, "y": 300, "delta_e": "0.5"}]}).encode(), 0)
        store.put(bad, json.dumps({"rows": [
            {"sample": "M1-60-300", "x": 60, "y": 300, "delta_e": "1.5"}]}).encode(), 0)
        report = evaluate_order(make_order(), by_child, store, [good, bad], 0)
        self.assertEqual(report.cells[0].status, FAIL)

    def test_candidate_split_keeps_uncertainty(self):
        # 两个候选把同一格指到不同检测覆盖：保留不确定性
        cut = Cut.from_row({
            "id": "K2", "child": "R1", "child_span": [0, 100], "parent": "M1",
            "candidate_ends": [[0, 100], [500, 600]], "trim": [0, 600],
            "flip_width": False, "face": "front", "known": 0,
        })
        by_child = group_by_child([cut])
        win = make_window("W", "M1", "front", (0, 100), (0, 600), "delta_e")
        store = EvidenceStore()
        store.put(win, json.dumps({"rows": [
            {"sample": "M1-50-300", "x": 50, "y": 300, "delta_e": "0.5"}]}).encode(), 0)
        report = evaluate_order(make_order(), by_child, store, [win], 0)
        self.assertEqual(report.cells[0].status, UNCERTAIN)
        kinds = {c.kind for c in report.cells[0].causes}
        self.assertIn("candidate_split", kinds)

    def test_indivisible_strip_pending_on_single_gap(self):
        # 不可拆条带某一位置缺证：整条待确认
        cuts = [
            Cut.from_row({
                "id": "K3", "child": "R1", "child_span": [0, 100], "parent": "M1",
                "parent_ends": [0, 100], "trim": [0, 600], "flip_width": False,
                "face": "front", "known": 0,
            }),
            Cut.from_row({
                "id": "K4", "child": "R1", "child_span": [100, 200], "parent": "M2",
                "parent_ends": [0, 100], "trim": [0, 600], "flip_width": False,
                "face": "front", "known": 0,
            }),
        ]
        by_child = group_by_child(cuts)
        win = make_window("W", "M1", "front", (0, 100), (0, 600), "delta_e")
        store = EvidenceStore()
        store.put(win, json.dumps({"rows": [
            {"sample": "M1-50-300", "x": 50, "y": 300, "delta_e": "0.5"}]}).encode(), 0)
        order = Order.from_row({
            "id": "T2", "roll": "R1", "purpose": "测试", "length": 200, "width": 600,
            "cells_mm": [100, 600], "items": {"delta_e_max": "1.0"},
            "indivisible": [[0, 200]], "known": 0,
            "tracks": [{"span": [0, 200], "width": [0, 600]}],
        })
        report = evaluate_order(order, by_child, store, [win], 0)
        self.assertEqual(len(report.units), 1)
        self.assertEqual(report.units[0].status, "pending")


class ScenarioTest(unittest.TestCase):
    """全流程演练：与资料端联调，逐轮核对领取、放行、停料、追回与退回重核。"""

    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever,
                        kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = SourceClient(base)
        self.store = EvidenceStore()
        self.monitor = Monitor(self.client, self.store)

    def turn(self, clock):
        self.client.advance(clock)
        return self.monitor.run_turn()

    def entry(self, report, ref):
        return next(e for e in report.ledger if e.ref == ref)

    def test_full_scenario(self):
        # 第 0 轮：按工单条带领取，不整卷下载；两工单共享已取窗口
        report = self.turn(0)
        self.assertEqual(set(self.store.window_ids()),
                         {"E1", "E1L", "E3", "E3L", "E4", "E4L"})
        self.assertNotIn("EX", self.store.window_ids())  # 面别不符，不适用
        self.assertEqual(report.order_reports["A20"].status, "released")
        self.assertEqual(report.order_reports["A30"].status, "released")
        self.assertEqual(self.entry(report, "OUT80").action, "clear")
        self.assertEqual(self.entry(report, "HOLD11").action, "ready")

        # 第 1 轮：E5 声明作废 M11-75-150 的色差项目但尚未就绪；
        # E2/E2L 虽在目录但与当前映射无关，不烧额度
        report = self.turn(1)
        self.assertNotIn("E2", self.store.window_ids())
        self.assertNotIn("E5", self.store.window_ids())
        self.assertEqual(report.order_reports["A20"].status, "pending")
        self.assertEqual(self.entry(report, "HOLD11").action, "hold")
        cell = next(c for c in report.order_reports["A20"].cells
                    if c.cell.x0 == 800 and c.cell.y0 == 0)
        self.assertEqual(cell.status, LACK)
        missing = next(c for c in cell.causes if c.kind == "missing_original")
        self.assertEqual(missing.detail["windows"], ["E5"])
        self.assertEqual(missing.detail["uploading"], ["E5"])
        self.assertEqual(missing.detail["voided"], [("M11-75-150", "E5")])

        # 第 2 轮：复尺 RR1 使已离库段改指 M10[1050,1850)；E2/E2L 即将到期，优先保住
        report = self.turn(2)
        self.assertIn("E2", self.store.window_ids())
        self.assertIn("E2L", self.store.window_ids())
        self.assertEqual(report.order_reports["A20"].status, "blocked")
        out = self.entry(report, "OUT80")
        self.assertEqual(out.action, "recall")
        self.assertEqual(out.destination, "Cutter-A")
        self.assertEqual(out.kept_evidence, (("E1", "E1L"), ("C1",)))
        failing = [c for c in out.problems if c.status == FAIL]
        self.assertEqual({c.cell.x0 for c in failing}, {0})
        cause = next(c for c in failing[0].causes if c.kind == "failing")
        self.assertEqual(cause.mother.roll, "M10")
        self.assertEqual((cause.mother.rect.x0, cause.mother.rect.x1), (1650, 1850))

        # 第 3 轮：E5 就绪，复测值 1.9 超 A20 阈值 → 备料停止；A30 阈值 2.1 仍合格
        report = self.turn(3)
        self.assertIn("E5", self.store.window_ids())
        self.assertEqual(self.entry(report, "HOLD11").action, "stop_prep")
        self.assertEqual(report.order_reports["A30"].status, "released")
        cell = next(c for c in report.order_reports["A20"].cells
                    if c.cell.x0 == 800 and c.cell.y0 == 0)
        self.assertEqual(cell.status, FAIL)

        # 第 4 轮：退回段按 R95 重核（阈值 2.3），有资格再发；其余仍按原交接追踪
        report = self.turn(4)
        ret = self.entry(report, "RET95")
        self.assertEqual(ret.action, "eligible")
        self.assertEqual(ret.location, "W2-S3")
        self.assertEqual(report.order_reports["R95"].status, "released")
        out = self.entry(report, "OUT80")
        self.assertEqual(out.still_out, [(200, 800)])
        self.assertTrue(any("已随 RET95" in note for note in out.notes))

        # 之后轮次稳定：已取证据不受窗口到期影响
        report = self.turn(6)
        self.assertEqual(self.entry(report, "RET95").action, "eligible")
        self.assertEqual(self.entry(report, "HOLD11").action, "stop_prep")

    def test_returned_segment_waits_for_evidence_if_window_missed(self):
        # 若第 2 轮没保住 E2/E2L，退回段只能等补证（窗口已过期不可补）
        self.turn(0)
        self.turn(1)
        report = self.turn(4)  # 直接跳到第 4 轮，E2 已过期
        self.assertNotIn("E2", self.store.window_ids())
        ret = self.entry(report, "RET95")
        self.assertEqual(ret.action, "awaiting")
        causes = [c for cell in ret.problems for c in cell.causes]
        missing = next(c for c in causes if c.kind == "missing_original")
        self.assertIn("E2", missing.detail["expired"])

    def test_planner_only_fetches_what_tracks_touch(self):
        self.client.advance(0)
        orders = [Order.from_row(r) for r in self.client.orders()]
        cuts = [Cut.from_row(r) for r in self.client.cuts()]
        by_child = group_by_child(effective_cuts(cuts, 0).values())
        windows = self.monitor._fetch_catalog(orders, by_child)
        needs = plan_needs(orders, by_child, self.store, windows, 0)
        self.assertEqual({n.window.id for n in needs},
                         {"E1", "E1L", "E3", "E3L", "E4", "E4L"})
        # 第 1 轮：E2/E2L 已上目录但不影响任何工单条带，不进计划
        self.client.advance(1)
        cuts = [Cut.from_row(r) for r in self.client.cuts()]
        by_child = group_by_child(effective_cuts(cuts, 1).values())
        rows = self.client.catalog("M10", "front", Rect(1000, 1900, 100, 700))
        self.assertIn("E2", {r["id"] for r in rows})  # 目录可见
        windows = self.monitor._fetch_catalog(orders, by_child)
        windows += [WindowMeta.from_row(r) for r in rows]
        needs = plan_needs(orders, by_child, self.store, windows, 1)
        self.assertNotIn("E2", {n.window.id for n in needs})
        self.assertIn("E5", {n.window.id for n in needs})


if __name__ == "__main__":
    unittest.main()
