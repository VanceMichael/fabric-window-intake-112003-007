"""色差仪窗口目录、检测原件和装卸事实。"""
from argparse import ArgumentParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    def data(name):
        return json.loads((root / "data" / (name + ".json")).read_text())

    config = data("sources")
    clock = 0
    tickets = {}
    spent = {}

    class Mill(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, payload, media="application/json", digest=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            if digest:
                self.send_header("X-Content-SHA256", digest)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            raw = urlsplit(self.path)
            q = {key: value[0] for key, value in parse_qs(raw.query).items()}
            try:
                return self.read(raw.path, q)
            except (KeyError, ValueError, IndexError):
                return self.send(400, {"state": "invalid_query"})

        def read(self, path, q):
            if path == "/":
                return self.send(200, {"clock": clock, "entry": "/catalog", "edition": config["edition"]})
            if path == "/catalog":
                beginning, end = int(q["from"]), int(q["until"])
                left, right = int(q["left"]), int(q["right"])
                if beginning >= end or left >= right:
                    return self.send(422, {"state": "invalid_window"})
                rows = [row for row in config["windows"] if row["roll"] == q["roll"] and row["face"] == q["face"]
                        and row["catalog_turn"] <= clock and row["span"][0] < end and beginning < row["span"][1]
                        and row["width"][0] < right and left < row["width"][1]]
                return self.send(200, {"clock": clock, "windows": rows})
            if path in ("/orders", "/cuts", "/warehouse"):
                return self.send(200, {"entries": [row for row in data(path.lstrip("/")) if row["known"] <= clock]})
            if path == "/ticket":
                record = tickets.get(q["request"])
                return self.send(200 if record else 404, record or {"state": "unknown"})
            if path.startswith("/raw/"):
                ident = path.rsplit("/", 1)[1]
                ref = tickets.get(q["ticket"])
                if not ref or ref["id"] != ident:
                    return self.send(403, {"state": "wrong_ticket"})
                if clock >= ref["until"]:
                    return self.send(410, {"state": "ticket_expired"})
                file = root / "originals" / ref["file"]
                payload = file.read_bytes()
                return self.send(200, payload, ref["media"], sha256(payload).hexdigest())
            return self.send(404, {"state": "unknown"})

        def do_POST(self):
            nonlocal clock
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/time":
                    target = request["clock"]
                    if type(target) is not int or target < clock:
                        return self.send(409, {"state": "cannot_rewind"})
                    clock = target
                    return self.send(200, {"clock": clock})
                if self.path == "/ticket":
                    return self.issue(request)
            except (KeyError, TypeError, ValueError):
                return self.send(400, {"state": "invalid_request"})
            return self.send(404, {"state": "unknown"})

        def issue(self, body):
            order, name, request = (body[key] for key in ("order", "id", "request"))
            before = tickets.get(request)
            if before:
                return self.send(200 if (before["order"], before["id"]) == (order, name) else 409, before)
            row = next((row for row in config["windows"] if row["id"] == name and row["catalog_turn"] <= clock), None)
            if not row or clock >= row["expires"]:
                return self.send(410, {"state": "unavailable"})
            if clock < row["ready"]:
                return self.send(425, {"state": "uploading"})
            count = spent.get((order, clock, row["roll"]), 0)
            limit = config["quota"].get(order, {}).get(row["roll"], 0)
            if count >= limit:
                return self.send(429, {"state": "window_limit", "used": count})
            spent[(order, clock, row["roll"])] = count + 1
            record = {"request": request, "id": name, "order": order, "until": row["expires"],
                      "file": row["file"], "media": row["media"], "href": f"/raw/{name}?ticket={request}"}
            tickets[request] = record
            return self.send(201, record)

    return HTTPServer(("127.0.0.1", port), Mill)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    with create_source(parser.parse_args().port) as server:
        print(f"检测资料端在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
