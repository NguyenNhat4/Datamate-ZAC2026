"""Conservative, reproducible scalar normalization for prepared source tables.

Parser specifications are JSON-serializable descriptions, never executable code.
Inference proposes a parser from the supplied evidence; the caller must validate
that proposal over every source record before publishing a typed column.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable


PARSER_VERSION = "1"
_CURRENCY = re.compile(r"\s*(?:vnd|vnđ|đồng|dong|₫|đ|\$)\s*$", re.IGNORECASE)
_NUMERIC = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)*|[.,]\d+)(?:[eE][+-]?\d+)?$")
_PREFIX_QUANTITY = re.compile(r"^\s*([+-]?(?:\d+(?:[.,]\d+)*|[.,]\d+))\s*(.*?)\s*$")
_DATE_DMY = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2})(\.\d{1,6})?)?)?$")
_DATE_NO_YEAR = re.compile(r"^(\d{1,2})[/.](\d{1,2})$")
_DATE_VI = re.compile(r"^(\d{1,2})\s*(?:thg|thang)\s*(\d{1,2})(?:\s*(?:nam\s*)?(\d{4}))?$")
_SERIAL = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def text_key(value: Any) -> str:
    """Return an accent-insensitive comparison key without changing display data."""
    text = unicodedata.normalize("NFKD", str(value)).replace("đ", "d").replace("Đ", "D")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def normalize_name(value: Any) -> str:
    """Return an ASCII SQL-friendly name; callers resolve collisions explicitly."""
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    value = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", value)
    name = re.sub(r"[^a-z0-9]+", "_", text_key(value)).strip("_")
    if not name:
        return "column"
    return "column_" + name if name[0].isdigit() else name


def _blank(value: Any) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _number_parts(value: str) -> tuple[str, int, bool]:
    """Return unsigned number text, exact power-of-ten multiplier, and currency."""
    value = value.strip().replace("\N{MINUS SIGN}", "-")
    negative_parentheses = value.startswith("(") and value.endswith(")")
    if negative_parentheses:
        value = value[1:-1].strip()
    currency = bool(_CURRENCY.search(value))
    value = _CURRENCY.sub("", value).strip()
    normalized = text_key(value)
    multiplier = 0
    abbreviated = re.fullmatch(r"(.+?)\s*(trieu|nghin|ngan|tr|k)(\d*)", normalized)
    if abbreviated:
        number, suffix, remainder = abbreviated.groups()
        multiplier = 6 if suffix in {"tr", "trieu"} else 3
        if remainder:
            # 1tr090 is the conventional decimal abbreviation 1.090 million.
            # A suffix followed by digits must have an integer part before it.
            if not re.fullmatch(r"[+-]?\d+", number.strip()) or len(remainder) > multiplier:
                raise ValueError("Invalid abbreviated number")
            normalized = number.strip() + "." + remainder
        else:
            normalized = number.strip()
    if negative_parentheses:
        if normalized.startswith(("+", "-")):
            raise ValueError("A parenthesized number cannot have another sign")
        normalized = "-" + normalized
    return normalized, multiplier, currency


def _separator_mode(body: str, separator: str, parser: dict[str, Any], *, currency: bool, abbreviated: bool) -> str:
    """Decide a single separator using shape or declared source evidence."""
    if parser.get("decimal_separator") == separator:
        return "decimal"
    if parser.get("thousands_separator") == separator:
        return "grouping"
    parts = body.lstrip("+-").split(separator)
    if len(parts) > 2:
        if 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:]):
            return "grouping"
        raise ValueError("Invalid repeated numeric separator")
    if not parts[0] or set(parts[0]) == {'0'} or len(parts[1]) != 3 or len(parts[0]) > 3:
        return "decimal"
    decimal_evidence = separator in parser.get("decimal_separators", [])
    grouping_evidence = separator in parser.get("grouping_separators", [])
    if decimal_evidence != grouping_evidence:
        return "decimal" if decimal_evidence else "grouping"
    if abbreviated:
        return "decimal"
    if currency or parser.get("currency") == "VND":
        return "grouping"
    raise ValueError("Ambiguous numeric separator; declare the source format")


def _parse_decimal(value: Any, parser: dict[str, Any]) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric quantity")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # Readers may already supply a native float. str preserves that value's
        # available decimal representation; text inputs never pass through float.
        if not math.isfinite(value):
            raise ValueError("Nonfinite number")
        result = Decimal(str(value))
    elif isinstance(value, str):
        body, multiplier, currency = _number_parts(value)
        # Grouped spaces are supported only in groups of exactly three digits.
        if " " in body or "\u00a0" in body or "\u202f" in body:
            body = body.replace("\u00a0", " ").replace("\u202f", " ")
            if not re.fullmatch(r"[+-]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?", body):
                raise ValueError("Invalid space-grouped number")
            body = body.replace(" ", "")
        if not _NUMERIC.fullmatch(body):
            raise ValueError("Unsupported numeric representation")
        if re.search(r"[eE]", body):
            if "," in body or body.count(".") > 1:
                raise ValueError("Scientific notation requires an ungrouped decimal point")
        elif "." in body and "," in body:
            # Mixed punctuation is unambiguous only when the other separator
            # forms valid thousands groups to the left of the decimal separator.
            decimal_separator = "." if body.rfind(".") > body.rfind(",") else ","
            thousands_separator = "," if decimal_separator == "." else "."
            integer, fraction = body.rsplit(decimal_separator, 1)
            if not re.fullmatch(r"[+-]?\d{1,3}(?:" + re.escape(thousands_separator) + r"\d{3})+", integer) or not fraction.isdigit():
                raise ValueError("Invalid mixed numeric separators")
            body = integer.replace(thousands_separator, "") + "." + fraction
        else:
            separator = "." if "." in body else "," if "," in body else None
            if separator:
                mode = _separator_mode(body, separator, parser, currency=currency, abbreviated=bool(multiplier))
                if mode == "grouping":
                    if not re.fullmatch(r"[+-]?\d{1,3}(?:" + re.escape(separator) + r"\d{3})+", body):
                        raise ValueError("Invalid thousands grouping")
                    body = body.replace(separator, "")
                else:
                    if body.count(separator) != 1:
                        raise ValueError("Multiple decimal separators")
                    body = body.replace(separator, ".")
        try:
            result = Decimal(body)
        except InvalidOperation as error:
            raise ValueError("Invalid decimal number") from error
        if multiplier:
            # Alter the exponent rather than multiplying under Decimal's global
            # context, which could silently round a long source value.
            sign, digits, exponent = result.as_tuple()
            result = Decimal((sign, digits, exponent + multiplier))
    else:
        raise ValueError("Unsupported numeric value type")
    if not result.is_finite():
        raise ValueError("Nonfinite number")
    return result


def parse_quantity(value: Any, parser: dict[str, Any] | None = None) -> tuple[Decimal, str | None]:
    """Split an explicit numeric prefix and unit without packaging conversion."""
    parser = parser or {}
    if isinstance(value, (Decimal, int, float)):
        return _parse_decimal(value, parser), None
    if not isinstance(value, str):
        raise ValueError("Unsupported quantity type")
    match = _PREFIX_QUANTITY.fullmatch(value)
    if not match:
        raise ValueError("Quantity must start with a supported number")
    number, unit = match.groups()
    if unit and not re.search(r"[^\W\d_]", unit, re.UNICODE):
        raise ValueError("Invalid quantity unit")
    return _parse_decimal(number, parser), unit or None


def _excel_datetime(value: Any, epoch: Any) -> datetime:
    if epoch is None:
        raise ValueError("Serial dates require a corroborated epoch")
    if epoch in {"1900", "1899-12-30", date(1899, 12, 30), datetime(1899, 12, 30)}:
        origin = datetime(1899, 12, 30)
        windows = True
    elif epoch in {"1904", "1904-01-01", date(1904, 1, 1), datetime(1904, 1, 1)}:
        origin = datetime(1904, 1, 1)
        windows = False
    else:
        raise ValueError("Unsupported serial-date epoch")
    serial = _parse_decimal(value, {"decimal_separator": "."})
    days = int(serial // 1)
    if windows and days == 60:
        raise ValueError("Serial 60 denotes the nonexistent Excel date 1900-02-29")
    if windows and 0 < serial < 60:
        serial += 1
    with localcontext() as context:
        context.prec = max(40, len(serial.as_tuple().digits) + 15)
        microseconds = serial * Decimal(86_400_000_000)
        if microseconds != microseconds.to_integral_value():
            raise ValueError("Serial timestamp exceeds microsecond precision")
        try:
            return origin + timedelta(microseconds=int(microseconds))
        except OverflowError as error:
            raise ValueError("Serial date outside supported calendar") from error


def _parse_datetime(value: Any, parser: dict[str, Any]) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _excel_datetime(value, parser.get("epoch"))
    if not isinstance(value, str):
        raise ValueError("Unsupported date value type")
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid ISO date or timestamp") from error
    vi_match = _DATE_VI.fullmatch(text_key(value))
    if vi_match:
        day, month, year = vi_match.groups()
        year = int(year) if year else parser.get("year")
        if year is None:
            raise ValueError("Date without a year requires an established year")
        return datetime(int(year), int(month), int(day))
    match = _DATE_DMY.fullmatch(value)
    if match:
        day, month, year, hour, minute, second, fraction = match.groups()
        if not parser.get("day_first", True):
            day, month = month, day
        return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), int((fraction or ".0")[1:].ljust(6, "0")))
    if _SERIAL.fullmatch(value) and parser.get("epoch") is not None and parser.get("year") is None:
        return _excel_datetime(value, parser["epoch"])
    match = _DATE_NO_YEAR.fullmatch(value)
    if match:
        if parser.get("year") is None:
            raise ValueError("Date without a year requires an established year")
        day, month = match.groups()
        return datetime(int(parser["year"]), int(month), int(day))
    if _SERIAL.fullmatch(value):
        return _excel_datetime(value, parser.get("epoch"))
    raise ValueError("Unsupported date representation")


def parse_value(value: Any, parser: dict[str, Any]) -> Any:
    """Normalize a scalar or raise ValueError; never turn failed parsing into NULL."""
    if value is None:
        return None
    kind = parser.get("kind", "text")
    if kind == "text":
        return value if isinstance(value, str) else str(value)
    if isinstance(value, str) and not value.strip():
        return None
    if parser.get("status") == "unresolved":
        raise ValueError("Parser has unresolved evidence")
    if kind == "identifier":
        if isinstance(value, bool) or isinstance(value, (float, Decimal)) and not Decimal(str(value)).is_finite():
            raise ValueError("Invalid identifier value")
        # Spaces around a code are typing noise: ' SP0017 ' and 'SP0017' are the same product.
        return value.strip() if isinstance(value, str) else str(value)
    if kind == "decimal":
        return _parse_decimal(value, parser)
    if kind == "quantity":
        return parse_quantity(value, parser)[0]
    if kind in {"date", "datetime"}:
        parsed = _parse_datetime(value, parser)
        return parsed.date() if kind == "date" else parsed
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        key = text_key(value)
        if key in parser.get("true_values", ["true", "yes", "co", "1"]):
            return True
        if key in parser.get("false_values", ["false", "no", "khong", "0"]):
            return False
        raise ValueError("Unsupported boolean representation")
    raise ValueError("Unsupported parser kind")


def representation(value: Any) -> str:
    """Describe an observed representation family without exposing the value."""
    if value is None:
        return "missing"
    if isinstance(value, str) and not value.strip():
        return "blank"
    if isinstance(value, bool):
        return "native_boolean"
    if isinstance(value, datetime):
        return "native_datetime"
    if isinstance(value, date):
        return "native_date"
    if isinstance(value, int):
        return "native_integer"
    if isinstance(value, (float, Decimal)):
        return "native_decimal"
    if not isinstance(value, str):
        return "other"
    stripped = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", stripped):
        return "iso_datetime" if "T" in stripped or " " in stripped else "iso_date"
    if _DATE_DMY.fullmatch(stripped):
        return "day_month_year"
    if _DATE_VI.fullmatch(text_key(stripped)):
        return "vietnamese_date"
    if _DATE_NO_YEAR.fullmatch(stripped):
        return "date_without_year"
    if _CURRENCY.search(stripped):
        return "currency"
    if re.fullmatch(r".*\d\s*(?:trieu|nghin|ngan|tr|k)\d*", text_key(stripped)):
        return "abbreviated_number"
    if _NUMERIC.fullmatch(stripped):
        if "." in stripped and "," in stripped:
            return "mixed_numeric_separators"
        if "." in stripped:
            return "numeric_dot"
        if "," in stripped:
            return "numeric_comma"
        return "integer_text"
    match = _PREFIX_QUANTITY.fullmatch(stripped)
    if match and match[2]:
        return "quantity_with_unit"
    return "text"


def _separator_evidence(values: list[Any]) -> tuple[list[str], list[str]]:
    decimal_separators: set[str] = set()
    grouping_separators: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            body, multiplier, currency = _number_parts(value)
        except ValueError:
            continue
        if not _NUMERIC.fullmatch(body) or re.search(r"[eE]", body):
            continue
        if "." in body and "," in body:
            decimal_separator = "." if body.rfind(".") > body.rfind(",") else ","
            thousands_separator = "," if decimal_separator == "." else "."
            try:
                _parse_decimal(value, {})
            except ValueError:
                continue
            decimal_separators.add(decimal_separator)
            grouping_separators.add(thousands_separator)
        else:
            separator = "." if "." in body else "," if "," in body else None
            if separator:
                try:
                    mode = _separator_mode(body, separator, {}, currency=currency, abbreviated=bool(multiplier))
                except ValueError:
                    continue
                (decimal_separators if mode == "decimal" else grouping_separators).add(separator)
    return sorted(decimal_separators), sorted(grouping_separators)


def infer_parser(label: str, samples: list[Any], *, epoch: str | None = None, year: int | None = None, description: str = "") -> dict[str, Any]:
    """Propose a deterministic parser from column semantics and bounded samples.

    ``status=proposed`` is deliberately not a full-data validation claim. Typed
    inference with unsupported or ambiguous examples is marked ``unresolved``.
    """
    label_key = normalize_name(label).replace("_", " ")
    words = set(label_key.split())
    semantic = label_key + " " + text_key(description)
    values = [value for value in samples if not _blank(value)]
    parser: dict[str, Any] = {
        "version": PARSER_VERSION,
        "kind": "text",
        "status": "proposed",
        "evidence": {"representations": dict(Counter(representation(value) for value in samples)), "sample_count": len(samples)},
    }
    identifier = bool(words & {"id", "code", "sku", "barcode", "sdt", "phone", "tel", "ma", "mst"}) or label_key in {"stt", "so dien thoai", "dien thoai", "so don", "so don hang", "so hoa don", "invoice number", "order number"}
    # A number of days or a day of the month ("Số ngày được nợ", "Hạn nợ (ngày)") is a number, not a date.
    day_count = "so ngay" in label_key or re.search(r"\(\s*ngay\s*\)", text_key(label)) is not None
    temporal = not day_count and (bool(words & {"date", "datetime", "timestamp", "ngay", "created", "updated", "expiry"}) or any(token in label_key for token in ("thoi gian", "han su dung", "han dung", "han sd")))
    quantity = bool(words & {"quantity", "qty", "sl", "stock"}) or any(token in label_key for token in ("so luong", "ton kho", "ton dau", "ton cuoi", "so vien", "so goi"))
    numeric = bool(words & {"amount", "price", "total", "cost", "revenue", "money", "debit", "credit", "discount", "balance", "vat", "gia", "tien", "phi", "no", "von", "percent", "rate", "doanhthu"}) or any(token in label_key for token in ("doanh thu", "chiet khau", "thanh tien", "loi nhuan", "ty le"))
    descriptive = bool(words & {"ten", "name", "description", "note", "notes", "email", "status", "state", "method", "reason", "cach", "kieu", "loai"}) or any(token in label_key for token in ("ghi chu", "trang thai", "hinh thuc", "phuong thuc", "dia chi", "hoat chat")) or label_key in {"unit", "units", "don vi", "don vi tinh", "dvt"}
    # A label such as "Tiến độ" holds words of a number ("tiền") only by accident when the README says it is a status.
    descriptive = descriptive or text_key(description).startswith(("trang thai", "kenh ban", "hinh thuc thanh toan", "phuong thuc thanh toan"))
    if identifier:
        parser.update(kind="identifier", status="validated")
        return parser
    if descriptive:
        parser.update(kind="text", status="validated")
        return parser
    if temporal or values and all(isinstance(value, (date, datetime)) for value in values):
        parser["kind"] = "datetime" if words & {"datetime", "timestamp", "created", "updated"} or any(isinstance(value, datetime) or representation(value) == "iso_datetime" for value in values) else "date"
        parser.update(epoch=epoch, year=year, day_first=True)
    elif values and (all(isinstance(value, bool) for value in values) or words & {"is", "has", "active", "enabled", "boolean", "bool"} and all(text_key(value) in {"true", "false", "yes", "no", "co", "khong", "0", "1"} for value in values)):
        parser.update(kind="boolean", status="validated")
        return parser
    elif quantity:
        parser["kind"] = "quantity"
    elif numeric or values and all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in values):
        parser["kind"] = "decimal"
    elif values:
        # Numeric-looking text is typed only when every sample has a supported
        # representation. Labels carrying identifiers already returned above.
        if any(isinstance(value, str) and re.fullmatch(r"0\d+", value.strip()) for value in values):
            parser.update(kind="identifier", status="proposed")
        elif all(representation(value) in {"integer_text", "numeric_dot", "numeric_comma", "currency", "abbreviated_number", "mixed_numeric_separators", "native_integer", "native_decimal"} for value in values):
            parser["kind"] = "decimal"
        elif all(representation(value) in {"iso_date", "iso_datetime", "day_month_year", "vietnamese_date"} for value in values):
            parser["kind"] = "datetime" if any(representation(value) == "iso_datetime" for value in values) else "date"
            parser.update(epoch=epoch, year=year, day_first=True)
    if parser["kind"] in {"decimal", "quantity"}:
        numeric_values = values
        if parser["kind"] == "quantity":
            numeric_values = [match[1] if isinstance(value, str) and (match := _PREFIX_QUANTITY.fullmatch(value)) else value for value in values]
        decimal_separators, grouping_separators = _separator_evidence(numeric_values)
        parser.update(decimal_separators=decimal_separators, grouping_separators=grouping_separators)
        if re.search(r"\b(?:vnd|dong)\b", semantic):
            parser["currency"] = "VND"
    failures: Counter[str] = Counter()
    for value in values:
        try:
            parse_value(value, parser)
        except (ValueError, TypeError, OverflowError) as error:
            failures[str(error)] += 1
    if failures:
        parser["status"] = "unresolved"
        parser["evidence"]["failure_reasons"] = dict(failures)
    elif parser["kind"] == "text":
        parser["status"] = "validated"
    return parser


def decimal_type(values: Iterable[Decimal | int]) -> tuple[int, int]:
    """Return exact DECIMAL precision/scale requirements, without narrowing them."""
    integer_digits = 0
    scale = 0
    for value in values:
        number = _parse_decimal(value, {})
        _, digits, exponent = number.as_tuple()
        scale = max(scale, -exponent)
        if number:
            integer_digits = max(integer_digits, len(digits) + exponent, 0)
    scale = max(scale, 0)
    return max(1, integer_digits + scale), scale
