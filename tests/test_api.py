"""HTTP 接口冒烟：错误归类状态码、风险报告结构、演示数据端点。"""
from __future__ import annotations

import logging
import unittest

from fastapi.testclient import TestClient

from app.api import create_app
from app.service import RiskService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("api")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(RiskService()))

    def test_wallet_ingest_duplicate_conflict(self) -> None:
        r1 = self.client.post("/wallets", json={"address": "A", "label": "a"})
        r2 = self.client.post("/wallets", json={"address": "A", "label": "a"})
        r3 = self.client.post("/wallets", json={"address": "A", "label": "other"})
        log.info("HTTP 输入: 钱包 A 提交3次(一致/一致/改label) -> %s/%s/%s",
                 r1.json()["status"], r2.json()["status"], r3.status_code)
        self.assertEqual(r1.json()["status"], "ACCEPTED")
        self.assertEqual(r2.json()["status"], "DUPLICATE")
        self.assertEqual(r3.status_code, 409)
        self.assertEqual(r3.json()["detail"]["error"], "CONTENT_CONFLICT")
        self.assertIn("label", r3.json()["detail"]["reason"])

    def test_transaction_lifecycle(self) -> None:
        for a in ("A", "B"):
            self.client.post("/wallets", json={"address": a})
        payload = {"tx_id": "t1", "block": 1, "tx_index": 0,
                   "sender": "A", "receiver": "B", "amount": 100}
        self.assertEqual(self.client.post("/transactions", json=payload).json()["status"],
                         "ACCEPTED")
        self.assertEqual(self.client.post("/transactions", json=payload).json()["status"],
                         "DUPLICATE")
        bad = dict(payload, amount=1)
        r = self.client.post("/transactions", json=bad)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["reason"], "交易字段冲突[amount]")
        invalid = {"tx_id": "t2", "block": 0, "sender": "A", "receiver": "Z", "amount": 1}
        r = self.client.post("/transactions", json=invalid)
        self.assertEqual(r.status_code, 422)
        log.info("HTTP 判定依据: ACCEPTED/DUPLICATE/409(amount冲突)/422(未知钱包) 全部正确归类")

    def test_risk_report_is_explainable(self) -> None:
        client = TestClient(create_app(RiskService.bootstrap_demo()))
        r = client.get("/wallets/SUS/risk")
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["verdict"], "HIGH")
        triggered = [f for f in body["factors"] if f["triggered"]]
        self.assertGreaterEqual(len(triggered), 3)
        for f in triggered:
            self.assertTrue(f["explanation"])
            self.assertIsInstance(f["data_points"], dict)
        log.info("HTTP 输入: GET /wallets/SUS/risk -> verdict=%s score=%s；触发因子及依据:",
                 body["verdict"], body["score"])
        for f in triggered:
            log.info("  - %s: %s", f["code"], f["explanation"])

    def test_insufficient_data_via_http(self) -> None:
        self.client.post("/wallets", json={"address": "SOLO"})
        body = self.client.get("/wallets/SOLO/risk").json()
        self.assertEqual(body["verdict"], "UNDETERMINED")
        self.assertTrue(body["undetermined_reasons"])
        log.info("HTTP 判定依据: 空钱包风险结论=%s，原因数=%d（未默认安全）",
                 body["verdict"], len(body["undetermined_reasons"]))

    def test_snapshot_and_missing(self) -> None:
        self.assertEqual(self.client.get("/wallets/NOPE/risk").status_code, 404)
        root = self.client.get("/").json()
        self.assertIn("service", root)
        snap = self.client.get("/ledger/snapshot").json()
        self.assertEqual(snap["transaction_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
