"""Synthetic normalization checks independent of business reference answers."""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from tabular_agent.preparation.parsers import (
    decimal_type,
    infer_parser,
    normalize_name,
    parse_quantity,
    parse_value,
    representation,
    text_key,
)


class ParserTests(unittest.TestCase):
    def test_names_preserve_meaning_and_are_sql_friendly(self):
        self.assertEqual(normalize_name("  Số lượng (quy đổi) "), "so_luong_quy_doi")
        self.assertEqual(normalize_name("Đơn Giá"), "don_gia")
        self.assertEqual(normalize_name("TongTien"), "tong_tien")
        self.assertEqual(normalize_name("2025 revenue"), "column_2025_revenue")
        self.assertEqual(normalize_name("---"), "column")
        self.assertEqual(text_key("  ĐẶNG   Văn  "), "dang van")

    def test_exact_money_abbreviations_and_mixed_column(self):
        values = ["55.000đ", "25k", "1tr090", "1,45tr", "1.234.567", "1.234,56", "1,234.56", "-2,5k", "(55.000đ)"]
        parser = infer_parser("Thành tiền", values)
        self.assertEqual(parser["kind"], "decimal")
        self.assertEqual(parser["status"], "proposed")
        self.assertEqual([parse_value(v, parser) for v in values], [Decimal("55000"), Decimal("25000"), Decimal("1090000"), Decimal("1450000"), Decimal("1234567"), Decimal("1234.56"), Decimal("1234.56"), Decimal("-2500"), Decimal("-55000")])

    def test_arithmetic_does_not_round_during_ingestion(self):
        with localcontext() as context:
            context.prec = 6
            value = "123456789012345678901234567890,123456789k"
            self.assertEqual(parse_value(value, {"kind": "decimal"}), Decimal("123456789012345678901234567890123.456789"))

    def test_ambiguous_bare_separator_requires_evidence(self):
        for raw in ["1,234", "1.234"]:
            with self.subTest(raw=raw):
                parser = infer_parser("Amount", [raw])
                self.assertEqual(parser["status"], "unresolved")
                with self.assertRaises(ValueError):
                    parse_value(raw, {"kind": "decimal"})
        grouped = infer_parser("Amount", ["1,234", "12,345,678"])
        self.assertEqual(parse_value("1,234", grouped), Decimal("1234"))
        fractional = infer_parser("Amount", ["1,234", "1,25"])
        self.assertEqual(parse_value("1,234", fractional), Decimal("1.234"))

    def test_explicit_formats_and_currency_metadata(self):
        self.assertEqual(parse_value("1.234", {"kind": "decimal", "decimal_separator": "."}), Decimal("1.234"))
        self.assertEqual(parse_value("1.234", {"kind": "decimal", "thousands_separator": "."}), Decimal("1234"))
        vnd = infer_parser("Amount", ["55.000", "1.200"], description="Unit: VND")
        self.assertEqual(parse_value("55.000", vnd), Decimal("55000"))
        self.assertEqual(parse_value("1 234 567,50", {"kind": "decimal"}), Decimal("1234567.50"))
        self.assertEqual(parse_value("1e-8", {"kind": "decimal"}), Decimal("0.00000001"))
        self.assertEqual(parse_value("0.005", {"kind": "decimal", "currency": "VND"}), Decimal("0.005"))
        self.assertEqual(parse_value("0,005", {"kind": "decimal", "currency": "VND"}), Decimal("0.005"))

    def test_invalid_formats_fail_instead_of_becoming_null(self):
        for raw in ["not recorded", "12.34.567", "1,2,3", "1.234,5,6", "1 tr090x", "NaN", "inf", "12 34", True, float("inf"), Decimal("NaN")]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_value(raw, {"kind": "decimal"})
        self.assertEqual(parse_value("0", {"kind": "decimal"}), Decimal(0))
        self.assertIsNone(parse_value(None, {"kind": "decimal"}))
        self.assertIsNone(parse_value("  ", {"kind": "decimal"}))
        self.assertEqual(parse_value("", {"kind": "text"}), "")
        self.assertEqual(parse_value("  original  ", {"kind": "text"}), "  original  ")

    def test_quantity_splits_numeric_prefix_without_packaging_conversion(self):
        self.assertEqual(parse_quantity("1 bao 25kg"), (Decimal(1), "bao 25kg"))
        self.assertEqual(parse_quantity("-2,5 kg"), (Decimal("-2.5"), "kg"))
        self.assertEqual(parse_quantity(3), (Decimal(3), None))
        parser = infer_parser("Số lượng", ["1 bao 25kg", "2,5 kg", 3])
        self.assertEqual(parser["kind"], "quantity")
        self.assertEqual(parse_value("1 bao 25kg", parser), Decimal(1))
        with self.assertRaises(ValueError):
            parse_quantity("bao 25kg")
        with self.assertRaises(ValueError):
            parse_quantity("1/2")

    def test_mixed_dates_with_explicit_serial_epoch(self):
        parser = infer_parser("Ngày đơn", ["01/06/2025", "1 thg 6 2025", "2025-06-01", 45809], epoch="1900")
        self.assertEqual(parser["status"], "proposed")
        self.assertEqual([parse_value(v, parser) for v in ["01/06/2025", "1 thg 6 2025", "2025-06-01", 45809]], [date(2025, 6, 1)] * 4)
        with self.assertRaises(ValueError):
            parse_value("31/02/2025", parser)

    def test_yearless_date_requires_supported_enclosing_year(self):
        parser = infer_parser("Ngày", ["31/12"])
        self.assertEqual(parser["status"], "unresolved")
        parser = infer_parser("Ngày", ["31/12", "1 thg 6"], year=2025)
        self.assertEqual(parse_value("31/12", parser), date(2025, 12, 31))
        self.assertEqual(parse_value("1 thg 6", parser), date(2025, 6, 1))

    def test_serial_epoch_must_be_corroborated_and_handles_both_date_systems(self):
        parser = infer_parser("Ngày", [45809])
        self.assertEqual(parser["status"], "unresolved")
        with self.assertRaisesRegex(ValueError, "epoch"):
            parse_value(45809, {"kind": "date"})
        self.assertEqual(parse_value(1, {"kind": "date", "epoch": "1900"}), date(1900, 1, 1))
        self.assertEqual(parse_value(61, {"kind": "date", "epoch": "1900"}), date(1900, 3, 1))
        self.assertEqual(parse_value(0, {"kind": "date", "epoch": "1904"}), date(1904, 1, 1))
        self.assertEqual(parse_value(1, {"kind": "date", "epoch": "1904"}), date(1904, 1, 2))
        with self.assertRaises(ValueError):
            parse_value(60, {"kind": "date", "epoch": "1900"})

    def test_timestamps_preserve_time_and_timezone(self):
        parser = infer_parser("created_at", ["2025-06-01T13:02:01Z"])
        self.assertEqual(parser["kind"], "datetime")
        self.assertEqual(parse_value("2025-06-01T13:02:01Z", parser), datetime(2025, 6, 1, 13, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(parse_value("01/06/2025 13:02:01.123", parser), datetime(2025, 6, 1, 13, 2, 1, 123000))
        self.assertEqual(parse_value(Decimal("1.5"), {"kind": "datetime", "epoch": "1904"}), datetime(1904, 1, 2, 12))
        self.assertEqual(parse_value("1.5", {"kind": "datetime", "epoch": "1904"}), datetime(1904, 1, 2, 12))

    def test_identifiers_preserve_zeros_and_original_spelling(self):
        for label in ["Mã sản phẩm", "order_id", "SĐT", "Barcode", "Số hóa đơn", "OrderNumber"]:
            with self.subTest(label=label):
                parser = infer_parser(label, ["001", "002"])
                self.assertEqual(parser["kind"], "identifier")
                self.assertEqual(parse_value("001", parser), "001")
        self.assertEqual(parse_value("Đỗ Văn Á", infer_parser("Tên khách", ["Đỗ Văn Á"])), "Đỗ Văn Á")
        self.assertEqual(infer_parser("CreatedAt", ["2025-06-01T13:02:01"])["kind"], "datetime")
        self.assertEqual(infer_parser("unknown", ["001", "002"])["kind"], "identifier")

    def test_spaces_around_a_code_are_not_part_of_it(self):
        parser = infer_parser("Mã sản phẩm", ["SP0017", " SP0017 "])
        self.assertEqual(parse_value(" SP0017 ", parser), "SP0017")
        self.assertEqual(parse_value("007", parser), "007")

    def test_a_count_of_days_is_a_number_not_a_date(self):
        for label in ("Số ngày được nợ", "Hạn nợ (ngày)"):
            with self.subTest(label=label):
                parser = infer_parser(label, ["30", "45", "60"])
                self.assertEqual(parser["kind"], "decimal")
                self.assertEqual(parse_value("45", parser), Decimal(45))
        self.assertEqual(infer_parser("Ngày bán", ["01/02/2025"])["kind"], "date")

    def test_boolean_inference_requires_boolean_semantics(self):
        parser = infer_parser("IsActive", ["Có", "Không"])
        self.assertEqual(parser["kind"], "boolean")
        self.assertTrue(parse_value("Có", parser))
        self.assertFalse(parse_value("Không", parser))
        self.assertEqual(infer_parser("Count", ["0", "1"])["kind"], "decimal")
        with self.assertRaises(ValueError):
            parse_value("maybe", parser)

    def test_all_empty_is_not_claimed_to_be_known(self):
        parser = infer_parser("Giới tính", [None, "", " "])
        self.assertEqual(parser["evidence"]["representations"], {"missing": 1, "blank": 2})
        self.assertEqual(parser["evidence"]["sample_count"], 3)
        self.assertEqual(parser["kind"], "text")

    def test_descriptive_fields_do_not_become_money_from_one_word(self):
        self.assertEqual(infer_parser("CachHoanTien", ["Tiền mặt", "Chuyển khoản"])["kind"], "text")
        self.assertEqual(infer_parser("GhiChuNo", ["Đã thanh toán"])["kind"], "text")
        self.assertEqual(infer_parser("LoaiGia", ["1", "2"])["kind"], "text")
        self.assertEqual(infer_parser("UnitPrice", ["100", "200"])["kind"], "decimal")

    def test_representation_contains_no_record_values(self):
        self.assertEqual(representation("1tr090"), "abbreviated_number")
        self.assertEqual(representation("55.000đ"), "currency")
        self.assertEqual(representation("1 thg 6 2025"), "vietnamese_date")
        parser = infer_parser("Ngày", ["bad value"])
        self.assertNotIn("bad value", str(parser))

    def test_decimal_type_is_exact_and_does_not_impose_database_limit(self):
        self.assertEqual(decimal_type([Decimal("1000"), Decimal("0.0001")]), (8, 4))
        self.assertEqual(decimal_type([Decimal("0.001")]), (3, 3))
        self.assertEqual(decimal_type([Decimal("1E+10")]), (11, 0))
        self.assertEqual(decimal_type([Decimal("1" * 50)]), (50, 0))
        self.assertEqual(decimal_type([]), (1, 0))


if __name__ == "__main__":
    unittest.main()
