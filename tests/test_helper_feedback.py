"""Synthetic checks for actionable, bounded worker helper errors."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

# The isolated worker loads these files as top-level modules from /runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tabular_agent"))
import data_tools
from tools import DataTools


class HelperFeedback(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = data_tools.ROOT
        data_tools.ROOT = self.root
        (self.root / "sales.csv").write_text("date,amount\n2026-01-01,10\n", encoding="utf-8")
        book = Workbook()
        book.active.title = "January"
        book.active.append(["date", "amount"])
        book.create_sheet("February")
        book.save(self.root / "sales.xlsx")
        book.close()
        with sqlite3.connect(self.root / "sales.sqlite") as db:
            db.execute("CREATE TABLE sales (amount INTEGER)")
            db.execute("CREATE TABLE returns (amount INTEGER)")
        self.tools = DataTools({"F1": "sales.csv", "F2": "sales.xlsx", "F3": "sales.sqlite"})

    def tearDown(self):
        data_tools.ROOT = self.old_root
        self.temp.cleanup()

    def test_missing_source_sheet_and_table_offer_safe_next_step(self):
        with self.assertRaisesRegex(ValueError, "Available aliases.*F1"):
            self.tools.read_table("bad")
        for operation in (lambda: self.tools.read_table("F2", sheet="bad"),
                          lambda: self.tools.inspect_table("F2", sheet="bad")):
            with self.assertRaisesRegex(ValueError, "January.*February.*inspect_table"):
                operation()
        with self.assertRaisesRegex(ValueError, "sheet=.*January.*inspect_table"):
            self.tools.read_table("F2")
        for operation in (lambda: self.tools.read_table("F3", table="bad"),
                          lambda: self.tools.inspect_table("F3", table="bad")):
            with self.assertRaisesRegex(ValueError, "sales.*returns.*inspect_table"):
                operation()

    def test_missing_column_reports_schema_across_helpers(self):
        frame = pd.DataFrame({"date": ["2026-01-01"], "amount": [10]})
        operations = [lambda: self.tools.profile_columns(frame, ["missing"]),
                      lambda: self.tools.audit_distinct(frame, "missing"),
                      lambda: self.tools.audit_date_filter(frame, "missing", "2026-01-01", "2026-01-02", dayfirst=False),
                      lambda: self.tools.table_profile("F1", date_column="missing", header_row=0),
                      lambda: self.tools.read_table("F1", header_row=0, numeric_columns=["missing"],
                                                    decimal_separator=".", thousands_separator=""),
                      lambda: data_tools.checked_join(frame, frame, on="missing")]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "missing.*date.*amount.*exact name"):
                operation()

    def test_large_schema_feedback_is_bounded_and_marks_omissions(self):
        frame = pd.DataFrame(columns=[f"column_{number}_" + "x" * 300 for number in range(100)])
        with self.assertRaises(ValueError) as caught:
            self.tools.audit_distinct(frame, "missing")
        message = str(caught.exception)
        self.assertIn("88 more omitted", message)
        self.assertIn("long names shortened", message)
        self.assertLess(len(message), 1600)

    def test_csv_delimiter_and_date_errors_are_actionable(self):
        (self.root / "ambiguous.csv").write_text("only one column\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "explicit delimiter"):
            self.tools.inspect_table("ambiguous.csv")
        frame = pd.DataFrame({"date": ["2026-01-01"]})
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.tools.audit_date_filter(frame, "date", "not-a-date", "2026-01-02", dayfirst=False)


if __name__ == "__main__":
    unittest.main()
