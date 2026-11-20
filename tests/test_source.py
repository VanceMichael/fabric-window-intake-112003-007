import json
from hashlib import sha256
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source


class MillTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def exchange(self, path, payload=None, headers=None, raw=False):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if not raw and response.headers["Content-Type"].startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def advance(self, turn):
        self.assertEqual(self.exchange("/time", {"clock": turn})[0], 200)

    def catalog(self, roll, face, start, stop, left, right):
        route = f"/catalog?roll={roll}&face={face}&from={start}&until={stop}&left={left}&right={right}"
        return self.exchange(route)[2]["windows"]

    def test_spatial_catalog_limits_retrieval_and_exposes_material_expiry(self):
        self.advance(1)
        early = self.catalog("M10", "front", 1700, 1860, 100, 370)
        self.assertEqual([item["id"] for item in early], ["E1", "E1L", "E2", "E2L"])
        alternate = self.catalog("M10", "back", 900, 1860, 100, 370)
        self.assertEqual([item["id"] for item in alternate], ["EX"])
        for i, name in enumerate(["E2", "E2L"]):
            req = {"order":"A30", "id":name, "request":f"X{i}"}
            ticket = self.exchange("/ticket", req)[2]
            status, _, payload = self.exchange(ticket["href"], raw=True)
            self.assertEqual(status, 200)
            expected = next(item["sha256"] for item in early if item["id"] == name)
            self.assertEqual(expected, sha256(payload).hexdigest())
        self.assertEqual(self.exchange("/ticket", {"order":"A30","id":"E1","request":"X3"})[0], 429)
        self.advance(3)
        self.assertEqual(self.exchange("/raw/E2?ticket=X0")[0], 410)

    def test_replacement_is_one_item_while_physical_returns_stay_linked(self):
        self.advance(2)
        req = {"order":"A20", "id":"E5", "request":"COLOR5"}
        self.assertEqual(self.exchange("/ticket", req)[0], 425)
        self.assertEqual(self.exchange("/warehouse")[2]["entries"][0]["cuts"], ["C1"])
        revised = self.exchange("/cuts")[2]["entries"][-1]
        self.assertEqual((revised["id"], revised["supersedes"]), ("RR1", "C1"))
        self.advance(3)
        win = next(x for x in self.catalog("M11", "front", 0, 200, 0, 600) if x["id"] == "E5")
        self.assertEqual(win["replaces"], {"document":"E3", "samples":["M11-75-150"]})
        ticket = self.exchange("/ticket", req)[2]
        self.assertEqual(self.exchange(ticket["href"])[2]["rows"][0]["sample"], "M11-75-150")
        self.advance(4)
        record = self.exchange("/warehouse")[2]["entries"][-1]
        self.assertEqual((record["part_of"], record["piece"], record["span"]), ("OUT80", "P800", [0, 200]))
