"""Rulebook-ready relations, recording rules and the SQL query guard, on invented data only."""

from datetime import date
from decimal import Decimal
import unittest

import duckdb

from tabular_agent.preparation.contracts import BusinessProfile
from tabular_agent.preparation.pipeline import (
    _documented_header, _inherit_group_values, _relation_base, _schema, _sequence_epoch)
from tabular_agent.preparation.semantics import canonical_status, map_status_labels
from tabular_agent.sql_agent import QueryRejected, check_query, date_block, date_phrases, normalize_answer


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
                    "DROP TABLE clean.sales", "SELECT 1; SELECT 2", "SELECT * FROM other", ""):
            with self.assertRaises(QueryRejected, msg=sql):
                check_query(self.db, sql, self.allowed)


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
        self.assertEqual(date_phrases("Doanh thu tháng 3/2025?", "2025-07-25"), [])

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
