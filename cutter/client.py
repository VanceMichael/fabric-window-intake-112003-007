"""资料端 HTTP 客户端（仅标准库）。"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SourceClient:
    def __init__(self, base: str, timeout: float = 5.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def exchange(self, path: str, payload=None, raw: bool = False):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data)
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            media = response.headers.get("Content-Type", "")
            if not raw and media.startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def clock(self) -> int:
        return self.exchange("/")[2]["clock"]

    def advance(self, clock: int) -> int:
        code, _, body = self.exchange("/time", {"clock": clock})
        if code != 200:
            raise RuntimeError(f"拨钟失败: {body}")
        return body["clock"]

    def orders(self) -> list:
        return self.exchange("/orders")[2]["entries"]

    def cuts(self) -> list:
        return self.exchange("/cuts")[2]["entries"]

    def warehouse(self) -> list:
        return self.exchange("/warehouse")[2]["entries"]

    def catalog(self, roll: str, face: str, rect) -> list:
        query = urlencode(
            {
                "roll": roll,
                "face": face,
                "from": rect.x0,
                "until": rect.x1,
                "left": rect.y0,
                "right": rect.y1,
            }
        )
        return self.exchange(f"/catalog?{query}")[2]["windows"]

    def ticket_lookup(self, request_id: str):
        code, _, body = self.exchange(f"/ticket?{urlencode({'request': request_id})}")
        return code, body

    def ticket_request(self, order_id: str, window_id: str, request_id: str):
        code, _, body = self.exchange(
            "/ticket", {"order": order_id, "id": window_id, "request": request_id}
        )
        return code, body

    def download(self, href: str):
        code, headers, body = self.exchange(href, raw=True)
        return code, headers.get("X-Content-SHA256"), body
