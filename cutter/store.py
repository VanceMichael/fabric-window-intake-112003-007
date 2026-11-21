"""检测原件证据库。

已取窗口的准确字节与解析出的样位观察保存在本地：先前成功取得的字节
不能从变动后的目录凭空补出，可跨工单复用。声明作废按样位与项目生效，
窗口中其他有效观察不用重抓。
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path

from .model import Observation, WindowMeta


class StoreError(Exception):
    """证据库错误（如内容摘要与目录声明不符）。"""


def parse_observations(meta: WindowMeta, content: bytes) -> tuple:
    """按窗口项目解析样位观察：JSON 给色差，CSV 给照度。"""
    text = content.decode("utf-8")
    if meta.item == "delta_e":
        rows = json.loads(text)["rows"]
        return tuple(
            Observation(
                meta.roll, meta.face, meta.id, str(row["sample"]),
                int(row["x"]), int(row["y"]), "delta_e", Decimal(str(row["delta_e"])),
            )
            for row in rows
        )
    reader = csv.DictReader(StringIO(text))
    return tuple(
        Observation(
            meta.roll, meta.face, meta.id, str(row["sample"]),
            int(row["x"]), int(row["y"]), "light", int(row["light_pass"]),
        )
        for row in reader
    )


class EvidenceStore:
    def __init__(self, path: str | None = None):
        self._dir = Path(path) if path else None
        self._meta: dict[str, WindowMeta] = {}
        self._content: dict[str, bytes] = {}
        self._obs: dict[str, tuple] = {}
        self._requests: dict[str, list] = {}
        if self._dir and self._dir.exists():
            self._load()

    # ---- 已取原件 ----
    def has(self, window_id: str) -> bool:
        return window_id in self._obs

    def meta(self, window_id: str) -> WindowMeta | None:
        return self._meta.get(window_id)

    def window_ids(self) -> list:
        return sorted(self._obs)

    def put(self, meta: WindowMeta, content: bytes, clock: int) -> None:
        digest = sha256(content).hexdigest()
        if meta.sha256 and digest != meta.sha256:
            raise StoreError(f"窗口 {meta.id} 内容摘要与目录声明不符")
        self._meta[meta.id] = meta
        self._content[meta.id] = bytes(content)
        self._obs[meta.id] = parse_observations(meta, content)
        if self._dir:
            self._save(meta, content, clock)

    def observations(self, roll=None, face=None, item=None) -> list:
        out = []
        for window_id, obs in self._obs.items():
            meta = self._meta[window_id]
            if roll is not None and meta.roll != roll:
                continue
            if face is not None and meta.face != face:
                continue
            out.extend(o for o in obs if item is None or o.item == item)
        return out

    # ---- 作废与有效观察 ----
    @staticmethod
    def voided_samples(windows, clock: int) -> set:
        """目录已公开窗口声明作废的 (document, sample, item)。

        声明作废后旧项目不能继续充足；窗口中其他项目、其他样位不受影响。
        """
        void = set()
        for w in windows:
            if w.catalog_turn <= clock and w.replaces_document:
                for sample in w.replaces_samples:
                    void.add((w.replaces_document, sample, w.item))
        return void

    @staticmethod
    def voided_by(windows, clock: int) -> dict:
        """(document, sample, item) → 声明作废它的窗口 id。"""
        out = {}
        for w in windows:
            if w.catalog_turn <= clock and w.replaces_document:
                for sample in w.replaces_samples:
                    out[(w.replaces_document, sample, w.item)] = w.id
        return out

    def valid_observations(self, roll, face, item, rect, windows, clock) -> list:
        """范围内、未被声明作废的已取观察。"""
        void = self.voided_samples(windows, clock)
        return [
            o
            for o in self.observations(roll, face, item)
            if rect.contains(o.x, o.y) and (o.document, o.sample, o.item) not in void
        ]

    # ---- 稳定领件号 ----
    def current_request_id(self, order_id: str, window_id: str) -> str | None:
        used = self._requests.get(f"{order_id}~{window_id}", [])
        return used[-1] if used else None

    def mint_request_id(self, order_id: str, window_id: str) -> str:
        """生成下一个稳定领件号；相同领件号只能原样重试。"""
        base = f"{order_id}~{window_id}"
        used = self._requests.setdefault(base, [])
        request = base if not used else f"{base}~{len(used) + 1}"
        used.append(request)
        self._save_requests()
        return request

    # ---- 持久化 ----
    def _save(self, meta: WindowMeta, content: bytes, clock: int) -> None:
        (self._dir / "windows").mkdir(parents=True, exist_ok=True)
        (self._dir / "blobs").mkdir(parents=True, exist_ok=True)
        row = meta.to_row()
        row["obtained_clock"] = clock
        (self._dir / "windows" / f"{meta.id}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        (self._dir / "blobs" / meta.id).write_bytes(content)

    def _save_requests(self) -> None:
        if not self._dir:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "tickets.json").write_text(
            json.dumps(self._requests, ensure_ascii=False, indent=2)
        )

    def _load(self) -> None:
        tickets = self._dir / "tickets.json"
        if tickets.exists():
            self._requests = {k: list(v) for k, v in json.loads(tickets.read_text()).items()}
        for row_file in sorted((self._dir / "windows").glob("*.json")):
            meta = WindowMeta.from_row(json.loads(row_file.read_text()))
            content = (self._dir / "blobs" / meta.id).read_bytes()
            if meta.sha256 and sha256(content).hexdigest() != meta.sha256:
                raise StoreError(f"证据库中窗口 {meta.id} 字节被改动")
            self._meta[meta.id] = meta
            self._content[meta.id] = content
            self._obs[meta.id] = parse_observations(meta, content)
