"""Synthetic layout and immutable-source checks; no reference answers."""
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile

from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

from tabular_agent.preparation.readers import SourceReadError, discover_sources, iter_tables


class Readers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.business = Path(self.temp.name)

    def tables(self):
        result = []
        for source in discover_sources(self.business):
            for table in iter_tables(self.business, source):
                result.append((table, list(table.rows)))
        return result

    def workbook(self, rows, name="book.xlsx"):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        path = self.business / name
        workbook.save(path)
        workbook.close()
        return path

    def test_headers_below_titles_and_row_accounting(self):
        for header in (2, 3, 4):
            with self.subTest(header=header):
                path = self.workbook([["Monthly report"]] + [[]] * (header - 2) + [["Order ID", "Product", "Quantity"], ["001", "Coffee", 2], ["Total", None, 2], ["Notes: end"]])
                [(table, rows)] = self.tables()
                self.assertEqual(table.header_rows, [header])
                self.assertEqual([row.number for row in rows], list(range(1, header + 4)))
                self.assertEqual([row.classification for row in rows[-3:]], ["record", "summary", "note"])
                self.assertEqual(rows[-3].values[0], "001")
                path.unlink()

    def test_report_filter_does_not_override_real_header(self):
        self.workbook([["Trạng thái đơn hàng", "Đặt hàng (Đã duyệt)"], [], ["Mã đơn hàng", "Ngày đặt hàng", "Tên sản phẩm", "Số lượng"], ["001", "01/01/2025", "Tea", 2]])
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [3])
        self.assertEqual(rows[0].classification, "title")

    def test_first_narrow_table_is_not_swallowed_by_later_wider_table(self):
        self.workbook([["Product", "Quantity"], ["Coffee", 2], [], ["Order ID", "Product", "Amount"], ["001", "Tea", 100]])
        tables = self.tables()
        self.assertEqual([t.header_rows for t, _ in tables], [[1], [4]])
        self.assertEqual([r.number for _, rows in tables for r in rows], [1, 2, 3, 4, 5])

    def test_csv_title_rows_and_snake_case_headers(self):
        (self.business / "data.csv").write_text('Monthly report\n\norder_id;product_name;quantity\n001;Coffee;2\n', encoding="utf-8")
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [3])
        self.assertEqual(rows[-1].values, ["001", "Coffee", "2"])

    def test_debt_terms_header_after_report_title(self):
        self.workbook([["CHI TIẾT CÔNG NỢ THEO HẠN THANH TOÁN"], [], ["Mã đơn hàng", "Số ngày được nợ", "Hạn thanh toán"], ["001", 30, "01/01/2025"]])
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [3])
        self.assertEqual(table.metadata["layout_issues"], [])
        self.assertEqual(rows[-1].classification, "record")

    def test_product_containing_total_is_not_summary(self):
        self.workbook([["Order ID", "Product", "Quantity"], ["001", "Tổng ngày", 2], ["002", "Tổng hợp vitamin", 3], ["Tổng ngày", None, 5]])
        [(_, rows)] = self.tables()
        self.assertEqual([r.classification for r in rows[1:]], ["record", "record", "summary"])

    def test_summary_labels_in_nonfirst_columns_and_missing_id_records(self):
        self.workbook([["Order ID", "Date", "Customer", "Product", "Quantity", "Amount"], ["001", "01/01/2025", "A", "Tổng ngày", 2, 100], [None, None, "Tổng ngày", None, None, 100], [None, None, None, "Cộng ngày", 2, 100], [None, "02/01/2025", "A", "Tổng ngày", 3, 150], [None, None, "A", "Tea", 4, 200]])
        [(_, rows)] = self.tables()
        self.assertEqual([r.classification for r in rows[1:]], ["record", "summary", "summary", "record", "record"])

    def test_monthly_and_yearly_totals_are_separate_from_records(self):
        self.workbook([["Order ID", "Product", "Amount"], ["001", "Tổng hợp vitamin", 100], [None, "Tổng tháng 03", 100], [None, "Cộng năm 2025", 100]])
        [(_, rows)] = self.tables()
        self.assertEqual([r.classification for r in rows[1:]], ["record", "summary", "summary"])

    def test_repeated_header_and_incomplete_record(self):
        self.workbook([["Order ID", "Product", "Quantity"], ["001", "Coffee", 2], ["Order ID", "Product", "Quantity"], ["002", "Tea"], ["003", "Tea", 3, "extra"], ["004"]])
        [(_, rows)] = self.tables()
        self.assertEqual(rows[2].reason, "repeated_header")
        self.assertEqual(rows[3].values, ["002", "Tea", None])
        self.assertEqual(rows[4].reason, "extra_populated_columns")
        self.assertEqual(rows[5].reason, "sparse_record_or_note")

    def test_multiple_regions_do_not_lose_lookahead_rows(self):
        self.workbook([["Order ID", "Quantity"], ["001", 2], [], ["Product", "Price"], ["Tea", 100], ["Coffee", 200]])
        tables = self.tables()
        self.assertEqual(len(tables), 2)
        self.assertEqual([r.number for _, rows in tables for r in rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual(tables[1][0].labels, ["Product", "Price"])
        self.assertEqual(tables[1][1][-1].values, ["Coffee", 200])

    def test_merged_grouped_header(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["Product", "Sales", None])
        worksheet.append([None, "Quantity", "Amount"])
        worksheet.append(["Coffee", 2, 100])
        worksheet.merge_cells("A1:A2")
        worksheet.merge_cells("B1:C1")
        workbook.save(self.business / "grouped.xlsx")
        workbook.close()
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [1, 2])
        self.assertEqual(table.labels, ["Product", "Sales / Quantity", "Sales / Amount"])
        self.assertEqual(rows[2].classification, "record")

    def test_unmerged_grouped_header_propagates_only_header_context(self):
        self.workbook([["Product", "Sales", None, "Price", None], [None, "Quantity", "Amount", "Old price", "New price"], ["Coffee", 2, 100, None, 50]])
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [1, 2])
        self.assertEqual(table.labels[2], "Sales / Amount")
        self.assertEqual(table.labels[4], "Price / New price")
        self.assertIsNone(rows[-1].values[3])

    def test_grouped_id_and_date_protect_real_summary_named_product(self):
        self.workbook([["Order", None, "Sales", None, None], ["ID", "Date", "Product", "Amount", "Quantity"], ["001", "01/01/2025", "Tổng ngày", 100, None]])
        [(table, rows)] = self.tables()
        self.assertEqual(table.header_rows, [1, 2])
        self.assertEqual(rows[-1].classification, "record")

    def test_missing_and_wrong_dimensions_are_not_limits(self):
        path = self.workbook([["Order ID", "Quantity"], ["001", 2], ["002", 4]])
        with zipfile.ZipFile(path) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents["xl/worksheets/sheet1.xml"] = contents["xl/worksheets/sheet1.xml"].replace(b'<dimension ref="A1:B3"/>', b'<dimension ref="A1:A1"/>')
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents_ in contents.items():
                archive.writestr(name, contents_)
        [(_, rows)] = self.tables()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1].values, ["002", 4])

    def test_csv_quoting_bom_and_physical_coordinates(self):
        (self.business / "data.csv").write_text('\ufeffOrder ID;Product;Quantity\n001;"Coffee; large";2\n002;"two\nlines";3\n003;Tea;4\n', encoding="utf-8")
        [(table, rows)] = self.tables()
        self.assertEqual(table.metadata["delimiter"], ";")
        self.assertEqual(rows[1].values, ["001", "Coffee; large", "2"])
        self.assertEqual(rows[2].values[1], "two\nlines")
        self.assertEqual([r.number for r in rows], [1, 2, 3, 5])

    def test_sqlite_quoted_tables_and_immutability(self):
        path = self.business / "db.sqlite"
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE "order items" ("Order ID" TEXT, "Quantity" INTEGER)')
        connection.execute('INSERT INTO "order items" VALUES (?, ?)', ("001", 2))
        connection.execute('CREATE TABLE empty (id TEXT)')
        connection.commit()
        connection.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        tables = self.tables()
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[1][0].header_rows, [])
        self.assertEqual(tables[1][1][0].values, ["001", 2])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_filename_alias_and_epoch_do_not_change_physical_name(self):
        name = "Bán.xlsx".encode("utf-8").decode("cp437")
        workbook = Workbook()
        workbook.epoch = CALENDAR_MAC_1904
        workbook.save(self.business / name)
        workbook.close()
        [source] = discover_sources(self.business)
        self.assertEqual(source.path, name)
        self.assertEqual(source.display_alias, "Bán.xlsx")
        self.assertEqual(source.epoch, "1904")
        self.assertTrue((self.business / name).exists())

    def test_uncached_formula_is_not_ordinary_missing(self):
        self.workbook([["Order ID", "Amount"], ["001", "=1+2"]])
        [(_, rows)] = self.tables()
        self.assertEqual(rows[1].classification, "unresolved")
        self.assertEqual(rows[1].reason, "formula_without_cached_value")

    def test_entire_uncached_formula_row_is_not_a_blank_separator(self):
        self.workbook([["Order ID", "Amount"], [None, "=1+2"]])
        [(table, rows)] = self.tables()
        self.assertEqual(rows[1].classification, "unresolved")
        self.assertEqual(table.metadata["formula_cells"]["B2"]["formula"], "1+2")

    def test_symlink_and_path_traversal_rejected(self):
        (self.business / "real.csv").write_text("id,amount\na,1\n")
        [source] = discover_sources(self.business)
        (self.business / "link.csv").symlink_to(self.business / "real.csv")
        with self.assertRaises(SourceReadError):
            discover_sources(self.business)
        from dataclasses import replace
        with self.assertRaises(SourceReadError):
            list(iter_tables(self.business, replace(source, path="../outside.csv")))

    def test_exact_distinct_headers_disambiguated(self):
        self.workbook([["ID", "Amount", "Amount", None], ["001", 2, 3, 4]])
        # An unnamed internal column with populated records cannot be discarded.
        [(table, rows)] = self.tables()
        self.assertEqual(rows[1].reason, "extra_populated_columns")
        self.assertEqual(table.labels, ["ID", "Amount", "Amount__2"])


if __name__ == "__main__":
    unittest.main()
