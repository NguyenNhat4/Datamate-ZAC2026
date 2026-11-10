"""Semantic proposals use invented labels/documentation, never answers."""

import unittest

from tabular_agent.preparation.semantics import (
    classify_status,
    column_description,
    duplicate_policy,
    propose_table,
    return_evidence_policy,
    readme_unit_conversions,
    sales_eligibility_policy,
)


RULES = """
| Đơn tính vào doanh thu | đơn **không huỷ** và **không trả hàng**. Đơn có trả hàng, **kể cả chỉ trả một phần**, là đơn trả hàng: bỏ **cả đơn**. Đơn đang giao vẫn tính |
Riêng trạng thái huỷ, trả hàng của đơn lấy theo trạng thái ghi trên đơn, kể cả khi phiếu trả ghi ngày sau hôm nay.
| Hai dòng của **cùng một đơn** giống nhau ở mọi ô | lỗi xuất file, tính một lần. Hai dòng giống nhau của hai đơn khác nhau là hai lần bán, tính cả hai |
| Bảng không ghi theo đơn | mỗi dòng là một lần ghi; hai dòng giống nhau trong cùng file vẫn tính cả hai |
"""


class Semantics(unittest.TestCase):
    def propose(self, labels, source="sales.csv", readme="", rules=RULES):
        return propose_table(labels, source_name=source, readme=readme, rulebook=rules)

    def test_order_headers_and_lines_keep_distinct_grains(self):
        header = self.propose(["MaHoaDon", "NgayLap", "ThoiGianLap", "TinhTrang", "TongTienHang", "TongThanhToan", "KhachDaTra"], "pos.sqlite::don_hang")
        self.assertEqual(header["grain"], "order")
        self.assertEqual(header["key_roles"], ["order_id"])
        self.assertEqual(header["roles"], {"order_id": 0, "order_date": 1, "created_at": 2, "status": 3, "order_goods_amount": 4, "order_total": 5, "paid_amount": 6})
        detail = self.propose(["MaHoaDon", "SoThuTu", "MaHangHoa", "SL", "SLQuyDoi", "TriGia", "GiaDonVi", "GiaVonLucBan"], "pos.sqlite::chi_tiet_don")
        self.assertEqual(detail["grain"], "order_item")
        self.assertEqual(detail["key_roles"], ["order_id", "line_number"])
        self.assertEqual(detail["roles"]["line_amount"], 5)
        self.assertEqual(detail["roles"]["unit_cost"], 7)

    def test_scoped_readme_overrides_ambiguous_aliases(self):
        readme = """
## Cột trong `sales.csv`
- **Mã GD**: mã đơn
- **Nội dung**: tên hàng
- **Tổng tiền**: thành tiền sau giảm
- **Ngày**: ngày bán
- **SL**: số lượng
## Cột trong `payments.csv`
- **Mã GD**: mã phiếu thu
- **Nội dung**: ghi chú
- **Giá trị**: số tiền
- **Ngày thu**: thời điểm thu
- **Số CT**: mã đơn
"""
        sales = self.propose(["Mã GD", "Nội dung", "Tổng tiền", "Ngày", "SL"], "sales.csv", readme)
        self.assertEqual(sales["grain"], "order_item")
        self.assertEqual(sales["roles"], {"order_id": 0, "product_name": 1, "line_amount": 2, "order_date": 3, "quantity": 4})
        payments = self.propose(["Mã GD", "Nội dung", "Giá trị", "Ngày thu", "Số CT"], "payments.csv", readme)
        self.assertEqual(payments["grain"], "payment")
        self.assertEqual(payments["roles"], {"payment_id": 0, "payment_amount": 2, "payment_date": 3, "order_id": 4})
        self.assertTrue(any(e["kind"] == "readme" for e in payments["evidence"]))

    def test_partition_suffix_preserves_scoped_definitions(self):
        readme = "## Cột trong `Sales_1.xlsx`\n- **Nội dung**: tên hàng\n- **Mã GD**: mã đơn\n- **SL**: số lượng\n"
        result = self.propose(["Nội dung", "Mã GD", "SL"], "Sales_2026.xlsx::2026-01", readme)
        self.assertEqual(result["grain"], "order_item")

    def test_a_readme_pattern_for_monthly_files_scopes_every_month(self):
        readme = ("## Cột trong `BanHang_T<tháng>_<năm>.xlsx`, sheet `DonHang_ChiTiet`\n"
                  "- **Tổng tiền**: thành tiền sau giảm\n- **Mã đơn hàng**: mã đơn\n")
        for month in ("BanHang_T1_2025.xlsx", "BanHang_T12_2026.xlsx", "BanHang_Thang03_2025.xlsx"):
            proposal = propose_table(["Mã đơn hàng", "Tổng tiền"], source_name=month + "::DonHang_ChiTiet",
                                     readme=readme, rulebook=RULES)
            self.assertEqual(proposal["roles"], {"order_id": 0, "line_amount": 1}, month)

    def test_orderless_records_never_gain_artificial_order_identity(self):
        result = self.propose(["Tên hàng", "Ngày bán", "Số lượng", "Thành tiền"])
        self.assertEqual(result["grain"], "sale_recording")
        self.assertEqual(result["duplicate_policy"], "preserve")
        self.assertEqual(result["key_roles"], [])
        self.assertNotIn("order_id", result["roles"])

    def test_deduplication_needs_rulebook_evidence(self):
        labels = ["Mã hóa đơn", "Tên hàng", "Số lượng", "Thành tiền"]
        self.assertEqual(self.propose(labels, rules="")["duplicate_policy"], "preserve")
        self.assertEqual(self.propose(labels)["duplicate_policy"], "same_order_exact")
        self.assertFalse(duplicate_policy("Duplicate rows are possible.")["same_order_exact"])
        self.assertTrue(duplicate_policy(RULES)["same_order_exact"])

    def test_lot_allocations_are_not_sales_lines(self):
        result = self.propose(["MaHoaDon", "SoThuTu", "MaLo", "SLQuyDoi", "SLTraQuyDoi"], "pos.sqlite::chi_tiet_don_lo")
        self.assertEqual(result["grain"], "lot_allocation")
        self.assertEqual(result["key_roles"], ["order_id", "line_number", "lot_id"])
        self.assertEqual(result["duplicate_policy"], "preserve")
        self.assertEqual(result["roles"]["return_quantity"], 4)

    def test_keys_are_not_guessed_from_products_on_order_items(self):
        result = self.propose(["Mã hóa đơn", "Mã hàng", "Số lượng"])
        self.assertEqual(result["grain"], "order_item")
        self.assertEqual(result["key_roles"], [])

    def test_qualified_sqlite_descriptions_do_not_leak(self):
        readme = "**Dòng hàng**: `chi_tiet_don.TriGia` là thành tiền của dòng (đã trừ giảm giá dòng); `chi_tiet_don.SLQuyDoi` là số lượng đã đổi ra đơn vị cơ bản.\n**Công nợ**: `thanh_toan.SoTienThu` là số tiền trả lần đó; `don_hang.TongThanhToan` là số khách phải trả cho cả đơn."
        result = self.propose(["MaHoaDon", "SoThuTu", "MaHangHoa", "TriGia", "SLQuyDoi"], "pos.sqlite::chi_tiet_don", readme)
        self.assertEqual(result["roles"]["line_amount"], 3)
        self.assertEqual(result["roles"]["base_quantity"], 4)
        result = self.propose(["MaPhieuThu", "MaHoaDon", "SoTienThu", "NgayGioThu"], "pos.sqlite::thanh_toan", readme)
        self.assertEqual(result["grain"], "payment")
        self.assertEqual(result["roles"]["payment_amount"], 2)

    def test_declared_empty_fields_do_not_acquire_roles(self):
        readme = "## Cột trong `products.csv`\n- **Tên sản phẩm**: tên\n- **Mã hàng**: mã hàng\n- **Thời gian tạo**: ngày tạo mặt hàng trên phần mềm\n- **Trạng thái**: cột trống, không có dữ liệu\n"
        result = self.propose(["Tên sản phẩm", "Mã hàng", "Thời gian tạo", "Trạng thái"], "products.csv", readme)
        self.assertEqual(result["grain"], "product")
        self.assertNotIn("status", result["roles"])
        self.assertNotIn("created_at", result["roles"])
        self.assertNotIn("order_date", result["roles"])

    def test_duplicate_role_columns_are_ambiguous(self):
        result = self.propose(["Order ID", "MaHoaDon", "Tên hàng", "Số lượng"])
        self.assertNotIn("order_id", result["roles"])
        self.assertTrue(any(e["kind"] == "ambiguous_role" for e in result["evidence"]))
        self.assertEqual(result["duplicate_policy"], "preserve")

    def test_inventory_snapshots_do_not_invent_history_date(self):
        result = self.propose(["Mã hàng", "Tên hàng", "Tồn cuối kỳ"], "BaoCaoTonKho.xlsx")
        self.assertEqual(result["grain"], "inventory_snapshot")
        self.assertNotIn("inventory_date", result["roles"])
        movement = self.propose(["MaHangHoa", "NgayGioPhatSinh", "SLQuyDoi", "TonCuoi"], "pos.sqlite::the_kho")
        self.assertEqual(movement["grain"], "inventory_movement")

    def test_price_history_and_conversion_are_not_sales(self):
        price = self.propose(["MaHangHoa", "NgayApDung", "GiaBanLe"], "pos.sqlite::lich_su_gia")
        self.assertEqual(price["grain"], "price_history")
        self.assertEqual(price["roles"]["new_price"], 2)
        conversion = self.propose(["MaHangHoa", "DonViTinh", "HeSoQuyDoi"], "pos.sqlite::don_vi_sp")
        self.assertEqual(conversion["grain"], "unit_conversion")
        self.assertEqual(conversion["key_roles"], ["product_id", "unit"])

    def test_an_empty_header_is_not_a_fabricated_entity(self):
        result = self.propose(["", "", ""], "san_pham.csv")
        self.assertEqual(result["grain"], "unknown")
        self.assertEqual(result["roles"], {})

    def test_lot_reference_to_receipt_does_not_change_grain(self):
        result = self.propose(["MaHangHoa", "MaLo", "MaPhieuNhap", "HanSuDung", "SLNhapQuyDoi"], "pos.sqlite::lo_hang")
        self.assertEqual(result["grain"], "lot")
        self.assertEqual(result["key_roles"], ["lot_id"])

    def test_mixed_source_labels_use_context_and_not_record_values(self):
        result = self.propose(["Giá vốn lúc bán", "SL cơ bản", "Giá niêm yết", "Thanh tien", "Mã giao dịch", "Chi tiết hàng", "Slg", "TT dòng", "Ma hang", "Đ.vị"], "BanHang_ChiTietHoaDon.xlsx")
        self.assertEqual(result["grain"], "order_item")
        self.assertEqual(result["roles"]["order_id"], 4)
        self.assertEqual(result["roles"]["base_quantity"], 1)
        self.assertEqual(result["roles"]["quantity"], 6)
        conversion = self.propose(["Đơn vị", "Bằng", "Mã sp"], "BanHang_DonViQuyDoi.xlsx")
        self.assertEqual(conversion["grain"], "unit_conversion")

    def test_purchase_prose_does_not_erase_sales_unit_price(self):
        readme = "- **Nhập hàng**: **Đơn giá** là giá nhập một đơn vị, theo đơn vị nhập của dòng."
        result = self.propose(["Mã hóa đơn", "Mã hàng", "Số lượng", "Đơn giá"], "DanhSachHoaDonChiTiet.xlsx", readme)
        self.assertEqual(result["roles"]["unit_price"], 3)

    def test_conflicting_partition_definitions_require_resolution(self):
        readme = "## Cột trong `Sales_1.xlsx`\n- **Mã GD**: mã đơn\n## Cột trong `Sales_2.xlsx`\n- **Mã GD**: mã phiếu thu\n"
        result = self.propose(["Mã GD", "Tên hàng"], "Sales_2026.xlsx", readme)
        self.assertNotIn("order_id", result["roles"])
        self.assertNotIn("payment_id", result["roles"])
        self.assertTrue(any(e["kind"] == "ambiguous_definition" for e in result["evidence"]))

    def test_filename_display_recovery_keeps_dictionary_scoped(self):
        filename = "Danh sách hàng hóa.xlsx"
        damaged = filename.encode("utf-8").decode("cp437")
        readme = f"## Cột trong `{filename}`\n- **Tên hàng hóa**: tên\n- **Mã hàng**: mã hàng\n- **Đơn vị tính**: đơn vị cơ bản\n"
        result = self.propose(["Tên hàng hóa", "Mã hàng", "Đơn vị tính"], damaged, readme)
        self.assertEqual(result["grain"], "product")
        self.assertEqual(result["roles"]["base_unit"], 2)

    def test_receipts_do_not_acquire_sales_price_or_order_date(self):
        result = self.propose(["Mã nhập hàng", "Mã hàng", "STT", "Số lượng", "Đơn giá"], "DanhSachChiTietNhapHang.xlsx")
        self.assertEqual(result["grain"], "receipt_item")
        self.assertEqual(result["key_roles"], ["receipt_id", "line_number"])
        self.assertNotIn("unit_price", result["roles"])


class RulePolicies(unittest.TestCase):
    def test_column_meanings_are_scoped_to_sources(self):
        readme = "## Cột trong `sales.csv`\n- **Nội dung**: tên hàng\n## Cột trong `payments.csv`\n- **Nội dung**: ghi chú\n"
        self.assertEqual(column_description("Nội dung", source_name="payments.csv", readme=readme), "ghi chú")
        self.assertEqual(column_description("Nội dung", source_name="unknown.csv", readme=readme), "")

    def test_sales_default_requires_whole_order_evidence(self):
        policy = sales_eligibility_policy(RULES)
        self.assertEqual(policy["status"], "resolved")
        self.assertTrue(policy["whole_order_returns"])
        self.assertTrue(policy["include_shipping"])
        self.assertTrue(policy["later_return_evidence_applies"])
        self.assertIn("**cả đơn**", policy["excerpt"])
        missing = sales_eligibility_policy("Revenue is the sum of sales.")
        self.assertEqual(missing["status"], "unresolved")
        self.assertIsNone(missing["exclude_cancelled"])

    def test_negative_quantities_need_explicit_return_meaning(self):
        self.assertFalse(return_evidence_policy("Quantities can be negative.")["negative_quantity_is_return"])
        policy = return_evidence_policy("- **Trả hàng**: hàng trả ghi số lượng âm, cùng mã đơn với đơn đã bán: đơn có dòng số lượng âm là đơn có trả hàng.")
        self.assertTrue(policy["negative_quantity_is_return"])
        policy = return_evidence_policy("- **Số lượng trả**: số lượng khách trả lại (đơn vị cơ bản); lớn hơn 0: đơn đó có trả hàng")
        self.assertTrue(policy["positive_return_quantity_is_return"])

    def test_order_time_described_as_the_hour_of_the_order(self):
        from tabular_agent.preparation.semantics import _role_from_description
        self.assertEqual(_role_from_description("giờ đặt đơn (dòng trả hàng: lúc trả)", ""), "created_at")

    def test_status_vocabulary_is_conservative(self):
        for value in (None, "", " ", "ready", "Đã thanh toán", "hoàn thành một phần", "không huỷ", 1):
            self.assertEqual(classify_status(value), "unknown", value)
        for value in ("Huỷ", "hủy", "CANCELLED", "Đã huỷ"):
            self.assertEqual(classify_status(value), "cancelled", value)
        for value in ("trả hàng", "Returned", "Hoàn trả", "Đổi trả"):
            self.assertEqual(classify_status(value), "returned", value)
        for value in ("hoàn thành", "ĐANG GIAO", "completed", "shipping", "Đã giao"):
            self.assertEqual(classify_status(value), "active", value)


class ReadmeConversions(unittest.TestCase):
    def test_explicit_product_equations_only(self):
        readme = """
- Nước thử: 1 lốc = 6 chai
- Nước thử: 1 thùng = 24 chai
- Gạo thử: 1 bao 25kg = 25 kg
- Chất thử: 1 chai = 0,5 lít
Product names can contain packaging: Product 12 x 4 bottles.
"""
        result = readme_unit_conversions(readme)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(len(result["conversions"]), 4)
        self.assertEqual(result["conversions"][2]["from_unit"], "bao 25kg")
        self.assertEqual(result["conversions"][3]["factor"], "0.5")

    def test_conflicting_and_nonpositive_equations_are_not_published(self):
        readme = "- Nước thử: 1 thùng = 24 chai\n- Nước thử: 1 thùng = 30 chai\n- Gạo thử: 1 bao = 0 kg\n"
        result = readme_unit_conversions(readme)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["conversions"], [])
        self.assertEqual({issue["code"] for issue in result["issues"]}, {"CONFLICTING_UNIT_CONVERSION", "INVALID_CONVERSION_FACTOR"})

    def test_equal_equations_combine_evidence_without_merging_accents(self):
        readme = "- Bò thử: 1 thùng = 6 gói\n- bò thử: 1 thùng = 6 gói\n- Bo thử: 1 thùng = 12 gói\n"
        conversions = readme_unit_conversions(readme)["conversions"]
        self.assertEqual(len(conversions), 2)
        self.assertEqual(len(conversions[0]["evidence"]), 2)

    def test_product_packaging_name_is_not_a_conversion(self):
        result = readme_unit_conversions("- Product: Chai 24 x 250ml\n- Shelf unit: thùng\n")
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["conversions"], [])


if __name__ == "__main__":
    unittest.main()
