"""Public preparation contract checks using invented business records only."""

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import duckdb

from tabular_agent.preparation import (
    PreparationConfig,
    PreparationSpec,
    load_agent_schema,
    prepare_business,
    profile_business,
)


RULEBOOK = """# Current business rule excerpts
| Đơn tính vào doanh thu | đơn **không huỷ** và **không trả hàng**. Đơn có trả hàng, **kể cả chỉ trả một phần**, là đơn trả hàng: bỏ **cả đơn**. Đơn đang giao vẫn tính |
| Hai dòng của **cùng một đơn** giống nhau ở mọi ô | lỗi xuất file, tính một lần. Hai dòng giống nhau của hai đơn khác nhau là hai lần bán, tính cả hai |
| Bảng không ghi theo đơn | mỗi dòng là một lần ghi; hai dòng giống nhau trong cùng file vẫn tính cả hai |
"""


def source_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


class PreparationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="preparation-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.split = self.root / "split"
        self.business = self.split / "data" / "sample_shop"
        self.business.mkdir(parents=True)
        self.readme = self.business / "README.md"
        self.readme.write_text("# Source documentation\nData values use VND.\n", encoding="utf-8")
        self.rulebook = self.split / "rulebook.md"
        self.rulebook.write_text(RULEBOOK, encoding="utf-8")
        self.calendar = self.split / "calendar.csv"
        self.calendar.write_text("solar_date,lunar_day,lunar_month,lunar_year\n2025-01-01,2,12,2024\n", encoding="utf-8")
        self.output = self.root / "prepared"
        self.config = PreparationConfig(batch_size=2, sample_rows=16)

    def prepare(self, **kwargs):
        return prepare_business(self.business, self.rulebook, self.calendar,
                                output=self.output, config=self.config, **kwargs)

    def profile(self, **kwargs):
        return profile_business(self.business, self.rulebook, self.calendar,
                                config=self.config, **kwargs)

    def write_order_system(self, *, bad_amount=False, duplicate=True):
        orders = [
            ["order_id", "order_date", "status", "order_goods_amount", "order_total", "customer_name", "email"],
            ["001", "2025-01-01", "completed", "100.12", "100.12", "PRIVATE_CUSTOMER_4455", ""],
            ["002", "2025-01-02", "cancelled", "200.34", "200.34", "PRIVATE_CUSTOMER_9900", ""],
        ]
        items = [
            ["order_id", "line_number", "product_id", "product_name", "quantity", "base_quantity", "line_amount", "unit_price"],
            ["001", "1", "P001", "PRIVATE_PRODUCT_7788", "2", "2", "bad amount" if bad_amount else "100.12", "50.06"],
            ["002", "1", "P002", "PRIVATE_PRODUCT_8899", "3", "3", "200.34", "66.78"],
        ]
        if duplicate:
            orders.append(list(orders[1]))
            items.append(list(items[1]))
        write_csv(self.business / "orders.csv", orders)
        write_csv(self.business / "order_items.csv", items)
        write_csv(self.business / "payments.csv", [
            ["payment_id", "order_id", "payment_date", "payment_amount"],
            ["PAY1", "001", "2025-01-01", "60.00"],
            ["PAY2", "001", "2025-01-02", "40.12"],
            ["PAY3", "002", "2025-01-02", "200.34"],
        ])

    def test_publishes_typed_entities_and_safe_many_to_one_relationships(self):
        self.write_order_system()
        before = source_hashes(self.split)
        bundle = self.prepare()
        self.assertEqual(source_hashes(self.split), before)
        self.assertTrue(bundle.database.is_file())
        self.assertFalse(bundle.path.is_relative_to(self.split))
        self.assertFalse(bundle.cache_hit)
        schema = load_agent_schema(bundle)
        relations = {r["name"]: r for r in schema.relations}
        self.assertEqual(relations["clean.orders"]["grain"], "order")
        self.assertEqual(relations["clean.orders"]["keys"], ["order_id"])
        self.assertEqual(relations["clean.order_items"]["grain"], "order_item")
        self.assertEqual(relations["clean.order_items"]["keys"], ["order_id", "line_number"])
        payment_join = next(r for r in schema.relationships if r["child"] == "clean.payments" and r["parent"] == "clean.orders")
        self.assertEqual(payment_join["cardinality"], "many_to_one")
        self.assertEqual(payment_join["join_multiplier"], 1)
        self.assertTrue(payment_join["validated"])
        with duckdb.connect(str(bundle.database), read_only=True) as connection:
            self.assertEqual(connection.execute("SELECT order_id FROM clean.orders ORDER BY order_id").fetchall(), [("001",), ("002",)])
            self.assertEqual(connection.execute("SELECT count(*) FROM clean.order_items").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM clean.payments").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT count(*) FROM semantic.eligible_orders").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT sum(line_amount) FROM semantic.eligible_sales_lines").fetchone()[0], Decimal("100.12"))
            # Every original row survives privately even after allowed deduplication.
            lineage = connection.execute("SELECT count(*), count(DISTINCT canonical_record_id) FROM private.lineage WHERE relation_name='orders'").fetchone()
            self.assertEqual(lineage, (3, 2))
        profile = json.loads(bundle.profile_path.read_text())
        self.assertEqual(profile["measurements"]["duplicates_removed"], 2)
        self.assertEqual(profile["reconciliations"][0]["compared"], 2)
        self.assertEqual(profile["reconciliations"][0]["mismatches"], 0)

    def test_full_scan_accounts_for_title_blank_summary_and_note_rows(self):
        write_csv(self.business / "sales.csv", [
            ["Sales report"],
            [],
            ["order_date", "product_name", "quantity", "line_amount"],
            ["2025-01-01", "Item A", "2", "12.50"],
            ["2025-01-01", "Tổng ngày", "1", "4.25"],
            ["Total", "", "3", "16.75"],
            ["Note: exported from local register"],
        ])
        before = source_hashes(self.split)
        profile = self.profile()
        self.assertEqual(source_hashes(self.split), before)
        self.assertEqual(len(profile.tables), 1)
        table = profile.tables[0]
        self.assertEqual(table.accounting, {"title": 1, "blank": 1, "header": 1, "record": 2, "summary": 1, "note": 1})
        self.assertEqual(sum(table.accounting.values()), 7)
        self.assertEqual(table.records, 2)
        self.assertEqual(table.extraction["scan_coverage"], "full")
        bundle = self.prepare()
        with duckdb.connect(str(bundle.database), read_only=True) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM clean.sales WHERE product_name='Tổng ngày'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM private.row_audit WHERE classification='summary'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM private.row_audit WHERE classification='note'").fetchone()[0], 1)

    def test_schema_has_no_record_samples_or_private_profile_values(self):
        self.write_order_system()
        bundle = self.prepare()
        schema = load_agent_schema(bundle)
        public = schema.model_dump_json()
        for secret in ["PRIVATE_CUSTOMER_4455", "PRIVATE_PRODUCT_7788", "PRIVATE_CUSTOMER_9900", "PRIVATE_PRODUCT_8899"]:
            self.assertNotIn(secret, public)
        for key in ["frequent_values", "failure_examples", "values_json", "_raw_fingerprint", "_record_id"]:
            self.assertNotIn(key, public)
        private = bundle.profile_path.read_text()
        self.assertIn("PRIVATE_CUSTOMER_4455", private)
        order_relation = next(r for r in schema.relations if r["name"] == "clean.orders")
        self.assertIn("email", order_relation["absent_columns"])
        self.assertNotIn("email", [c["name"] for c in order_relation["columns"]])
        self.assertEqual(load_agent_schema(bundle.path).model_dump(), schema.model_dump())

    def test_failed_amount_is_blocked_instead_of_silently_summed_as_null(self):
        self.write_order_system(bad_amount=True, duplicate=False)
        bundle = self.prepare()
        schema = load_agent_schema(bundle)
        relation = next(r for r in schema.relations if r["name"] == "clean.order_items")
        self.assertEqual(bundle.status, "partial")
        self.assertIn("line_amount", relation["blocked_columns"])
        self.assertNotIn("line_amount", [c["name"] for c in relation["columns"]])
        self.assertEqual(schema.capabilities["revenue"]["status"], "blocked_by_quality")
        profile = json.loads(bundle.profile_path.read_text())
        table = next(t for t in profile["tables"] if t["name"] == "order_items")
        field = next(c for c in table["columns"] if c["name"] == "line_amount")
        self.assertGreaterEqual(field["parse_failures"], 1)
        self.assertFalse(field["usable"])
        self.assertEqual(table["records"], 2)
        with duckdb.connect(str(bundle.database), read_only=True) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM clean.order_items").fetchone()[0], 2)
            with self.assertRaises(duckdb.BinderException):
                connection.execute("SELECT sum(line_amount) FROM clean.order_items")

    def test_identical_orderless_records_are_preserved(self):
        write_csv(self.business / "sales.csv", [
            ["order_date", "product_name", "quantity", "line_amount"],
            ["2025-01-01", "Item A", "2", "12.50"],
            ["2025-01-01", "Item A", "2", "12.50"],
        ])
        bundle = self.prepare()
        schema = load_agent_schema(bundle)
        relation = next(r for r in schema.relations if r["name"] == "clean.sales")
        self.assertEqual(relation["grain"], "sale_recording")
        self.assertEqual(relation["rows"], 2)
        self.assertEqual(relation["keys"], [])
        self.assertEqual(schema.capabilities["order_count"]["status"], "missing_in_source")
        with duckdb.connect(str(bundle.database), read_only=True) as connection:
            self.assertEqual(connection.execute("SELECT sum(line_amount) FROM clean.sales").fetchone()[0], Decimal("25.00"))
            self.assertEqual(connection.execute("SELECT count(DISTINCT canonical_record_id) FROM private.lineage").fetchone()[0], 2)

    def test_cache_reuses_only_unchanged_inputs(self):
        self.write_order_system(duplicate=False)
        first = self.prepare()
        second = self.prepare()
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.bundle_id, first.bundle_id)
        # Relevant documentation, calendar, and records each invalidate the bundle.
        self.readme.write_text(self.readme.read_text() + "\nA clarified source note.\n", encoding="utf-8")
        readme_change = self.prepare()
        self.assertNotEqual(readme_change.bundle_id, first.bundle_id)
        self.rulebook.write_text(RULEBOOK + "\nFinal amounts use half-up rounding.\n", encoding="utf-8")
        rule_change = self.prepare()
        self.assertNotEqual(rule_change.bundle_id, readme_change.bundle_id)
        self.calendar.write_text(self.calendar.read_text() + "2025-01-02,3,12,2024\n", encoding="utf-8")
        calendar_change = self.prepare()
        self.assertNotEqual(calendar_change.bundle_id, rule_change.bundle_id)
        payments = self.business / "payments.csv"
        with payments.open("a", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(["PAY4", "002", "2025-01-03", "1.00"])
        source_change = self.prepare()
        self.assertNotEqual(source_change.bundle_id, calendar_change.bundle_id)
        self.assertFalse(source_change.cache_hit)

    def test_saved_spec_reproduces_clean_results_without_model_calls(self):
        self.write_order_system()
        first = self.prepare()
        spec = PreparationSpec.model_validate_json((first.path / "spec.json").read_text())
        self.assertTrue(spec.layouts)
        self.assertTrue(spec.tables)
        second = self.prepare(spec=spec)
        self.assertEqual(second.measurements["model_calls"], 0)
        self.assertEqual(load_agent_schema(first).model_dump(), load_agent_schema(second).model_dump())
        with duckdb.connect(str(first.database), read_only=True) as original, duckdb.connect(str(second.database), read_only=True) as replay:
            for relation in ["clean.orders", "clean.order_items", "clean.payments", "semantic.eligible_sales_lines"]:
                query = f"SELECT * FROM {relation} ORDER BY ALL"
                self.assertEqual(original.execute(query).fetchall(), replay.execute(query).fetchall())

    def test_multiple_regions_have_globally_unique_provenance(self):
        write_csv(self.business / "mixed.csv", [
            ["order_id", "order_date", "status"],
            ["001", "2025-01-01", "completed"],
            [],
            ["product_id", "product_name", "quantity"],
            ["P001", "Item A", "2"],
        ])
        write_csv(self.business / "products.csv", [
            ["product_id", "product_name", "unit"],
            ["P002", "Item B", "piece"],
        ])
        profile = self.profile()
        self.assertEqual(len(profile.inventory), 2)
        self.assertEqual(len(profile.tables), 3)
        self.assertEqual(len({t.region_id for t in profile.tables}), 3)
        self.assertEqual(len({t.source_id for t in profile.tables}), 2)
        self.assertEqual(sum(sum(t.accounting.values()) for t in profile.tables), 7)
        self.assertEqual(sum(t.records for t in profile.tables), 3)
        self.assertTrue(all(t.region_id.startswith(t.source_id + ":") for t in profile.tables))
        bundle = self.prepare()
        schema = load_agent_schema(bundle)
        clean = [r for r in schema.relations if r["name"].startswith("clean.") and r["name"] != "clean.calendar"]
        self.assertEqual(len(clean), 3)
        self.assertEqual(sum(r["rows"] for r in clean), 3)
        self.assertIn("clean.calendar", {r["name"] for r in schema.relations})
        replay = self.prepare(spec=PreparationSpec.model_validate_json((bundle.path / "spec.json").read_text()))
        self.assertEqual(load_agent_schema(replay).model_dump(), schema.model_dump())

    def test_decimal_scale_and_negative_values_survive_materialization(self):
        negative = "-12345678901234567890.123456789012345678"
        tiny = "0.000000000000000001"
        write_csv(self.business / "sales.csv", [
            ["order_date", "product_name", "quantity", "line_amount"],
            ["2025-01-01", "Item A", "1", negative],
            ["2025-01-02", "Item B", "1", tiny],
        ])
        bundle = self.prepare()
        with duckdb.connect(str(bundle.database), read_only=True) as connection:
            self.assertEqual(connection.execute("SELECT line_amount FROM clean.sales ORDER BY order_date").fetchall(), [(Decimal(negative),), (Decimal(tiny),)])
            type_name = connection.execute("DESCRIBE clean.sales").fetchall()
            self.assertEqual(next(c[1] for c in type_name if c[0] == "line_amount"), "DECIMAL(38,18)")

    def test_output_cannot_be_placed_inside_source_split(self):
        self.write_order_system(duplicate=False)
        before = source_hashes(self.split)
        with self.assertRaisesRegex(ValueError, "separate"):
            prepare_business(self.business, self.rulebook, self.calendar, output=self.split / "prepared")
        self.assertEqual(source_hashes(self.split), before)


if __name__ == "__main__":
    unittest.main()
