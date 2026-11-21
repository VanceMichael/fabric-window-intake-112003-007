"""裁床检测收件与放行核对的数据结构。

坐标一律毫米、左闭右开 [start, end)：长度方向 x、宽度方向 y。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Rect:
    """半开矩形 [x0,x1) x [y0,y1)。"""

    x0: int
    x1: int
    y0: int
    y1: int

    def intersect(self, other: "Rect") -> "Rect | None":
        x0, x1 = max(self.x0, other.x0), min(self.x1, other.x1)
        y0, y1 = max(self.y0, other.y0), min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rect(x0, x1, y0, y1)

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def __str__(self) -> str:
        return f"[{self.x0},{self.x1})x[{self.y0},{self.y1})"


@dataclass(frozen=True)
class Cut:
    """分切/修边/倒卷/拼接单。复尺单以 supersedes 替换旧单。"""

    id: str
    child: str
    child_span: tuple
    parent: str
    parent_ends: tuple | None
    candidates: tuple
    trim: tuple
    flip_width: bool
    face: str
    known: int
    supersedes: str | None

    @classmethod
    def from_row(cls, row: dict) -> "Cut":
        ends = row.get("parent_ends")
        return cls(
            id=row["id"],
            child=row["child"],
            child_span=tuple(row["child_span"]),
            parent=row["parent"],
            parent_ends=tuple(ends) if ends else None,
            candidates=tuple(tuple(e) for e in row.get("candidate_ends", ())),
            trim=tuple(row["trim"]),
            flip_width=bool(row["flip_width"]),
            face=row["face"],
            known=int(row["known"]),
            supersedes=row.get("supersedes"),
        )

    @property
    def remeasure(self) -> bool:
        """是否复尺单（替换既有分切单）。"""
        return self.supersedes is not None


@dataclass(frozen=True)
class Order:
    """工艺交货单：用途、网格、阈值与本次核查条带。"""

    id: str
    roll: str
    purpose: str
    cells_mm: tuple
    delta_e_max: Decimal | None
    light_required: bool
    indivisible: tuple
    tracks: tuple
    known: int
    deadline: int | None

    @classmethod
    def from_row(cls, row: dict) -> "Order":
        items = row.get("items", {})
        delta = items.get("delta_e_max")
        return cls(
            id=row["id"],
            roll=row["roll"],
            purpose=row.get("purpose", ""),
            cells_mm=tuple(row["cells_mm"]),
            delta_e_max=Decimal(str(delta)) if delta is not None else None,
            light_required=bool(items.get("light_pass", False)),
            indivisible=tuple(tuple(r) for r in row.get("indivisible", ())),
            tracks=tuple(
                Rect(t["span"][0], t["span"][1], t["width"][0], t["width"][1])
                for t in row["tracks"]
            ),
            known=int(row["known"]),
            deadline=row.get("deadline"),
        )

    @property
    def required_items(self) -> tuple:
        """用途要求核查的项目：色差 / 照度。"""
        items = []
        if self.delta_e_max is not None:
            items.append("delta_e")
        if self.light_required:
            items.append("light")
        return tuple(items)

    def violates(self, item: str, value) -> bool:
        """有效观测是否超出本工单阈值（阈值来自工单，不随来源窗口变化）。"""
        if item == "delta_e":
            return self.delta_e_max is not None and value > self.delta_e_max
        if item == "light":
            return self.light_required and int(value) != 1
        return False


@dataclass(frozen=True)
class WindowMeta:
    """检测机目录窗口：声明的适用范围、时限与替代关系。"""

    id: str
    roll: str
    face: str
    span: tuple
    width: tuple
    catalog_turn: int
    ready: int
    expires: int
    item: str
    media: str
    sha256: str
    replaces_document: str | None
    replaces_samples: tuple

    @classmethod
    def from_row(cls, row: dict) -> "WindowMeta":
        rep = row.get("replaces") or {}
        return cls(
            id=row["id"],
            roll=row["roll"],
            face=row["face"],
            span=tuple(row["span"]),
            width=tuple(row["width"]),
            catalog_turn=int(row["catalog_turn"]),
            ready=int(row["ready"]),
            expires=int(row["expires"]),
            item=row["item"],
            media=row.get("media", ""),
            sha256=row.get("sha256", ""),
            replaces_document=rep.get("document"),
            replaces_samples=tuple(rep.get("samples", ())),
        )

    def to_row(self) -> dict:
        row = {
            "id": self.id,
            "roll": self.roll,
            "face": self.face,
            "span": list(self.span),
            "width": list(self.width),
            "catalog_turn": self.catalog_turn,
            "ready": self.ready,
            "expires": self.expires,
            "item": self.item,
            "media": self.media,
            "sha256": self.sha256,
        }
        if self.replaces_document:
            row["replaces"] = {
                "document": self.replaces_document,
                "samples": list(self.replaces_samples),
            }
        return row

    @property
    def rect(self) -> Rect:
        return Rect(self.span[0], self.span[1], self.width[0], self.width[1])

    def retrievable(self, clock: int) -> bool:
        """ready 含当轮，expires 不含当轮。"""
        return self.ready <= clock < self.expires


@dataclass(frozen=True)
class Observation:
    """原件中的一个样位观察。"""

    roll: str
    face: str
    document: str
    sample: str
    x: int
    y: int
    item: str
    value: object  # Decimal（色差）或 int（照度 0/1）


@dataclass(frozen=True)
class LedgerEntry:
    """装卸凭据：备料、离库、退回三类事实，不被后来重算的卷位替换。"""

    ref: str
    piece: str
    order: str | None
    roll: str
    span: tuple
    state: str
    cuts: tuple
    evidence: tuple
    destination: str | None
    location: str | None
    part_of: str | None
    accepted_order: str | None
    known: int

    @classmethod
    def from_row(cls, row: dict) -> "LedgerEntry":
        return cls(
            ref=row["ref"],
            piece=row["piece"],
            order=row.get("order"),
            roll=row["roll"],
            span=tuple(row["span"]),
            state=row["state"],
            cuts=tuple(row.get("cuts", ())),
            evidence=tuple(row.get("evidence", ())),
            destination=row.get("destination"),
            location=row.get("location"),
            part_of=row.get("part_of"),
            accepted_order=row.get("accepted_order"),
            known=int(row["known"]),
        )
