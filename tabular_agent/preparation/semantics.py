"""Evidence-backed *proposals* for business roles, never data-level guarantees.

Only labels and documentation enter this module. Keys, table grain, and joins
still need full-record validation before publication. In particular a familiar
file name does not establish that its identifiers are unique. Ambiguous labels
are intentionally omitted so a validated specification can supply an override.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any, Iterable


def _words(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = unicodedata.normalize("NFD", value.replace("đ", "d").replace("Đ", "D"))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _token(value: str) -> str:
    return _words(value).replace(" ", "")


def _source_tokens(value: str) -> set[str]:
    # Callers may provide a path, a table name, or path::table/sheet.
    result: set[str] = set()
    for part in re.split(r"::|:|#", value.replace("\\", "/")):
        name = PurePosixPath(part).name
        try:
            name = name.encode("cp437").decode("utf-8")
        except UnicodeError:
            pass
        name = re.sub(r"\.(?:xlsx|csv|sqlite|sqlite3|db)$", "", name, flags=re.I)
        name = re.sub(r"_(?:\d{1,2}|20\d{2})$", "", name)
        if name:
            result.add(_token(name))
    return result


@lru_cache(maxsize=512)
def _scoped_descriptions(readme: str, source_name: str) -> dict[str, tuple[str, str]]:
    """Read explicit field dictionaries, scoped by physical source/table name."""
    names = _source_tokens(source_name)
    active = False
    result: dict[str, tuple[str, str]] = {}
    for line in readme.splitlines():
        if re.match(r"^#{1,6}\s", line):
            heading = re.sub(r"^#{1,6}\s*", "", line)
            references = re.findall(r"`([^`]+)`", heading)
            # "file, sheet X" names one sheet: both must match, or another sheet's fields would leak in.
            matches = [bool(names & _source_tokens(ref)) for ref in references]
            scoped = all(matches) if " sheet " in f" {_words(heading)} " and len(references) > 1 else any(matches)
            active = bool(re.search(r"cot|columns?|fields?", _words(heading))) and bool(matches) and scoped
        if active:
            match = re.match(r"\s*[-*]\s+\*\*(.+?)\*\*\s*:\s*(.+)", line)
            if match:
                key = _token(match[1])
                previous = result.get(key)
                if previous and _words(previous[0]) != _words(match[2]):
                    result[key] = ("", previous[1] + "\n" + line)
                else:
                    result[key] = (match[2], line)
    return result


@lru_cache(maxsize=4096)
def _inline_description(
    readme: str, source_name: str, label: str
) -> tuple[str, str] | None:
    names, wanted = _source_tokens(source_name), _token(label)
    for line in readme.splitlines():
        # Qualified fields in prose cannot leak into another table.
        for match in re.finditer(r"`([^`]+)\.([^`.]+)`\s+(?:là|la|is)\s+([^;\n]+)", line):
            if names & _source_tokens(match[1]) and _token(match[2]) == wanted:
                return match[3], line
        # Only explicit "column is meaning" phrases, not a whole rule heading.
        for match in re.finditer(r"\*\*([^*]+)\*\*\s+(?:là|la|is)\s+([^;\n]+)", line):
            if _token(match[1]) == wanted:
                if "gia nhap" in _words(match[2]) and _source_context(source_name) not in {"receipt", "receipt_item", "lot"}:
                    continue
                return match[2], line
    return None


def column_description(label: str, *, source_name: str, readme: str) -> str:
    """A field's scoped meaning; callers must not search every README section.

    Empty strings mean no unique documented meaning was established, not that
    the field is empty. ``propose_table`` records conflicting definitions.
    """
    definition = _scoped_descriptions(readme, source_name).get(_token(label))
    if definition is None and " / " in label:
        definition = _scoped_descriptions(readme, source_name).get(_token(label.rsplit(" / ", 1)[1]))
    if definition is None:
        definition = _inline_description(readme, source_name, label)
    if definition is None and " / " in label:
        definition = _inline_description(readme, source_name, label.rsplit(" / ", 1)[1])
    return definition[0] if definition else ""


def readme_unit_conversions(readme: str) -> dict[str, Any]:
    """Parse only explicit product-specific equations, never packaging names.

    Returned product names/units are records for preparation, and must not be
    copied into the answering schema. Resolve product identities against clean
    tables before publishing conversions. Conflicting equations are excluded.
    """
    equations: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: set[tuple[str, str]] = set()
    issues: list[dict[str, Any]] = []

    def comparison(value: str) -> str:
        # Preserve Vietnamese accents: similar display strings need not name
        # the same product. A catalogue-backed mapping may resolve aliases later.
        return " ".join(unicodedata.normalize("NFC", value).casefold().split())

    pattern = re.compile(r"^\s*[-*]\s+(.+?):\s*1\s+(.+?)\s*=\s*([+-]?\d+(?:[.,]\d+)?)\s+(.+?)\s*$")
    for line in readme.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        product, selling_unit, number, base_unit = (value.strip().strip("*") for value in match.groups())
        key = (comparison(product), comparison(selling_unit))
        try:
            factor = Decimal(number.replace(",", "."))
        except InvalidOperation:
            factor = Decimal(0)
        if factor <= 0:
            issues.append({"code": "INVALID_CONVERSION_FACTOR", "evidence": line})
            conflicts.add(key)
            continue
        proposed = {"product_name": product, "from_unit": selling_unit, "base_unit": base_unit,
                    "factor": str(factor), "evidence": [line]}
        previous = equations.get(key)
        if previous:
            if comparison(previous["base_unit"]) != comparison(base_unit) or Decimal(previous["factor"]) != factor:
                conflicts.add(key)
                issues.append({"code": "CONFLICTING_UNIT_CONVERSION", "evidence": previous["evidence"] + [line]})
            else:
                previous["evidence"].append(line)
        else:
            equations[key] = proposed
    conversions = [value for key, value in equations.items() if key not in conflicts]
    return {"status": "partial" if issues else "resolved" if conversions else "absent",
            "conversions": conversions, "issues": issues}


# These are label aliases, not arbitrary substring matches. Broad labels such
# as Tiền, Tổng tiền, Nội dung, Mã GD and Ngày require scoped documentation.
_ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "orderid", "invoice_id", "MaHoaDon", "mã hóa đơn", "mã đơn", "mã đơn hàng", "mã bill", "so hoa don", "số hđ"),
    "line_number": ("line_number", "line_no", "SoThuTu", "STT", "TT dòng", "thứ tự", "No."),
    "product_id": ("product_id", "sku", "MaHangHoa", "ma_sp", "mã hàng", "mã sản phẩm", "mã sp", "mã món", "mã sản phẩm bán", "mã HH", "mã mặt hàng", "mã hàng bán", "mã SKU", "ID SP"),
    "product_name": ("product_name", "product", "TenHang", "tên hàng", "tên sản phẩm", "tên SP", "tên hàng hóa", "tên mặt hàng", "tên món", "hàng bán", "mặt hàng", "chi tiết hàng", "món"),
    "customer_id": ("customer_id", "MaKhachHang", "mã khách hàng", "mã KH", "ma_kh", "mã KH mua", "mã khách mua", "mã khách lẻ/sỉ", "số KH"),
    "customer_name": ("customer_name", "TenKH", "tên khách", "tên khách hàng", "khách hàng (tên)", "khách", "bên mua", "đối tác"),
    "order_date": ("order_date", "NgayLap", "NgayBan", "ngày bán", "ngày đặt hàng", "ngày đặt", "ngày mua", "ngày giao dịch", "ngày tháng"),
    "created_at": ("created_at", "ThoiGianLap", "thời gian tạo đơn", "thời gian tạo", "giờ tạo đơn"),
    "status": ("status", "TinhTrang", "trang_thai", "trạng thái", "trạng thái đơn", "trạng thái đơn hàng", "trạng thái xử lý", "tình trạng đơn", "kết quả", "xử lý", "ghi chú TT"),
    "quantity": ("quantity", "qty", "SoLuong", "Sluong", "Slg", "SL", "S.L", "số lượng", "số lượng bán", "số lượng hàng", "số lượng sản phẩm", "SL bán", "SL nhập"),
    "base_quantity": ("base_quantity", "SLQuyDoi", "số lượng quy đổi", "SL quy đổi", "SL quy ra lẻ", "SL cơ bản", "số lượng lẻ", "SL lẻ", "quy ra ĐV lẻ", "SLNhapQuyDoi", "SL nhập (ĐVT cơ bản)"),
    "unit": ("unit", "DonViTinh", "DonVi", "đơn vị tính", "đơn vị (tính)", "ĐVT", "DVT", "ĐV tính", "Đ.vị", "ĐVT nhập"),
    "line_amount": ("line_amount", "TriGia", "trị giá", "ThanhTien", "thành tiền", "thành tiền (đ)", "T.Tiền", "Amount", "tiền bán"),
    "unit_price": ("unit_price", "GiaDonVi", "đơn giá", "đơn giá bán", "don_gia", "đơn giá (đ)", "giá sản phẩm", "giá bán lẻ", "giá 1 đv", "giá đơn vị", "giá lẻ", "Price"),
    "unit_cost": ("unit_cost", "GiaVonLucBan", "GiaVonHang", "giá vốn", "giá vốn lúc bán", "giá vốn hiện tại"),
    "order_goods_amount": ("order_goods_amount", "TongTienHang", "tổng tiền hàng", "Tien hang", "tiền hàng trước giảm"),
    "order_total": ("order_total", "TongThanhToan", "TongTien", "tổng sau giảm giá", "tổng đơn", "tổng bill", "phải thu"),
    "paid_amount": ("paid_amount", "KhachDaTra", "khách đã trả", "khách trả", "Da tra", "đã thu"),
    "payment_id": ("payment_id", "MaPhieuThu", "mã phiếu thu", "phiếu thu"),
    "payment_date": ("payment_date", "NgayGioThu", "ngày thu", "thời gian thu"),
    "payment_amount": ("payment_amount", "SoTienThu", "số tiền thu"),
    "return_quantity": ("return_quantity", "SLTraQuyDoi", "số lượng trả", "số lượng trả (ĐVT cơ bản)", "SL trả (ĐVT cơ bản)"),
    "lot_id": ("lot_id", "MaLo", "mã lô", "lô", "số lô"),
    "expiry_date": ("expiry_date", "HanSuDung", "hạn sử dụng", "hạn dùng", "HSD"),
    "receipt_id": ("receipt_id", "MaPhieuNhap", "mã nhập hàng", "mã đơn nhập", "mã phiếu nhập"),
    "receipt_date": ("receipt_date", "NgayNhapKho", "ngày nhập"),
    "receipt_amount": ("receipt_amount", "TongGiaTriNhap", "cần trả NCC"),
    "inventory_quantity": ("inventory_quantity", "TonCuoi", "Ton kho", "tồn cuối", "tồn cuối kỳ", "tồn thực"),
    "inventory_date": ("inventory_date", "NgayGioPhatSinh", "ngày tồn", "ngày chốt tồn"),
    "price_effective_date": ("price_effective_date", "NgayApDung", "ngày áp dụng"),
    "new_price": ("new_price", "giá mới"),
    "old_price": ("old_price", "giá cũ"),
    "conversion_factor": ("conversion_factor", "HeSoQuyDoi", "hệ số", "hệ số QĐ", "tỉ lệ quy đổi", "tỷ lệ quy đổi"),
    "base_unit": ("base_unit", "DonViCoSo", "đơn vị cơ bản", "đơn vị gốc", "ĐV gốc", "ĐV nhỏ"),
    "payment_method": ("payment_method", "hình thức thanh toán", "phương thức thanh toán", "phương thức TT", "HTTT", "PTTT", "trả bằng"),
    "channel": ("channel", "kênh bán", "kênh bán hàng", "bán qua", "loại bán"),
    "remaining_amount": ("remaining_amount", "ConNo", "còn nợ"),
    "product_group": ("product_group", "NhomHang", "nhóm hàng", "nhóm hàng hóa", "nhóm hàng(3 cấp)", "danh mục", "loại sản phẩm"),
    "employee": ("employee", "NhanVien", "nhân viên", "nhân viên bán", "NV bán"),
}
_LABEL_ROLE = {_token(alias): role for role, aliases in _ALIASES.items() for alias in aliases}


def _role_from_description(description: str, context: str) -> str | None:
    text = _words(description)
    if any(phrase in text for phrase in ("cot trong", "khong co du lieu", "empty column")):
        return None
    # Longer definitions must precede their more general prefixes.
    tests = (
        ("base_quantity", ("so luong da doi ra don vi co ban", "so luong khach tra lai", "so luong ban lay tu lo")),
        ("conversion_factor", ("he so so don vi co ban", "conversion factor")),
        ("base_unit", ("don vi co ban", "base unit")),
        ("line_number", ("so thu tu dong", "line number")),
        ("payment_id", ("ma phieu thu", "payment identifier")),
        ("receipt_id", ("ma phieu nhap", "receipt identifier")),
        ("order_id", ("ma don", "order identifier")),
        ("product_id", ("ma hang", "ma san pham", "product identifier")),
        ("customer_id", ("ma khach", "customer identifier")),
        ("customer_name", ("ten khach", "customer name")),
        ("product_name", ("ten hang", "ten san pham", "product name")),
        ("order_date", ("ngay ban", "ngay dat", "order date")),
        ("created_at", ("thoi diem dat don", "gio dat don", "order creation time")),
        ("status", ("trang thai don", "trang thai phieu nhap", "order status")),
        ("order_goods_amount", ("tong tien hang cua don", "tien hang cua don")),
        ("order_total", ("tong tien don", "so khach phai tra cho ca don", "order total")),
        ("paid_amount", ("da thanh toan", "amount already paid")),
        ("payment_method", ("hinh thuc thanh toan", "phuong thuc thanh toan", "payment method")),
        ("channel", ("kenh ban", "sales channel")),
        ("remaining_amount", ("con no", "remaining debt")),
        ("product_group", ("nhom hang", "product group")),
        ("employee", ("nhan vien", "employee")),
        ("payment_date", ("thoi diem thu", "payment time")),
        ("payment_amount", ("so tien tra lan do", "payment amount")),
        ("receipt_amount", ("tong tien phieu nhap", "tong tien cua ca phieu nhap")),
        ("line_amount", ("thanh tien sau giam", "thanh tien cua dong", "tien nhap cua ca dong", "line amount")),
        ("unit_cost", ("gia von mot don vi", "gia von", "unit cost")),
        ("unit_price", ("don gia", "unit price")),
        ("receipt_date", ("ngay nhap", "receipt date")),
        ("inventory_quantity", ("ton kho", "ton sau lan", "ton cuoi ngay", "stock quantity")),
        ("price_effective_date", ("ngay bat dau ap dung gia", "price effective date")),
        ("new_price", ("gia ban ap dung tu ngay", "gia ban ap dung", "new price")),
        ("old_price", ("gia truoc lan doi", "old price")),
        ("lot_id", ("so lo", "lot identifier")),
        ("expiry_date", ("han su dung", "expiry date")),
        ("quantity", ("so luong", "quantity")),
        ("unit", ("don vi tinh", "don vi nhap", "don vi lon", "selling unit")),
    )
    if "so luong khach tra lai" in text:
        return "return_quantity"
    # "quantity (base units)" is distinct from a unit description.
    if "so luong" in text and "don vi co ban" in text:
        if "da doi" in text or "lay tu lo" in text or "nhap" in text:
            return "base_quantity"
    for role, phrases in tests:
        if any(text.startswith(p) for p in phrases):
            return role
    if text == "ten":
        return "customer_name" if context == "customer" else "product_name" if context in {"product", "unit_conversion", "price_history", "order_item", "receipt_item", "lot", "inventory_snapshot"} else None
    if text == "so tien" and context == "payment":
        return "payment_amount"
    return None


def _source_context(source_name: str) -> str:
    names = _source_tokens(source_name)
    contexts = (
        ("lot_allocation", ("chitietdonlo", "bantheolo", "chitietbandonlo")),
        ("inventory_movement", ("thekho", "stockmovements", "inventorymovements")),
        ("price_history", ("lichsugia", "banggia", "pricehistory")),
        ("unit_conversion", ("donvisp", "quydoi", "donvitinh", "conversion")),
        ("payment", ("thanhtoan", "phieuthu", "soquy", "payments")),
        ("return", ("trahang", "returns")),
        ("receipt_item", ("chitietnhap", "sonhaphang", "receiptitems")),
        ("receipt", ("phieunhap", "danhsachnhaphang", "receipts")),
        ("inventory_snapshot", ("tonkho", "inventorysnapshot")),
        ("lot", ("lohang", "lots")),
        ("customer", ("khachhang", "customers")),
        ("product", ("sanpham", "hanghoa", "products", "danhmuc")),
        ("order_item", ("chitiethoadon", "chitietdon", "donhangchitiet", "salesitems", "orderitems")),
        ("order", ("hoadon", "donhang", "orders")),
    )
    for context, fragments in contexts:
        if any(fragment in name for name in names for fragment in fragments):
            return context
    return "unknown"


def duplicate_policy(rulebook: str) -> dict[str, Any]:
    """Resolve the same-order exact-duplicate rule only from supplied text."""
    same_order: list[str] = []
    orderless: list[str] = []
    for line in rulebook.splitlines():
        text = _words(line)
        if (("cung mot don" in text and "giong nhau" in text and "tinh mot lan" in text)
                or ("same order" in text and "identical" in text and "once" in text)):
            same_order.append(line)
        if (("khong ghi theo don" in text and "giong nhau" in text and "tinh ca hai" in text)
                or ("orderless" in text and "identical" in text and "preserve" in text)):
            orderless.append(line)
    return {
        "status": "resolved" if same_order else "unresolved",
        "same_order_exact": bool(same_order),
        "preserve_orderless": True,
        "evidence": same_order + orderless,
    }


def sales_eligibility_policy(rulebook: str) -> dict[str, Any]:
    """Extract an explicit sales default; absent/changed rules stay unresolved."""
    candidates = []
    for line in rulebook.splitlines():
        text = _words(line)
        if ("don tinh vao doanh thu" in text or "sales eligibility" in text or "revenue eligibility" in text):
            candidates.append(line)
    text = _words(" ".join(candidates))
    cancelled = bool(re.search(r"khong huy|exclude cancelled|exclude canceled", text))
    returned = bool(re.search(r"khong tra hang|exclude returned|exclude returns", text))
    whole_order = "bo ca don" in text or "whole order" in text
    future_lines = [line for line in rulebook.splitlines() if "ke ca khi phieu tra ghi ngay sau" in _words(line)]
    resolved = cancelled and returned and whole_order
    return {
        "status": "resolved" if resolved else "unresolved",
        "exclude_cancelled": True if cancelled else None,
        "exclude_returned": True if returned else None,
        "whole_order_returns": True if whole_order and returned else None,
        "include_shipping": True if "dang giao van tinh" in text else None,
        "later_return_evidence_applies": True if future_lines else None,
        "excerpt": "\n".join(candidates + future_lines),
        "evidence": candidates + future_lines,
    }


def return_evidence_policy(readme: str) -> dict[str, Any]:
    """Return quantities are usable only where the README states their meaning."""
    negative: list[str] = []
    positive: list[str] = []
    for line in readme.splitlines():
        text = _words(line)
        if "so luong am" in text and "cung ma don" in text and "tra hang" in text:
            negative.append(line)
        if "so luong khach tra lai" in text and "lon hon 0" in text and "tra hang" in text:
            positive.append(line)
    return {
        "negative_quantity_is_return": bool(negative),
        "positive_return_quantity_is_return": bool(positive),
        "evidence": negative + positive,
    }


_STATUS_LABELS = {
    "completed": {"hoan thanh", "da hoan thanh", "hoan tat", "da hoan tat", "xong", "da xong", "thanh cong",
                  "giao thanh cong", "da giao", "da giao hang", "da nhan", "da nhan hang", "da lay", "da lay hang",
                  "ok", "completed", "complete", "done", "delivered", "success"},
    "shipping": {"dang giao", "dang giao hang", "dang giao dich", "dang van chuyen", "dang xu ly", "dang gui",
                 "dang ship", "cho giao", "cho giao hang", "cho xu ly", "cho lay hang", "shipping", "in delivery",
                 "processing", "in progress"},
    "cancelled": {"huy", "da huy", "bi huy", "huy don", "don huy", "da huy don", "khach huy", "huy bo", "da huy bo",
                  "khong giao duoc", "cancelled", "canceled", "cancel"},
    "returned": {"tra hang", "da tra hang", "hoan tra", "da hoan tra", "doi tra", "tra lai", "da tra lai",
                 "hoan hang", "da hoan hang", "hang tra ve", "khach tra hang", "returned", "return", "refunded"},
}
CANONICAL_STATUSES = ("completed", "shipping", "cancelled", "returned")


def canonical_status(value: Any) -> str:
    """Map one status label to a rulebook state by exact, accent-folded vocabulary.

    Returns completed, shipping, cancelled, returned or unknown. Qualified or
    negated labels ("hoàn thành một phần", "không huỷ") never match.
    """
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    text = _words(value)
    for status, values in _STATUS_LABELS.items():
        if text in values:
            return status
    return "unknown"


def map_status_labels(values: Iterable[Any]) -> tuple[dict[str, str], str | None]:
    """Map every distinct status label of one source to a canonical state.

    The rulebook gives each order exactly one of four states. When exactly one
    nonblank label is unfamiliar and exactly one state has no label, the
    unfamiliar label is that state. Returns the mapping and the label inferred
    this way, if any; other unfamiliar labels stay unknown.
    """
    labels = [value for value in values if isinstance(value, str) and value.strip()]
    mapping = {label: canonical_status(label) for label in labels}
    unknown = [label for label, state in mapping.items() if state == "unknown"]
    missing = [state for state in CANONICAL_STATUSES if state not in mapping.values()]
    folded = {_words(label) for label in unknown}
    if len(folded) == 1 and len(missing) == 1:
        for label in unknown:
            mapping[label] = missing[0]
        return mapping, unknown[0]
    return mapping, None


def classify_status(value: Any) -> str:
    """Known status vocabulary only. Missing or unfamiliar never means active."""
    state = canonical_status(value)
    return "active" if state in ("completed", "shipping") else state


def propose_table(
    labels: list[str], *, source_name: str, readme: str, rulebook: str
) -> dict[str, Any]:
    """Propose a documented grain and roles without touching records or answers.

    README definitions override exact label aliases, including suppressing roles
    for explicitly empty fields. Multiple columns claiming the same role are
    left unresolved rather than choosing whichever happened to appear first.
    """
    context = _source_context(source_name)
    definitions = _scoped_descriptions(readme, source_name)
    candidates: dict[str, list[int]] = {}
    evidence: list[dict[str, Any]] = []
    for position, label in enumerate(labels):
        lookup_label = label.rsplit(" / ", 1)[1] if " / " in label else label
        definition = definitions.get(_token(label)) or _inline_description(readme, source_name, label)
        if definition is None and lookup_label != label:
            definition = definitions.get(_token(lookup_label)) or _inline_description(readme, source_name, lookup_label)
        if definition:
            description, excerpt = definition
            role = _role_from_description(description, context)
            basis = "readme"
            if not description:
                evidence.append({"kind": "ambiguous_definition", "position": position, "excerpt": excerpt})
        else:
            role = _LABEL_ROLE.get(_token(label)) or _LABEL_ROLE.get(_token(lookup_label))
            excerpt = label
            basis = "label"
            contextual = {
                "unit_conversion": {"quydoi": "conversion_factor", "bang": "conversion_factor", "donviquydoi": "unit"},
                "receipt": {"sophieu": "receipt_id"},
                "receipt_item": {"sophieu": "receipt_id"},
                "order_item": {"magiaodich": "order_id", "gianiemyet": "unit_price", "giaban": "unit_price"},
            }
            contextual_role = contextual.get(context, {}).get(_token(label))
            if contextual_role:
                role, basis = contextual_role, "source_context"
                excerpt = source_name + ": " + label
        if role:
            candidates.setdefault(role, []).append(position)
            evidence.append({"kind": basis, "role": role, "position": position, "excerpt": excerpt})
    roles = {role: positions[0] for role, positions in candidates.items() if len(positions) == 1}
    for role, positions in candidates.items():
        if len(positions) > 1:
            evidence.append({"kind": "ambiguous_role", "role": role, "positions": positions, "excerpt": "Multiple columns propose the same role; explicit mapping required."})

    # Source context disambiguates otherwise conventional names; it never adds
    # a column that is not present. Generic TenGoi is safe only in a catalogue.
    if context in {"product", "customer"} and "order_id" not in roles:
        entity = "product" if context == "product" else "customer"
        for position, label in enumerate(labels):
            if _token(label) in {"tengoi", "name", "ten"} and f"{entity}_name" not in roles:
                roles[f"{entity}_name"] = position
                evidence.append({"kind": "source_context", "role": f"{entity}_name", "position": position, "excerpt": source_name})
        if "created_at" in roles:
            # Catalogue creation dates are not order creation dates.
            roles.pop("created_at")
    if context == "price_history":
        for position, label in enumerate(labels):
            if _token(label) in {"giabanle", "giaban", "price"} and "new_price" not in roles:
                roles["new_price"] = position
                roles.pop("unit_price", None)
                evidence.append({"kind": "source_context", "role": "new_price", "position": position, "excerpt": source_name})
    if context in {"receipt", "receipt_item", "lot"}:
        # Purchase prices must not masquerade as sales prices.
        if "unit_price" in roles:
            roles.pop("unit_price")
        if "order_date" in roles:
            roles.pop("order_date")

    has_product = "product_id" in roles or "product_name" in roles
    if context == "lot_allocation" or ({"order_id", "line_number", "lot_id"} <= roles.keys()):
        grain = "lot_allocation"
    elif "payment_id" in roles or (context == "payment" and "payment_amount" in roles):
        grain = "payment"
    elif context == "lot" and "lot_id" in roles:
        grain = "lot"
    elif "receipt_id" in roles:
        grain = "receipt_item" if has_product else "receipt"
    elif "price_effective_date" in roles and "new_price" in roles:
        grain = "price_history"
    elif "conversion_factor" in roles and has_product:
        grain = "unit_conversion"
    elif "inventory_quantity" in roles and has_product:
        grain = "inventory_movement" if context == "inventory_movement" else "inventory_snapshot"
    elif context == "return" and "order_id" in roles:
        grain = "return"
    elif "order_id" in roles:
        grain = "order_item" if has_product and any(r in roles for r in ("quantity", "base_quantity", "line_amount")) else "order"
    elif has_product and "order_date" in roles and any(r in roles for r in ("quantity", "base_quantity", "line_amount")):
        grain = "sale_recording"
    elif context in {"product", "customer"} and f"{context}_id" in roles:
        grain = context
    elif context == "product" and "product_name" in roles:
        grain = "product"
    else:
        grain = "unknown"
    key_recipes = {
        "order": ["order_id"], "order_item": ["order_id", "line_number"],
        "payment": ["payment_id"], "receipt": ["receipt_id"],
        "receipt_item": ["receipt_id", "line_number"],
        "lot_allocation": ["order_id", "line_number", "lot_id"],
        "lot": ["lot_id"], "customer": ["customer_id"],
        "product": ["product_id"] if "product_id" in roles else ["product_name"],
        "unit_conversion": ["product_id" if "product_id" in roles else "product_name", "unit"],
        "price_history": ["product_id" if "product_id" in roles else "product_name", "price_effective_date"],
    }
    wanted_keys = key_recipes.get(grain, [])
    key_roles = wanted_keys if all(role in roles for role in wanted_keys) else []
    duplicate_rule = duplicate_policy(rulebook)
    deduplicate = grain in {"order", "order_item"} and "order_id" in roles and duplicate_rule["same_order_exact"]
    evidence.append({"kind": "grain_proposal", "grain": grain, "excerpt": "Proposed from documented roles; validate against all records before publication."})
    if deduplicate:
        evidence.extend({"kind": "rulebook", "decision": "same_order_exact", "excerpt": line} for line in duplicate_rule["evidence"])
    return {"grain": grain, "roles": roles, "key_roles": key_roles,
            "duplicate_policy": "same_order_exact" if deduplicate else "preserve", "evidence": evidence}
