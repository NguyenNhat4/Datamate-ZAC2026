"""Rulebook-ready relations, recording rules and the SQL query guard, on invented data only."""

from datetime import date
from decimal import Decimal
import unittest

import duckdb

from tabular_agent.preparation.contracts import BusinessProfile
from tabular_agent.preparation.pipeline import (
    _documented_header, _inherit_group_values, _relation_base, _schema, _sequence_epoch)
from tabular_agent.preparation.semantics import canonical_status, map_status_labels
from tabular_agent.sql_agent import (
    QueryRejected, asks_percent, asks_unsold, check_query, compares_periods, counts_days_over_amount, date_block,
    date_phrases, describe_schema, grounded, normalize_answer, ranks_customers, rescaled_percent, unready_sales_relations)


RULES = """
| Đơn tính vào doanh thu | đơn không huỷ và không trả hàng. Đơn có trả hàng, kể cả chỉ trả một phần, là đơn trả hàng: bỏ cả đơn. Đơn đang giao vẫn tính |
| Nợ một đơn tại ngày X | phần chưa trả của tổng đơn, tính các khoản trả đến hết ngày X |
| Nợ thêm, trả nợ, thu nợ trong kỳ | chỉ đơn ghi nợ, không huỷ. Trả nợ: các khoản trả sau ngày mua có ngày trả trong kỳ |
"""


class StatusMapping(unittest.TestCase):
    def test_exact_vocabulary_covers_four_states(self):
        self.assertEqual(canonical_status("Đã hoàn thành"), "completed")
        self.assertEqual(canonical_status("Đang vận chuyển"), "shipping")
        self.assertEqual(canonical_status("Đã hủy đơn"), "cancelled")
        self.assertEqual(canonical_status("Đã hoàn hàng"), "returned")
        for value in ("không huỷ", "hoàn thành một phần", "chưa rõ", None, ""):
            self.assertEqual(canonical_status(value), "unknown", value)

    def test_single_unfamiliar_label_takes_the_only_missing_state(self):
        mapping, inferred = map_status_labels(["Đã hoàn thành", "Trả hàng", "Đang gửi", "Nhãn lạ"])
        self.assertEqual(mapping["Nhãn lạ"], "cancelled")
        self.assertEqual(inferred, "Nhãn lạ")

    def test_elimination_needs_exactly_one_label_and_one_state(self):
        mapping, inferred = map_status_labels(["Xong", "Nhãn lạ", "Nhãn khác"])
        self.assertIsNone(inferred)
        self.assertEqual(mapping["Nhãn lạ"], "unknown")
        mapping, inferred = map_status_labels(["Xong", "Huỷ", "Trả hàng", "Đang giao", "Nhãn lạ"])
        self.assertIsNone(inferred)
        self.assertEqual(mapping["Nhãn lạ"], "unknown")


class RelationNames(unittest.TestCase):
    def test_period_suffixes_are_dropped(self):
        for path in ("Sales_1.xlsx", "Sales_T1_2025.xlsx", "Sales_Thang01_2025.xlsx"):
            self.assertEqual(_relation_base(None, "Sheet1", path, []), "Sales", path)
        self.assertEqual(_relation_base(None, "Sheet1", "SalesDetail.xlsx", []), "SalesDetail")

    def test_differently_named_sheets_are_separate_relations(self):
        inventory = [{"path": "Export.xlsx", "sheet": "Products"}, {"path": "Export.xlsx", "sheet": "Receipts"}]
        self.assertEqual(_relation_base(None, "Products", "Export.xlsx", inventory), "Products")
        monthly = [{"path": "Book.xlsx", "sheet": "01.2025"}, {"path": "Book.xlsx", "sheet": "02.2025"}]
        self.assertEqual(_relation_base(None, "01.2025", "Book.xlsx", monthly), "Book")

    def test_documented_header_needs_most_labels_in_the_readme(self):
        readme = "- **Mã lạ**: mã hàng\n- **Tên lạ**: tên\n- **Cột ba**: nhóm"
        self.assertTrue(_documented_header(["Mã lạ", "Tên lạ", "Khác"], readme))
        self.assertFalse(_documented_header(["Mã lạ", "A", "B"], readme))


class RecordingRules(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        self.db.execute("CREATE SCHEMA private")

    def raw(self, columns, rows):
        fields = ",".join(f'"{c}" VARCHAR' for c in columns)
        self.db.execute(f'CREATE TABLE private."r_x" (_record_id VARCHAR,_source_id VARCHAR,_row BIGINT,_raw_fingerprint VARCHAR,{fields})')
        for number, row in enumerate(rows, 2):
            self.db.execute('INSERT INTO private."r_x" VALUES (' + ",".join("?" for _ in range(4 + len(columns))) + ")",
                            [f"id{number}", "s", number, "fp" + "|".join(str(v) for v in row), *row])
        return 'private."r_x"'

    def test_continuation_rows_inherit_only_order_level_cells(self):
        raw = self.raw(["order", "day", "status", "product", "note"], [
            (None, None, None, "Before any order", None),
            ("A1", "01/01/2025", "Xong", "P1", "gift"),
            (None, None, None, "P2", None),
            (None, None, None, "P2", None),
            ("A2", "02/01/2025", "Huỷ", "P1", None),
            (None, None, None, "P2", "fragile"),
        ])
        filled = _inherit_group_values(self.db, raw, ["order", "day", "status", "product", "note"], "order")
        self.assertEqual(filled, 3)
        rows = self.db.execute(f'SELECT "order","day","status","product","note" FROM {raw} ORDER BY _row').fetchall()
        self.assertEqual(rows[0], (None, None, None, "Before any order", None))
        self.assertEqual(rows[2], ("A1", "01/01/2025", "Xong", "P2", None))
        self.assertEqual(rows[5], ("A2", "02/01/2025", "Huỷ", "P2", "fragile"))
        prints = [r[0] for r in self.db.execute(f"SELECT _raw_fingerprint FROM {raw} ORDER BY _row").fetchall()]
        self.assertEqual(prints[2], prints[3])
        self.assertNotEqual(prints[2], prints[5])

    def test_no_inheritance_when_every_row_has_an_order(self):
        raw = self.raw(["order", "product"], [("A1", "P1"), ("A2", "P2")])
        self.assertEqual(_inherit_group_values(self.db, raw, ["order", "product"], "order"), 0)

    def test_serial_dates_need_ordered_written_neighbours(self):
        written = [f"{day:02d}/01/2025" for day in range(1, 26)]
        values = []
        for day, text in enumerate(written, 1):
            values.append((text,))
            values.append((str(45658 + day - 1),))
        raw = self.raw(["day"], values)
        epoch, evidence = _sequence_epoch(self.db, raw, "day")
        self.assertEqual(epoch, "1900")
        self.assertEqual(evidence["serial_values_checked"], 25)

    def test_unordered_dates_choose_no_epoch(self):
        values = [("10/01/2025",), ("45658",)] * 13 + [("01/01/2025",), ("45658",)] * 13
        raw = self.raw(["day"], values)
        self.assertEqual(_sequence_epoch(self.db, raw, "day"), (None, None))


class BusinessViews(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        for schema in ("clean", "semantic", "private"):
            self.db.execute("CREATE SCHEMA " + schema)
        self.profile = BusinessProfile(business="invented", fingerprint="fixture")
        self.relations = []

    def table(self, name, grain, fields, rows, keys=()):
        columns = list(fields)
        self.db.execute("CREATE TABLE clean." + name + " (" + ",".join('"' + c + '" ' + fields[c] for c in columns) + ")")
        self.db.executemany("INSERT INTO clean." + name + " VALUES (" + ",".join("?" for _ in fields) + ")", rows)
        self.relations.append({"name": "clean." + name, "grain": grain, "keys": list(keys),
                               "roles": {c: c for c in columns}, "rows": len(rows),
                               "columns": [{"name": c, "role": c, "type": fields[c], "meaning": c, "source_alias": c} for c in columns],
                               "source_regions": [], "blocked_columns": [], "absent_columns": []})

    def names(self, schema):
        return {relation["name"] for relation in schema.relations}

    def credit_shop(self):
        self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                       "order_total": "DECIMAL(20,0)", "payment_method": "VARCHAR", "customer_id": "VARCHAR"},
                   [("1", "2025-01-01", "completed", 100, "debt", "C1"),
                    ("2", "2025-01-01", "cancelled", 500, "debt", "C1"),
                    ("3", "2025-01-02", "completed", 300, "cod", "C2"),
                    ("4", "2025-01-02", "returned", 200, "debt", None),
                    ("5", "2025-01-03", "completed", 50, "cash", "C2")], ["order_id"])
        self.table("items", "order_item", {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_name": "VARCHAR",
                                           "quantity": "DECIMAL(20,0)", "line_amount": "DECIMAL(20,0)"},
                   [("1", "1", "A", 1, 60), ("1", "2", "B", 1, 40), ("2", "1", "A", 5, 500),
                    ("3", "1", "A", 3, 300), ("4", "1", "B", 2, 200), ("5", "1", "A", 1, 50)], ["order_id", "line_number"])
        self.table("payments", "payment", {"payment_id": "VARCHAR", "order_id": "VARCHAR", "payment_date": "TIMESTAMP",
                                           "payment_amount": "DECIMAL(20,0)"},
                   [("P1", "1", "2025-01-01 09:00:00", 30), ("P2", "1", "2025-01-05 10:00:00", 20),
                    ("P3", "4", "2025-01-10 10:00:00", 50), ("P4", "5", "2025-01-03 08:00:00", 50)], ["payment_id"])

    def test_orders_carry_state_and_revenue(self):
        self.credit_shop()
        _schema(self.db, self.profile, self.relations, "", RULES)
        rows = dict((r[0], r[1:]) for r in self.db.execute(
            "SELECT order_id, order_state, counts_as_sale, revenue FROM semantic.orders").fetchall())
        self.assertEqual(rows["1"], ("completed", True, Decimal(100)))
        self.assertEqual(rows["2"], ("cancelled", False, Decimal(500)))
        self.assertEqual(rows["4"], ("returned", False, Decimal(200)))
        total = self.db.execute("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines "
                                "WHERE order_date = DATE '2025-01-01'").fetchone()[0]
        self.assertEqual(total, Decimal(100))

    def test_order_date_is_the_day_of_the_order_timestamp_when_no_date_is_recorded(self):
        self.table("orders", "order", {"order_id": "VARCHAR", "created_at": "TIMESTAMP", "status": "VARCHAR"},
                   [("1", "2025-01-31 23:30:00", "completed"), ("2", "2025-02-01 00:10:00", "cancelled")], ["order_id"])
        self.table("items", "order_item", {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_name": "VARCHAR",
                                           "quantity": "DECIMAL(20,0)", "line_amount": "DECIMAL(20,0)"},
                   [("1", "1", "A", 1, 60), ("2", "1", "A", 1, 40)], ["order_id", "line_number"])
        _schema(self.db, self.profile, self.relations, "", RULES)
        self.assertEqual(self.db.execute("SELECT order_id, order_date FROM semantic.orders ORDER BY 1").fetchall(),
                         [("1", date(2025, 1, 31)), ("2", date(2025, 2, 1))])
        january = self.db.execute("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines "
                                  "WHERE order_date BETWEEN DATE '2025-01-01' AND DATE '2025-01-31'").fetchone()[0]
        self.assertEqual(january, Decimal(60))

    def test_orders_and_lines_carry_shift_customer_and_catalogue_facts(self):
        rules = RULES + "| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59 |"
        self.table("orders", "order", {"order_id": "VARCHAR", "created_at": "TIMESTAMP", "status": "VARCHAR",
                                       "customer_id": "VARCHAR"},
                   [("1", "2025-01-01 13:59:00", "completed", "C1"), ("2", "2025-01-01 14:00:00", "completed", None),
                    ("3", "2025-01-01 23:00:00", "completed", "C1")], ["order_id"])
        self.table("items", "order_item", {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_id": "VARCHAR",
                                           "product_name": "VARCHAR", "quantity": "DECIMAL(20,0)", "line_amount": "DECIMAL(20,0)"},
                   [("1", "1", "P1", "Tra vai", 1, 10), ("2", "1", "P1", "Trà vải", 1, 20), ("3", "1", "P2", "Bánh", 1, 40)],
                   ["order_id", "line_number"])
        self.table("products", "product", {"product_id": "VARCHAR", "product_name": "VARCHAR", "product_group": "VARCHAR",
                                           "base_unit": "VARCHAR"},
                   [("P1", "Trà vải", "Đồ uống", "ly"), ("P2", "Bánh", "Bánh", "cái")], ["product_id"])
        self.table("customers", "customer", {"customer_id": "VARCHAR", "customer_name": "VARCHAR"}, [("C1", "Chị Lan")], ["customer_id"])
        _schema(self.db, self.profile, self.relations, "", rules)
        self.assertEqual(self.db.execute("SELECT order_id, shift, customer_key, customer_name FROM semantic.orders ORDER BY 1").fetchall(),
                         [("1", "morning", "C1", "Chị Lan"), ("2", "afternoon", None, None), ("3", None, "C1", "Chị Lan")])
        rows = self.db.execute("SELECT product_name, product_group, base_unit, SUM(line_amount) FROM semantic.eligible_sales_lines "
                               "GROUP BY ALL ORDER BY 1").fetchall()
        self.assertEqual(rows, [("Bánh", "Bánh", "cái", Decimal(40)), ("Trà vải", "Đồ uống", "ly", Decimal(30))])
        self.assertEqual(self.db.execute("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE shift = 'afternoon'").fetchone()[0], Decimal(20))

    def test_name_spellings_merge_and_walk_in_is_not_a_customer(self):
        self.table("items", "order_item", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                           "customer_name": "VARCHAR", "product_name": "VARCHAR", "line_amount": "DECIMAL(20,0)"},
                   [("1", "2025-01-01", "completed", "Chị Lan", "Trà vải", 10), ("2", "2025-01-01", "completed", "chi lan", "Trà vải", 10),
                    ("3", "2025-01-01", "completed", "Khách lẻ", "Tra vai", 10), ("4", "2025-01-01", "completed", "Chị Lan 2", "Trà đào", 10),
                    ("5", "2025-01-01", "completed", "KL", "Tra dao", 10)])
        _schema(self.db, self.profile, self.relations, "", RULES)
        keys = self.db.execute("SELECT customer_key, COUNT(*) FROM semantic.orders GROUP BY 1 ORDER BY 1 NULLS FIRST").fetchall()
        self.assertEqual(keys, [(None, 2), ("chi lan", 2), ("chi lan 2", 1)])
        names = self.db.execute("SELECT product_name, COUNT(*) FROM semantic.eligible_sales_lines GROUP BY 1 ORDER BY 1").fetchall()
        self.assertEqual(names, [("Trà vải", 3), ("Trà đào", 2)])

    def period_shop(self):
        rules = (RULES + "| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59 |\n"
                 "| Mặt hàng | Dòng Phí phục vụ không phải mặt hàng: không xếp hạng, không đếm |")
        self.table("orders", "order", {"order_id": "VARCHAR", "created_at": "TIMESTAMP", "status": "VARCHAR"},
                   [("1", "2025-01-10 09:00:00", "completed"), ("2", "2025-02-03 08:00:00", "completed"),
                    ("3", "2025-02-03 15:00:00", "completed"), ("4", "2025-02-04 15:30:00", "cancelled"),
                    ("5", "2025-02-05 16:00:00", "completed")], ["order_id"])
        self.table("items", "order_item", {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_id": "VARCHAR",
                                           "product_name": "VARCHAR", "base_quantity": "DECIMAL(20,0)",
                                           "line_amount": "DECIMAL(20,0)"},
                   [("1", "1", "P1", "Trà", 2, 20), ("2", "1", "P1", "Trà", 3, 30), ("2", "2", "P3", "Phí phục vụ", 5, 5),
                    ("3", "1", "P2", "Trà sữa", 3, 60), ("4", "1", "P2", "Trà sữa", 10, 100), ("5", "1", "P4", "Bánh", 1, 5)],
                   ["order_id", "line_number"])
        self.table("products", "product", {"product_id": "VARCHAR", "product_name": "VARCHAR", "product_group": "VARCHAR"},
                   [("P1", "Trà", "Trà"), ("P2", "Trà sữa", "Trà sữa"), ("P3", "Phí phục vụ", None), ("P4", "Bánh", "Bánh")],
                   ["product_id"])
        return _schema(self.db, self.profile, self.relations, "", rules)

    def test_period_functions_answer_in_one_call(self):
        schema = self.period_shop()
        self.assertTrue({"semantic.group_revenue_share", "semantic.period_comparison", "semantic.shift_summary",
                         "semantic.top_products", "semantic.unsold_products"} <= self.names(schema))
        feb = "DATE '2025-02-01', DATE '2025-02-28'"
        # The share's denominator is the whole shop, including the ungrouped service-fee line; similar names stay apart.
        self.assertEqual(self.db.execute(f"SELECT product_group, revenue, shop_revenue, share_percent "
                                         f"FROM semantic.group_revenue_share({feb}) ORDER BY 1 NULLS FIRST").fetchall(),
                         [(None, Decimal(5), Decimal(100), Decimal("5.00")), ("Bánh", Decimal(5), Decimal(100), Decimal("5.00")),
                          ("Trà", Decimal(30), Decimal(100), Decimal("30.00")), ("Trà sữa", Decimal(60), Decimal(100), Decimal("60.00"))])
        row = self.db.execute(f"SELECT * FROM semantic.period_comparison({feb}, DATE '2025-01-01', DATE '2025-01-31')").fetchone()
        self.assertEqual(row, (Decimal(100), Decimal(20), Decimal(80), Decimal("400.00"), 3, 1, 2, Decimal("200.00")))
        # The base period is the asked period's comparison: a fall is negative.
        self.assertEqual(self.db.execute(f"SELECT revenue_change_percent, orders_difference FROM semantic.period_comparison("
                                         f"DATE '2025-01-01', DATE '2025-01-31', {feb})").fetchone(), (Decimal("-80.00"), -2))
        # A period without data is NULL, not 0.
        self.assertEqual(self.db.execute(f"SELECT revenue, orders, revenue_change_percent FROM semantic.period_comparison("
                                         f"DATE '2024-01-01', DATE '2024-01-31', {feb})").fetchone(), (None, None, None))
        # The morning shift is the base; the cancelled afternoon order counts in neither shift.
        self.assertEqual(self.db.execute(f"SELECT * FROM semantic.shift_summary({feb})").fetchall(),
                         [("morning", Decimal(35), 1, Decimal("0.00"), Decimal("0.00")),
                          ("afternoon", Decimal(65), 2, Decimal("85.71"), Decimal("100.00"))])
        # Ranked by base quantity, ties by code; the service-fee line is not a product.
        self.assertEqual(self.db.execute(f"SELECT rank, product_id, product_name, base_quantity "
                                         f"FROM semantic.top_products({feb})").fetchall(),
                         [(1, "P1", "Trà", Decimal(3)), (2, "P2", "Trà sữa", Decimal(3)), (3, "P4", "Bánh", Decimal(1))])
        # Unsold: sold in a counted order before the period, not in it. P2's only sale in the period is cancelled;
        # P4 was first sold in the period; the service-fee line is not a product.
        self.assertEqual(self.db.execute("SELECT product_id, product_name, last_sold FROM semantic.unsold_products("
                                         "DATE '2025-02-04', DATE '2025-02-28')").fetchall(),
                         [("P1", "Trà", date(2025, 2, 3)), ("P2", "Trà sữa", date(2025, 2, 3))])
        self.assertEqual(self.db.execute(f"SELECT count(*) FROM semantic.unsold_products({feb})").fetchone()[0], 0)

    def test_comparison_and_shift_patterns_follow_the_question(self):
        schema = {"relations": self.period_shop().relations}
        self.db.execute("SET search_path='semantic,clean'")
        plain, allowed = describe_schema(self.db, schema, "Cho tôi biết dthu tuần trước.")
        self.assertNotIn("FROM semantic.period_comparison(", plain)
        # The unsold-products function is described only for questions about unsold products.
        self.assertNotIn("semantic.unsold_products", plain)
        self.assertNotIn("semantic.unsold_products", allowed)
        unsold, allowed = describe_schema(self.db, schema, "Tháng 2/2025 có mấy mặt hàng ế?")
        self.assertIn("semantic.unsold_products(first_day DATE, last_day DATE)", unsold)
        self.assertIn("SELECT COUNT(*) FROM semantic.unsold_products(", unsold)
        self.assertIn("semantic.unsold_products", allowed)
        self.assertNotIn("FROM semantic.shift_summary(", plain)
        compared, _ = describe_schema(self.db, schema, "So với tháng trước, doanh thu tháng 2/2025 tăng mấy %?")
        self.assertIn("SELECT revenue_change_percent FROM semantic.period_comparison(", compared)
        self.assertIn("SELECT orders_change_percent FROM semantic.period_comparison(", compared)
        shifts, _ = describe_schema(self.db, schema, "tháng 2/2025 ca chiều có bao nhiêu đơn")
        self.assertIn("SELECT orders FROM semantic.shift_summary(", shifts)
        self.assertNotIn("FROM semantic.period_comparison(", shifts)
        # Without a question every pattern the bundle supports is shown.
        everything, _ = describe_schema(self.db, schema)
        self.assertIn("FROM semantic.period_comparison(", everything)
        self.assertIn("SELECT COUNT(*) FROM semantic.unsold_products(", everything)
        self.assertIn("HAVING SUM(line_amount) >= 49000000", everything)
        # The shop records no customers, so the customer ranking is not offered.
        self.assertNotIn("Customers who bought the most money", everything)

    def test_customers_ranked_by_money_leave_out_walk_in_orders(self):
        self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                       "customer_id": "VARCHAR"},
                   [("1", "2025-01-01", "completed", "C1"), ("2", "2025-01-02", "completed", None),
                    ("3", "2025-01-03", "completed", "C3"), ("4", "2025-01-04", "completed", "C2")], ["order_id"])
        self.table("items", "order_item", {"order_id": "VARCHAR", "line_number": "VARCHAR", "product_name": "VARCHAR",
                                           "line_amount": "DECIMAL(20,0)"},
                   [("1", "1", "A", 100), ("2", "1", "A", 500), ("3", "1", "A", 200), ("4", "1", "A", 200)],
                   ["order_id", "line_number"])
        self.table("customers", "customer", {"customer_id": "VARCHAR", "customer_name": "VARCHAR"},
                   [("C1", "An"), ("C2", "Bình"), ("C3", "Chi")], ["customer_id"])
        schema = _schema(self.db, self.profile, self.relations, "", RULES)
        self.db.execute("SET search_path='semantic,clean'")
        text, _ = describe_schema(self.db, {"relations": schema.relations})
        sql = text[text.index("- Customers who bought the most money"):].split("\n")[1].strip()
        sql = sql.replace("2025-06-01", "2025-01-01").replace("2025-06-30", "2025-01-31")
        # The walk-in order (500) is not a customer; C2 and C3 tie at 200 and are ordered by code.
        self.assertEqual(self.db.execute(sql).fetchall(), [("Bình",), ("Chi",), ("An",)])

    def test_period_functions_need_their_columns(self):
        self.table("items", "order_item", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                           "product_name": "VARCHAR", "line_amount": "DECIMAL(20,0)"},
                   [("1", "2025-01-01", "completed", "A", 10)])
        names = self.names(_schema(self.db, self.profile, self.relations, "", RULES))
        self.assertIn("semantic.period_comparison", names)
        self.assertIn("semantic.unsold_products", names)
        for name in ("semantic.group_revenue_share", "semantic.shift_summary", "semantic.top_products"):
            self.assertNotIn(name, names)

    def test_lines_without_amounts_still_get_unsold_products(self):
        self.table("items", "order_item", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                           "product_name": "VARCHAR", "quantity": "DECIMAL(20,0)"},
                   [("1", "2025-01-01", "completed", "A", 1), ("2", "2025-02-01", "completed", "B", 1)])
        names = self.names(_schema(self.db, self.profile, self.relations, "", RULES))
        self.assertIn("semantic.unsold_products", names)
        for name in ("semantic.period_comparison", "semantic.group_revenue_share", "semantic.top_products"):
            self.assertNotIn(name, names)
        self.assertEqual(self.db.execute("SELECT product_name FROM semantic.unsold_products("
                                         "DATE '2025-02-01', DATE '2025-02-28')").fetchall(), [("A",)])

    def test_stock_without_unit_conversions_still_reports_base_units(self):
        self.table("stock", "inventory_snapshot", {"product_id": "VARCHAR", "product_name": "VARCHAR", "base_unit": "VARCHAR",
                                                   "inventory_quantity": "DECIMAL(20,0)"}, [("S1", "Bottle", "chai", 38)])
        readme = "- Dữ liệu có đến hết 12/06/2026.\n- **Tồn kho**: **Tồn cuối kỳ** là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản."
        _schema(self.db, self.profile, self.relations, readme, RULES)
        self.assertEqual(self.db.execute("SELECT stock_base_units, base_unit, stock_date FROM semantic.stock_snapshot").fetchall(),
                         [(Decimal(38), "chai", date(2026, 6, 12))])

    def test_debt_follows_the_rulebook_definition(self):
        self.credit_shop()
        schema = _schema(self.db, self.profile, self.relations, "", RULES)
        self.assertLessEqual({"semantic.debt_as_of", "semantic.debt_repayments", "semantic.debt_added"}, self.names(schema))
        owing = dict(self.db.execute("SELECT order_id, outstanding FROM semantic.debt_as_of(DATE '2025-01-04')").fetchall())
        # Cancelled and cash-on-delivery orders never owe; a returned order still does.
        self.assertEqual(owing, {"1": Decimal(70), "4": Decimal(200)})
        later = dict(self.db.execute("SELECT order_id, outstanding FROM semantic.debt_as_of(DATE '2025-01-31')").fetchall())
        self.assertEqual(later, {"1": Decimal(50), "4": Decimal(150)})
        # A payment on the purchase day is not a repayment.
        repaid = self.db.execute("SELECT SUM(amount) FROM semantic.debt_repayments "
                                 "WHERE repayment_date BETWEEN DATE '2025-01-01' AND DATE '2025-01-31'").fetchone()[0]
        self.assertEqual(repaid, Decimal(70))
        added = dict(self.db.execute("SELECT order_id, added_debt FROM semantic.debt_added").fetchall())
        self.assertEqual(added, {"1": Decimal(70), "4": Decimal(200)})

    def test_debt_is_not_published_without_a_payment_method(self):
        self.table("orders", "order", {"order_id": "VARCHAR", "order_date": "DATE", "status": "VARCHAR",
                                       "order_total": "DECIMAL(20,0)"}, [("1", "2025-01-01", "completed", 100)], ["order_id"])
        self.table("payments", "payment", {"payment_id": "VARCHAR", "order_id": "VARCHAR", "payment_date": "DATE",
                                           "payment_amount": "DECIMAL(20,0)"}, [("P1", "1", "2025-01-01", 30)], ["payment_id"])
        schema = _schema(self.db, self.profile, self.relations, "", RULES)
        self.assertNotIn("semantic.debt_as_of", self.names(schema))

    def test_book_without_orders_filters_rows_by_their_own_state(self):
        self.table("book", "sale_recording", {"status": "VARCHAR", "line_amount": "DECIMAL(20,0)", "order_date": "DATE",
                                              "product_name": "VARCHAR"},
                   [("Đã hoàn thành", 10, "2025-01-01", "A"), ("Trả hàng", 20, "2025-01-01", "A"),
                    ("Đang gửi", 40, "2025-01-01", "B"), ("Nhãn lạ", 80, "2025-01-01", "B")])
        schema = _schema(self.db, self.profile, self.relations, "", RULES)
        self.assertIn("semantic.eligible_sales_lines", self.names(schema))
        self.assertNotIn("semantic.orders", self.names(schema))
        self.assertEqual(self.db.execute("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines").fetchone()[0], Decimal(50))

    def test_stock_is_split_into_large_units_for_one_date(self):
        self.table("stock", "inventory_snapshot", {"product_id": "VARCHAR", "product_name": "VARCHAR", "unit": "VARCHAR",
                                                   "inventory_quantity": "DECIMAL(20,0)"},
                   [("S1", "Boxed", "piece", 80), ("S2", "Loose", "piece", 7)])
        self.table("units", "unit_conversion", {"product_id": "VARCHAR", "unit": "VARCHAR", "conversion_factor": "DECIMAL(20,0)"},
                   [("S1", "box", 6)], ["product_id", "unit"])
        readme = "- Dữ liệu có đến hết 14/09/2026.\n- **Tồn kho**: **Tồn cuối kỳ** là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản."
        schema = _schema(self.db, self.profile, self.relations, readme, RULES)
        self.assertIn("semantic.stock_snapshot", self.names(schema))
        row = self.db.execute("SELECT whole_large_units, loose_base_units, stock_date FROM semantic.stock_snapshot "
                              "WHERE product_id = 'S1'").fetchone()
        self.assertEqual(row, (13, 2, date(2026, 9, 14)))
        undated = _schema_names(self, "- Dữ liệu có đến hết 14/09/2026.")
        self.assertNotIn("semantic.stock_snapshot", undated)


def _schema_names(case, readme):
    """Publish the same relations into a fresh database with a different README."""
    db = duckdb.connect()
    case.addCleanup(db.close)
    for schema in ("clean", "semantic", "private"):
        db.execute("CREATE SCHEMA " + schema)
    for relation in case.relations:
        name = relation["name"].split(".")[1]
        fields = ",".join('"' + c["name"] + '" ' + c["type"] for c in relation["columns"])
        db.execute(f"CREATE TABLE clean.{name} ({fields})")
    schema = _schema(db, BusinessProfile(business="invented", fingerprint="fixture"), case.relations, readme, RULES)
    return {relation["name"] for relation in schema.relations}


class QueryGuard(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        self.allowed = {"clean.sales", "semantic.orders", "semantic.debt_as_of"}

    def test_listed_relations_and_functions_pass(self):
        check_query(self.db, "WITH a AS (SELECT * FROM clean.sales) SELECT * FROM a JOIN orders USING (id)", self.allowed)
        check_query(self.db, "SELECT SUM(outstanding) FROM semantic.debt_as_of(DATE '2025-01-01');", self.allowed)

    def test_everything_else_is_rejected(self):
        for sql in ("SELECT * FROM private.lineage", "SELECT * FROM read_csv('/etc/passwd')",
                    "SELECT * FROM duckdb_tables()", "SELECT * FROM information_schema.tables",
                    "DROP TABLE clean.sales", "SELECT 1; SELECT 2", "SELECT * FROM other", "",
                    "SELECT * FROM clean.sales WHERE d = CURRENT_DATE", "SELECT * FROM clean.sales WHERE n LIKE '%a%'"):
            with self.assertRaises(QueryRejected, msg=sql):
                check_query(self.db, sql, self.allowed)


def _relation(name, grain, *roles):
    return {"name": name, "grain": grain,
            "columns": [{"name": f"c{index}", "type": "VARCHAR", "role": role} for index, role in enumerate(roles)]}


class UnreadySales(unittest.TestCase):
    """Sales tables the rulebook could not be applied to are named, refused and explained."""

    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        for schema in ("clean", "semantic"):
            self.db.execute("CREATE SCHEMA " + schema)
        for name in ("sales_book", "debt_terms", "deliveries", "stock", "purchases", "loose_sales", "warranty"):
            self.db.execute(f"CREATE TABLE clean.{name} (c0 VARCHAR, c1 VARCHAR, c2 VARCHAR, c3 VARCHAR)")
        self.schema = {"relations": [
            _relation("clean.sales_book", "order_item", "order_id", "line_amount", "order_date"),
            _relation("clean.debt_terms", "order", "order_id"),
            _relation("clean.deliveries", "order_item", "order_id", "product_name", "base_quantity", "base_unit"),
            _relation("clean.stock", "inventory_snapshot", "product_name", "quantity"),
            _relation("clean.purchases", "unknown", "line_amount", "receipt_date", "product_name"),
            _relation("clean.loose_sales", "unknown", "customer_name", "line_amount", "quantity"),
            _relation("clean.warranty", "unknown", "customer_name", "status"),
        ]}

    def test_sales_tables_are_named_only_without_rulebook_ready_lines(self):
        self.assertEqual(unready_sales_relations(self.schema), {"clean.sales_book", "clean.loose_sales"})
        ready = {"relations": self.schema["relations"] + [
            _relation("semantic.eligible_sales_lines", "order_item", "order_id", "line_amount")]}
        self.assertEqual(unready_sales_relations(ready), set())

    def test_schema_lists_them_by_name_with_the_instruction_to_abstain(self):
        text, allowed = describe_schema(self.db, self.schema)
        self.assertEqual(allowed, {"clean.debt_terms", "clean.deliveries", "clean.stock", "clean.purchases",
                                   "clean.warranty"})
        self.assertIn("## Sales tables that cannot be used", text)
        self.assertIn("clean.loose_sales, clean.sales_book", text)
        self.assertIn('"type": "abstain"', text)
        self.assertIn("not the selling price on a past date", text)
        self.assertNotIn("clean.sales_book  (one row", text)
        # Tables without sale facts stay usable for what they record, with a note.
        deliveries = text[text.index("clean.deliveries  (one row"):]
        self.assertIn("never for revenue, order counts or quantities sold", deliveries.split("\n\n")[0])

    def test_queries_on_them_are_refused_with_the_reason(self):
        _, allowed = describe_schema(self.db, self.schema)
        refused = unready_sales_relations(self.schema)
        for sql in ("SELECT SUM(c1) FROM clean.sales_book",
                    "SELECT COUNT(*) FROM sales_book",
                    "WITH s AS (SELECT * FROM clean.stock) SELECT * FROM s JOIN loose_sales USING (c0)"):
            with self.assertRaises(QueryRejected, msg=sql) as caught:
                check_query(self.db, sql, allowed, refused)
            self.assertIn("rulebook-ready", str(caught.exception))
            self.assertIn("abstain", str(caught.exception))
        check_query(self.db, "SELECT SUM(c1) FROM clean.stock", allowed, refused)
        check_query(self.db, "SELECT COUNT(DISTINCT c0) FROM clean.deliveries", allowed, refused)
        check_query(self.db, "WITH sales_book AS (SELECT * FROM clean.purchases) SELECT * FROM sales_book",
                    allowed, refused)

    def test_rulebook_ready_orders_answer_order_questions_and_revenue(self):
        self.db.execute("CREATE TABLE semantic.eligible_orders (order_id VARCHAR, order_date DATE, "
                        "order_goods_amount DECIMAL(20,0), order_total DECIMAL(20,0))")
        orders = _relation("semantic.eligible_orders", "order", "order_id", "order_date", "order_goods_amount",
                           "order_total")
        for column, kind in zip(orders["columns"], ("VARCHAR", "DATE", "DECIMAL(20,0)", "DECIMAL(20,0)")):
            column["name"], column["type"] = column["role"], kind
        schema = {"relations": [orders] + self.schema["relations"]}
        text, allowed = describe_schema(self.db, schema)
        self.assertIn("semantic.eligible_orders", allowed)
        self.assertIn("Number of orders", text)
        patterns = text[text.index("## Query patterns"):]
        self.assertTrue(patterns.split("\n")[1].startswith("- Revenue (doanh thu) in a period"))
        self.assertIn("SELECT SUM(order_goods_amount) FROM semantic.eligible_orders", patterns)
        self.assertNotIn("not separated", text)
        with self.assertRaises(QueryRejected) as caught:
            check_query(self.db, "SELECT SUM(c1) FROM clean.sales_book", allowed, unready_sales_relations(schema), True)
        self.assertIn("semantic.eligible_orders", str(caught.exception))


class AnswerFormat(unittest.TestCase):
    def test_numeric_text_becomes_numbers_but_codes_stay_text(self):
        self.assertEqual(normalize_answer({"type": "list", "answer": ["13", "2"]})["answer"], [13, 2])
        self.assertEqual(normalize_answer({"type": "list", "answer": ["0042", "SP01"]})["answer"], ["0042", "SP01"])
        self.assertEqual(normalize_answer({"type": "number", "answer": 70712.0})["answer"], 70712)
        self.assertEqual(normalize_answer({"type": "percent", "answer": 12.345})["answer"], 12.35)
        self.assertEqual(normalize_answer({"type": "abstain", "reason": "x"}), {"type": "abstain", "reason": "x"})

    def test_date_phrases_in_a_question_are_resolved_without_accents(self):
        self.assertEqual(date_phrases("ee cho toi biet dthu bua qua", "2025-10-25"),
                         ['- "bua qua" means yesterday: the single day 2025-10-24'])
        lines = date_phrases("Doanh thu tuần rồi so với 7 ngày gần nhất?", "2025-07-25")
        self.assertIn('- "tuan roi" means last week: 2025-07-14 to 2025-07-20', lines)
        self.assertIn('- "7 ngay gan nhat" means the last 7 days: 2025-07-19 to 2025-07-25', lines)
        self.assertEqual(date_phrases("Doanh thu là bao nhiêu?", "2025-07-25"), [])

    def test_named_months_and_quarters_get_explicit_ranges(self):
        # A question that compares nothing gets only its own period.
        self.assertEqual(date_phrases("Doanh thu tháng 2/2024?", "2025-07-25"),
                         ['- "thang 2 2024" means 2024-02-01 to 2024-02-29'])
        self.assertEqual(date_phrases("bao nhieu don trong quy 3 nam 2025", "2026-09-14"),
                         ['- "quy 3 nam 2025" means 2025-07-01 to 2025-09-30'])
        self.assertEqual(date_phrases("dthu quý 3 năm 2025 tăng hay giảm bn % so với quý trước", "2026-09-14"),
                         ['- "quy 3 nam 2025" means 2025-07-01 to 2025-09-30; the quarter before it is 2025-04-01 to 2025-06-30'])
        self.assertIn("the month before it is 2025-07-01 to 2025-07-31",
                      date_phrases("thang 8/2025 dthu thay doi bao nhiu % so voi thang lien trc", "2026-08-19")[0])
        # A question that names both periods brings its own base: no earlier range is added.
        self.assertEqual(date_phrases("Doanh thu tháng 4 năm 2026 tăng trưởng bao nhiêu % so với tháng 4 năm 2025?", "2026-08-19"),
                         ['- "thang 4 nam 2026" means 2026-04-01 to 2026-04-30', '- "thang 4 nam 2025" means 2025-04-01 to 2025-04-30'])
        self.assertIn("the same month a year earlier is 2025-05-01 to 2025-05-31",
                      date_phrases("doanh thu tháng 5/2026 so với cùng tháng năm trước tăng mấy %", "2026-08-19")[0])
        # Comparing the shifts of one month is not a comparison with an earlier month.
        self.assertEqual(date_phrases("tháng 12/2025 doanh thu ca chiều so với ca sáng chênh bao nhiêu %", "2026-06-02"),
                         ['- "thang 12 2025" means 2025-12-01 to 2025-12-31'])
        # Without a year: this year's unless it starts after the reference date.
        self.assertIn("2025-02-01 to 2025-02-28", date_phrases("doanh thu tháng 2", "2026-01-15")[0])
        self.assertIn("2026-01-01 to 2026-01-31", date_phrases("doanh thu tháng 1", "2026-01-15")[0])
        # Beside a named month, "the month before" belongs to that month, not to today.
        self.assertEqual(date_phrases("So cùng kỳ năm trước, doanh thu tháng 5/2026 tăng mấy %?", "2026-06-02"),
                         ['- "thang 5 2026" means 2026-05-01 to 2026-05-31; '
                          'the same month a year earlier is 2025-05-01 to 2025-05-31'])
        self.assertIn("2025-11-01 to 2025-11-30", date_phrases("dthu thangs 11/2025 hơn hay kém tháng trc", "2026-06-02")[0])
        lines = date_phrases("so với tháng trước, doanh thu tháng 12/2025 tăng mấy %", "2026-06-02")
        self.assertEqual(len(lines), 1)
        self.assertIn("the month before it is 2025-11-01 to 2025-11-30", lines[0])

    def test_percent_questions_and_grounded_numbers(self):
        self.assertTrue(asks_percent("Tỉ lệ đơn huỷ tháng 1 là bao nhiêu?"))
        self.assertTrue(asks_percent("doanh thu tang hay giam may % so voi thang truoc"))
        self.assertFalse(asks_percent("tồn kho thuốc 10% chai 90ml còn bao nhiêu chai"))
        self.assertTrue(asks_percent("nhóm gỏi đóng góp bao nhieeu % dthu"))
        self.assertTrue(asks_percent("dthu hơn hay kém tháng trc bnhiu phần trawm"))
        self.assertTrue(asks_percent("Trà vải chiếm bao nhiêu phaanf trăm doanh thu?"))
        self.assertTrue(asks_percent("dthu tăng hay giảm maays %"))
        self.assertFalse(asks_percent("Povidine 10 % chai 90ml còn bao nhiêu chai"))
        self.assertTrue(grounded(0.79, [0.79]))
        self.assertTrue(grounded(70712, [Decimal("70711.6")]))
        self.assertTrue(grounded(2.89, [2.8912]))
        self.assertFalse(grounded(79, [0.79]))
        self.assertFalse(grounded(0, []))

    def test_question_kinds_for_rankings_and_counts(self):
        for text in ("Tháng 6/2025 khách nào mua nhiều tiền nhất?", "top 3 khach chi tieu nhieu nhat tuan roi",
                     "ai mua hang nhiu tien nhat thang 3/2025"):
            self.assertTrue(ranks_customers(text), text)
        for text in ("5 khách nào đang nợ nhiều nhất?", "doanh thu tháng 6/2025 là bao nhiêu"):
            self.assertFalse(ranks_customers(text), text)
        for text in ("Tháng 5 có mấy ngày doanh thu đạt 20.000.000 đồng trở lên?", "so ngay co dthu ≥ 10.000.000 la bn"):
            self.assertTrue(counts_days_over_amount(text), text)
        # "đắt" (expensive) folds like "đạt" (reach): the best day is not a threshold count.
        for text in ("tháng 4 ngày nào bán đắt nhất", "doanh thu ngày 3/4/2025"):
            self.assertFalse(counts_days_over_amount(text), text)
        for text in ("Tuần rồi có mấy mặt hàng ế?", "co bao nhieu mat hang ko ban duoc cai nao trong 7 ngay"):
            self.assertTrue(asks_unsold(text), text)
        # "ê" opening a question folds like "ế"; out of stock (hết hàng) is a different question.
        for text in ("e doanh thu cua hang hom qua bao nhieu", "Cuối ngày có mấy mặt hàng hết hàng?",
                     "ê nhóm hàng trà chiếm bao nhiêu %"):
            self.assertFalse(asks_unsold(text), text)

    def test_comparisons_with_an_earlier_period(self):
        for question in ("So với tháng trước đó, doanh thu tháng 11/2025 tăng hay giảm mấy phần trăm?",
                         "thang 8/2025 dthu thay doi bao nhiu % so voi thang lien trc",
                         "dthu tuần này so với tuần trước tăng bn %", "doanh thu tháng 5/2026 so cùng kỳ năm ngoái"):
            self.assertTrue(compares_periods(question), question)
        for question in ("Cho tôi biết dthu tuần trước.", "Số ly Cà phê muối bán được trong tháng 6/2025",
                         "ca chiều so với ca sáng chênh bao nhiêu %", "tổng tiền giảm giá tháng 3/2026",
                         "tỉ trọng của nhóm giảm đau hạ sốt trong dthu tháng 10/2025 là bao nhiêu %"):
            self.assertFalse(compares_periods(question), question)

    def test_a_percent_cell_multiplied_by_100_again_is_rescaled(self):
        self.assertEqual(rescaled_percent(-35, [Decimal("-0.35")]), -0.35)
        self.assertEqual(rescaled_percent(74, [Decimal("0.7412")]), 0.74)
        self.assertIsNone(rescaled_percent(12, [Decimal("0.5")]))
        self.assertIsNone(rescaled_percent(0, [Decimal("0")]))

    def test_several_numbers_are_a_list_and_a_table_needs_a_named_object(self):
        misplaced = {"type": "table", "columns": ["whole", "loose"], "rows": [[13, 2]]}
        self.assertEqual(normalize_answer(misplaced), {"type": "list", "answer": [13, 2]})
        named = {"type": "table", "columns": ["name", "debt"], "rows": [["A", "100"]]}
        self.assertEqual(normalize_answer(named), {"type": "table", "answer": {"columns": ["name", "debt"], "rows": [["A", 100]]}})
        several = {"type": "table", "answer": {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}}
        self.assertEqual(normalize_answer(several)["type"], "table")

    def test_relative_periods_follow_the_reference_date(self):
        text = date_block("2025-07-25")
        self.assertIn("yesterday (hôm qua, hqua, bữa qua): 2025-07-24", text)
        self.assertIn("this week (tuần này): 2025-07-21 to 2025-07-25", text)
        self.assertIn("last week (tuần trước, tuần rồi): 2025-07-14 to 2025-07-20", text)
        self.assertIn("last month, same number of days (so với tháng trước): 2025-06-01 to 2025-06-25", text)
        self.assertIn("same number of days (so với tháng trước): 2025-02-01 to 2025-02-28", date_block("2025-03-31"))


if __name__ == "__main__":
    unittest.main()
