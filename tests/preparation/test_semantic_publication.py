"""Semantic SQL publication checks against invented data and explicit rules."""

from datetime import date, datetime
from decimal import Decimal
import unittest

import duckdb

from tabular_agent.preparation.contracts import BusinessProfile, ColumnProfile, TableProfile
from tabular_agent.preparation.pipeline import _schema


RULES = """
| Đơn tính vào doanh thu | đơn không huỷ và không trả hàng. Đơn có trả hàng, kể cả chỉ trả một phần, là đơn trả hàng: bỏ cả đơn. Đơn đang giao vẫn tính |
Riêng trạng thái huỷ, trả hàng của đơn lấy theo trạng thái ghi trên đơn, kể cả khi phiếu trả ghi ngày sau hôm nay.
Đơn vị không có dòng quy đổi nào là đơn vị cơ bản, hệ số 1.
"""


class SemanticPublication(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        for schema in ("clean", "semantic", "private"):
            self.db.execute("CREATE SCHEMA " + schema)
        self.profile = BusinessProfile(business="invented", fingerprint="fixture")
        self.relations = []

    def table(self, name, grain, fields, rows, keys=(), labels=None):
        columns = list(fields)
        self.db.execute("CREATE TABLE clean." + name + " (" + ",".join('"' + c + '" ' + fields[c] for c in columns) + ")")
        if rows:
            self.db.executemany("INSERT INTO clean." + name + " VALUES (" + ",".join("?" for _ in fields) + ")", rows)
        relation = {"name": "clean." + name, "grain": grain, "keys": list(keys),
                    "roles": {column: column for column in columns}, "rows": len(rows),
                    "columns": [{"name": c, "role": c, "type": fields[c], "meaning": c,
                                 "source_alias": (labels or {}).get(c, c)} for c in columns],
                    "source_regions": [], "blocked_columns": [], "absent_columns": []}
        self.relations.append(relation)
        return relation

    def orders(self, statuses=("completed", "completed")):
        return self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE",
            "status": "VARCHAR", "order_total": "DECIMAL(20,2)"},
            [(str(i + 1), "2025-01-01", status, 100) for i, status in enumerate(statuses)], ["order_id"])

    def items(self, *, base=True, quantities=(1, 2), statuses=None):
        fields = {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_name": "VARCHAR",
                  "quantity": "DECIMAL(20,2)", "line_amount": "DECIMAL(20,2)", "unit": "VARCHAR"}
        if base:
            fields["base_quantity"] = "DECIMAL(20,2)"
        if statuses:
            fields["order_date"] = "DATE"
            fields["status"] = "VARCHAR"
        rows = []
        for i, quantity in enumerate(quantities):
            row = [str(i + 1), "1", "Invented product", quantity, 10 * (i + 1), "piece"]
            if base:
                row.append(quantity)
            if statuses:
                row.extend(["2025-01-01", statuses[i]])
            rows.append(row)
        return self.table("items", "order_item", fields, rows, ["order_id", "line_number"])

    def schema(self, readme="", rules=RULES):
        return _schema(self.db, self.profile, self.relations, readme, rules)

    def names(self, schema):
        return {relation["name"] for relation in schema.relations}

    def test_whole_order_returns_use_documented_negative_quantities(self):
        self.orders()
        self.items(quantities=(1, -1))
        readme = "- Trả hàng: hàng trả ghi số lượng âm, cùng mã đơn với đơn đã bán: đơn có dòng số lượng âm là đơn có trả hàng."
        schema = self.schema(readme)
        self.assertEqual(self.db.execute("SELECT order_id FROM semantic.eligible_orders").fetchall(), [("1",)])
        self.assertEqual(schema.capabilities["product_quantities"]["status"], "available")
        self.assertEqual(self.db.execute("SELECT sum(base_quantity) FROM semantic.product_quantities").fetchone()[0], Decimal(1))

    def test_undocumented_negative_quantity_does_not_imply_return(self):
        self.orders()
        self.items(quantities=(1, -1))
        self.schema("Negative adjustments can occur.")
        self.assertEqual(self.db.execute("SELECT count(*) FROM semantic.eligible_orders").fetchone()[0], 2)

    def test_partial_sales_policy_never_publishes_eligible_views(self):
        self.orders()
        self.items()
        schema = self.schema(rules="Sales eligibility: exclude cancelled orders, whole orders.")
        self.assertNotIn("semantic.eligible_orders", self.names(schema))

    def test_unknown_status_is_not_treated_as_active(self):
        self.orders(statuses=("completed", "ready"))
        self.items()
        schema = self.schema()
        self.assertNotIn("semantic.eligible_orders", self.names(schema))
        self.assertTrue(any(i.code == "UNKNOWN_ORDER_STATUS" for i in self.profile.issues))

    def test_positive_return_quantities_need_documented_meaning(self):
        self.orders()
        self.items()
        self.table("allocations", "lot_allocation", {"order_id": "VARCHAR", "return_quantity": "DECIMAL(10,0)"}, [("2", 1)])
        self.schema()
        self.assertEqual(self.db.execute("SELECT count(*) FROM semantic.eligible_orders").fetchone()[0], 2)

    def test_documented_positive_return_excludes_entire_order(self):
        self.orders()
        self.items()
        self.table("allocations", "lot_allocation", {"order_id": "VARCHAR", "return_quantity": "DECIMAL(10,0)"}, [("2", 1)])
        self.schema("- Số lượng trả: số lượng khách trả lại (đơn vị cơ bản); lớn hơn 0: đơn đó có trả hàng")
        self.assertEqual(self.db.execute("SELECT order_id FROM semantic.eligible_orders").fetchall(), [("1",)])

    def test_unlinked_return_records_block_eligibility(self):
        self.orders()
        self.items()
        self.table("returns", "return", {"order_id": "VARCHAR"}, [("other-system",)])
        schema = self.schema()
        self.assertNotIn("semantic.eligible_orders", self.names(schema))
        self.assertTrue(any(i.code == "RETURN_LINK_UNRESOLVED" for i in self.profile.issues))

    def test_order_dates_must_functionally_depend_on_real_order_id(self):
        self.table("items", "order_item", {"order_id": "VARCHAR", "order_date": "DATE",
            "status": "VARCHAR", "product_name": "VARCHAR", "line_amount": "DECIMAL(20,2)"},
            [("1", "2025-01-01", "completed", "A", 10), ("1", "2025-01-02", "completed", "B", 20)])
        schema = self.schema()
        self.assertNotIn("semantic.orders", self.names(schema))
        self.assertTrue(any(i.code == "ORDER_GRAIN_UNRESOLVED" for i in self.profile.issues))

    def sales_book(self, rows):
        """A line-based sales book: one row per line, the order's date and status repeated on each line."""
        return self.table("sales_book", "order_item", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
            "product_name": "VARCHAR", "quantity": "DECIMAL(20,2)", "line_amount": "DECIMAL(20,2)"}, rows)

    def test_a_sheet_of_order_ids_without_order_facts_is_not_the_order_table(self):
        # Credit terms list only credit orders: an order key, a day count and a due date, no order fact.
        self.table("credit_terms", "order", {"order_id": "VARCHAR", "days": "DECIMAL(5,0)"}, [("2", 30)], ["order_id"])
        self.sales_book([("1", "2025-01-01", "completed", "A", 1, 10), ("2", "2025-01-02", "completed", "B", 2, 20)])
        schema = self.schema()
        self.assertIn("semantic.eligible_sales_lines", self.names(schema))
        self.assertEqual(self.db.execute("SELECT count(*) FROM semantic.eligible_orders").fetchone()[0], 2)
        self.assertFalse(any(i.code in ("ORPHAN_KEYS", "ORDER_SYSTEM_AMBIGUOUS") for i in self.profile.issues))

    def test_an_order_system_without_status_names_its_issue(self):
        self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE"}, [("1", "2025-01-01")], ["order_id"])
        schema = self.schema()
        self.assertNotIn("semantic.orders", self.names(schema))
        self.assertTrue(any(i.code == "ORDER_STATUS_MISSING" for i in self.profile.issues))

    def test_a_status_column_that_failed_cleaning_is_named(self):
        orders = self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE"}, [("1", "2025-01-01")],
                            ["order_id"])
        orders["source_regions"] = ["source:1"]
        self.profile.tables.append(TableProfile(source_id="source", region_id="source:1", source={}, name="orders",
            labels=["progress"], header_rows=[1], grain="order", roles={"status": "progress"},
            columns=[ColumnProfile(name="progress", label="progress", position=0, role="status", usable=False,
                                   parser={"kind": "decimal"})]))
        self.schema()
        issue = next(i for i in self.profile.issues if i.code.startswith("ORDER_STATUS"))
        self.assertEqual(issue.code, "ORDER_STATUS_BLOCKED")
        self.assertIn("progress", issue.message)

    def test_a_delivery_book_is_not_a_sales_line_table(self):
        self.sales_book([("1", "2025-01-01", "completed", "A", 1, 10), ("2", "2025-01-02", "completed", "B", 2, 20)])
        self.table("deliveries", "order_item", {"order_id": "VARCHAR", "product_name": "VARCHAR",
                                                "base_quantity": "DECIMAL(20,2)"}, [("1", "A", 1), ("2", "B", 2)])
        schema = self.schema()
        self.assertIn("semantic.eligible_sales_lines", self.names(schema))
        self.assertEqual(self.db.execute("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines").fetchone()[0],
                         Decimal(30))

    def test_a_line_table_without_sale_facts_is_used_when_it_is_the_only_one(self):
        self.orders()
        self.table("lines", "order_item", {"order_id": "VARCHAR", "product_name": "VARCHAR",
                                           "base_quantity": "DECIMAL(20,2)"}, [("1", "A", 1), ("2", "B", 2)])
        schema = self.schema()
        self.assertIn("semantic.eligible_sales_lines", self.names(schema))
        self.assertEqual(self.db.execute("SELECT SUM(base_quantity) FROM semantic.eligible_sales_lines").fetchone()[0],
                         Decimal(3))

    def test_an_order_that_could_not_be_delivered_is_cancelled(self):
        self.sales_book([("1", "2025-01-01", "Đã giao", "A", 1, 10), ("2", "2025-01-02", "Không giao được", "B", 2, 20)])
        self.schema()
        self.assertEqual(self.db.execute("SELECT order_id, order_state FROM semantic.orders ORDER BY 1").fetchall(),
                         [("1", "completed"), ("2", "cancelled")])

    def test_a_return_line_dated_later_keeps_the_order_on_its_sale_day(self):
        readme = "- Trả hàng: hàng trả ghi số lượng âm, cùng mã đơn với đơn đã bán: đơn có dòng số lượng âm là đơn có trả hàng."
        rows = [("1", "2025-01-01", "completed", "A", 1, 10), ("1", "2025-01-05", "completed", "A", -1, -10),
                ("2", "2025-01-02", "completed", "B", 2, 20)]
        self.sales_book(rows)
        schema = self.schema(readme)
        self.assertIn("semantic.eligible_sales_lines", self.names(schema))
        self.assertEqual(self.db.execute("SELECT order_id, order_date, order_state FROM semantic.orders ORDER BY 1").fetchall(),
                         [("1", date(2025, 1, 1), "returned"), ("2", date(2025, 1, 2), "completed")])
        # Without the README rule a second date on an order is still a conflict.
        self.setUp()
        self.sales_book(rows)
        schema = self.schema()
        self.assertNotIn("semantic.orders", self.names(schema))
        self.assertTrue(any(i.code == "ORDER_GRAIN_UNRESOLVED" for i in self.profile.issues))

    def test_a_return_line_timed_later_keeps_the_order_time_and_shift(self):
        readme = "- Trả hàng: hàng trả ghi số lượng âm, cùng mã đơn với đơn đã bán: đơn có dòng số lượng âm là đơn có trả hàng."
        rules = RULES + "\n| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59 |\n"
        self.table("sales_book", "order_item", {"order_id": "VARCHAR", "order_date": "DATE", "created_at": "TIMESTAMP",
            "status": "VARCHAR", "product_name": "VARCHAR", "quantity": "DECIMAL(20,2)", "line_amount": "DECIMAL(20,2)"},
            [("1", "2025-01-01", "2025-01-01 09:00:00", "completed", "A", 1, 10),
             ("1", "2025-01-05", "2025-01-05 15:30:00", "completed", "A", -1, -10),
             ("2", "2025-01-02", "2025-01-02 14:00:00", "completed", "B", 2, 20)])
        self.schema(readme, rules)
        self.assertEqual(self.db.execute("SELECT order_id, created_at, shift FROM semantic.orders ORDER BY 1").fetchall(),
                         [("1", datetime(2025, 1, 1, 9, 0), "morning"), ("2", datetime(2025, 1, 2, 14, 0), "afternoon")])

    def test_unrelated_receipt_quantities_do_not_establish_sales_quantities(self):
        self.table("receipts", "receipt_item", {"base_quantity": "DECIMAL(20,2)", "product_name": "VARCHAR"}, [(10, "A")])
        schema = self.schema()
        self.assertNotEqual(schema.capabilities["product_quantities"]["status"], "available")

    def test_columns_on_unconnected_payment_tables_do_not_establish_history(self):
        self.table("dates", "payment", {"payment_date": "DATE"}, [("2025-01-01",)])
        self.table("amounts", "payment", {"payment_amount": "DECIMAL(20,2)"}, [(10,)])
        schema = self.schema()
        self.assertNotEqual(schema.capabilities["payment_history"]["status"], "available")
        self.assertNotEqual(schema.capabilities["historical_debt"]["status"], "available")

    def test_empty_documented_field_is_missing_not_quality_failure(self):
        self.profile.tables.append(TableProfile(source_id="source", region_id="source:1", source={}, name="payments",
            labels=["payment_amount"], header_rows=[1], grain="payment", roles={"payment_amount": "payment_amount"},
            columns=[ColumnProfile(name="payment_amount", label="payment_amount", position=0,
                role="payment_amount", all_empty=True, usable=False, parser={"kind": "decimal"})]))
        schema = self.schema()
        self.assertEqual(schema.capabilities["payment_history"]["status"], "missing_in_source")

    def test_snapshot_cutoff_is_explicit_and_never_establishes_history(self):
        self.table("stock", "inventory_snapshot", {"product_name": "VARCHAR", "inventory_quantity": "DECIMAL(20,2)"},
            [("A", 10)], labels={"inventory_quantity": "Tồn cuối"})
        schema = self.schema("- Dữ liệu có đến hết 24/06/2026.\n- Tồn kho: Tồn cuối là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản.")
        stock = next(r for r in schema.relations if r["name"] == "clean.stock")
        self.assertEqual(stock["snapshot_as_of"], "2026-06-24")
        self.assertFalse(stock["temporal_semantics"]["historical_reconstruction"])
        self.assertEqual(schema.capabilities["historical_inventory"]["status"], "missing_in_source")

    def test_cutoff_without_snapshot_meaning_does_not_assign_snapshot_date(self):
        self.table("stock", "inventory_snapshot", {"product_name": "VARCHAR", "inventory_quantity": "DECIMAL(20,2)"}, [("A", 10)])
        schema = self.schema("Dữ liệu có đến hết 24/06/2026.")
        stock = next(r for r in schema.relations if r["name"] == "clean.stock")
        self.assertNotIn("snapshot_as_of", stock)

    def test_readme_equations_convert_all_sales_without_leaking_products_into_schema(self):
        self.orders()
        self.items(base=False)
        self.db.execute("UPDATE clean.items SET unit='box'")
        schema = self.schema("- Invented product: 1 box = 6 piece")
        self.assertEqual(schema.capabilities["product_quantities"]["status"], "available")
        self.assertEqual(self.db.execute("SELECT sum(base_quantity) FROM semantic.product_quantities").fetchone()[0], Decimal(18))
        self.assertNotIn("Invented product", schema.model_dump_json())

    def test_missing_conversion_never_silently_drops_sales(self):
        self.orders()
        self.items(base=False)
        self.db.execute("UPDATE clean.items SET unit='box'")
        schema = self.schema("- Different product: 1 box = 6 piece", rules=RULES.replace("Đơn vị không có dòng quy đổi nào là đơn vị cơ bản, hệ số 1.", ""))
        self.assertNotIn("semantic.product_quantities", self.names(schema))
        self.assertTrue(any(i.code == "UNIT_MAPPING_INCOMPLETE" for i in self.profile.issues))

    def test_verified_table_conversions_and_base_units_preserve_multiplicity(self):
        self.orders()
        self.items(base=False)
        self.db.execute("UPDATE clean.items SET unit='box' WHERE order_id='2'")
        self.table("products", "product", {"product_name": "VARCHAR", "base_unit": "VARCHAR"}, [("Invented product", "piece")], ["product_name"])
        self.table("conversions", "unit_conversion", {"product_name": "VARCHAR", "unit": "VARCHAR", "conversion_factor": "DECIMAL(20,2)"}, [("Invented product", "box", 6)], ["product_name", "unit"])
        schema = self.schema()
        self.assertEqual(schema.capabilities["product_quantities"]["status"], "available")
        self.assertEqual(self.db.execute("SELECT count(*),sum(base_quantity) FROM semantic.product_quantities").fetchone(), (2, Decimal(13)))


if __name__ == "__main__":
    unittest.main()
