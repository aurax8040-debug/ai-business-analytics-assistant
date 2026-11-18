from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import actions, charts, semantics
from server import config
from server.api import dispatch
from server.db import connect


class PublicDataTest(unittest.TestCase):
    def test_bundled_database_is_the_default(self):
        self.assertEqual(config.ETL_DB, config.ROOT / "data" / "bi_data_layer_v1.db")
        self.assertTrue(config.ETL_DB.exists())

    def test_public_dimensions_are_generic(self):
        with connect() as conn:
            regions = {r[0] for r in conn.execute(
                "SELECT DISTINCT region_name_std FROM fact_erp_shipment "
                "WHERE region_scope_status='IN_SCOPE'")}
            customers = [r[0] for r in conn.execute(
                "SELECT customer_name FROM dim_customer WHERE customer_key>0 LIMIT 10")]
            stores = [r[0] for r in conn.execute(
                "SELECT store_name FROM dim_store WHERE store_key>0 LIMIT 10")]
            skus = [r[0] for r in conn.execute(
                "SELECT sku_code FROM dim_sku WHERE sku_key>0 LIMIT 10")]
        self.assertEqual(regions, {"战区A", "战区B", "战区C", "战区D", "战区E"})
        self.assertTrue(all(name.startswith("客户") for name in customers))
        self.assertTrue(all(name.startswith("示例门店") for name in stores))
        self.assertTrue(all(code.startswith("SKU-") for code in skus))

    def test_core_http_endpoints(self):
        for endpoint in ("/api/health", "/api/meta", "/api/dashboard", "/api/cockpit"):
            status, payload = dispatch(endpoint)
            self.assertEqual(status, 200, (endpoint, payload))
            self.assertTrue(payload.get("ok"), (endpoint, payload))


class ChatBITrustTest(unittest.TestCase):
    def test_ambiguous_sales_volume_requires_clarification(self):
        result = semantics.resolve("战区B 10 月销量多少", {})
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["route_tools"], [])
        self.assertIn("ERP", result["clarification"])
        self.assertIn("POS", result["clarification"])

    def test_unverified_result_never_gets_a_chart(self):
        result = charts.build([], {"status": "blocked", "verified": False}, "BLOCKED", {})
        self.assertFalse(result["available"])
        self.assertFalse(result["verified"])

    def test_verified_tool_values_are_copied_to_chart(self):
        tool_data = [{
            "key": "pos", "tool": "query_pos_performance", "ok": True,
            "source": "MOCK POS", "caliber": "POS Sell-out",
            "groups": [{"name": "by_month", "rows": [
                {"month": 8, "sales_te": 10.0},
                {"month": 9, "sales_te": 12.5},
            ]}],
        }]
        result = charts.build(tool_data, {"status": "passed", "verified": True},
                              "SUCCESS", {"metrics": [{"fields": ["sales_te"]}]})
        self.assertTrue(result["available"])
        self.assertEqual([p["value"] for p in result["points"]], [10.0, 12.5])


class ActionBoundaryTest(unittest.TestCase):
    def test_action_requires_confirmation_and_stays_demo_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                actions, "ACTION_PATH", Path(tmp) / "actions.jsonl"):
            with self.assertRaises(actions.ActionError):
                actions.create({"confirmed": False})
            task = actions.create({
                "confirmed": True,
                "run_id": "public-demo-run",
                "suggestion": "复核示例门店动销",
                "owner": "演示负责人",
                "due_date": "2026-12-01",
            })
            self.assertEqual(task["effect"], "demo_only_no_business_data_write")
            self.assertFalse(task["external_notification"])
            self.assertFalse(task["business_system_write"])


if __name__ == "__main__":
    unittest.main()
