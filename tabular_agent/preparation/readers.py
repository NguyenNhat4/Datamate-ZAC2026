"""Read-only, streaming source discovery and explicit spreadsheet row classification.

Consumers must exhaust ``RawTable.rows`` before requesting the next table. Discovery
uses bounded lookahead; extraction streams the complete source without worksheet
maximum-dimension assumptions. No row is discarded by the reader.
"""
from __future__ import annotations

import csv
import atexit
import threading
import hashlib
import itertools
import io
import re
import posixpath
import sqlite3
import unicodedata
import zipfile
from collections import Counter, OrderedDict
from contextlib import contextmanager
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries


@dataclass(frozen=True)
class SourceInfo:
    source_id: str
    path: str
    format: str
    source_hash: str
    sheet: str | None = None
    table: str | None = None
    epoch: str | None = None
    display_alias: str | None = None


@dataclass
class RawRow:
    number: int
    values: list
    classification: str = "record"
    reason: str | None = None


@dataclass
class RawTable:
    source: SourceInfo
    region_id: str
    labels: list[str]
    header_rows: list[int]
    rows: Iterator[RawRow]
    metadata: dict = field(default_factory=dict)


class SourceReadError(ValueError):
    """An unsafe or unreadable source; callers should record it as blocked."""


@lru_cache(maxsize=8192)
def _fold_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold().replace("đ", "d").replace("_", " ")
    return " ".join(re.sub(r"[^\w]+", " ", "".join(c for c in text if not unicodedata.combining(c))).split())


def _fold(value: Any) -> str:
    return _fold_text("" if value is None else str(value))


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _trim(values: list) -> list:
    end = len(values)
    while end and not _present(values[end - 1]):
        end -= 1
    return values[:end]


def _safe_path(business: Path, relative: str | Path) -> Path:
    business = Path(business).absolute()
    if business.is_symlink():
        raise SourceReadError("Business directory is a symbolic link")
    candidate = business / relative
    try:
        parts = candidate.relative_to(business).parts
    except ValueError as exc:
        raise SourceReadError("Source path escapes the business directory") from exc
    cursor = business
    for part in parts:
        if part in {"..", "."}:
            raise SourceReadError("Source path contains traversal")
        cursor /= part
        if cursor.is_symlink():
            raise SourceReadError("Symbolic links are not permitted as sources")
    if not candidate.resolve().is_relative_to(business.resolve()):
        raise SourceReadError("Source path escapes the business directory")
    return candidate


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alias(path: str) -> str:
    try:
        repaired = path.encode("cp437").decode("utf-8")
        return repaired if repaired != path else path
    except (UnicodeError, LookupError):
        return path


def _sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def discover_sources(business: Path) -> list[SourceInfo]:
    """Inventory every supported physical file, worksheet, and SQLite table.

    Unreadable containers receive a file-level source so iter_tables can report a
    blocked source rather than make the file disappear from inventory.
    """
    business = Path(business)
    _safe_path(business, "")
    sources: list[SourceInfo] = []
    paths = sorted(business.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise SourceReadError(f"Symbolic link in source directory: {path.relative_to(business)}")
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        kind = {".xlsx": "xlsx", ".csv": "csv", ".sqlite": "sqlite", ".sqlite3": "sqlite", ".db": "sqlite"}.get(suffix)
        if kind is None:
            continue
        relative = path.relative_to(business).as_posix()
        _safe_path(business, relative)
        checksum = _hash(path)
        selectors: list[tuple[str | None, str | None, str | None]] = []
        try:
            if kind == "xlsx":
                sheets, epoch = _workbook_layout(path)
                selectors = [(name, None, epoch) for name in sheets]
            elif kind == "sqlite":
                connection = _sqlite(path)
                try:
                    names = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                    selectors = [(None, name, None) for (name,) in names]
                finally:
                    connection.close()
            else:
                selectors = [(None, None, None)]
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            selectors = [(None, None, None)]
        if not selectors:
            selectors = [(None, None, None)]
        for sheet, table, epoch in selectors:
            identity = hashlib.sha256(f"{relative}\0{sheet or ''}\0{table or ''}".encode()).hexdigest()[:24]
            sources.append(SourceInfo(identity, relative, kind, checksum, sheet, table, epoch, _alias(relative)))
    return sources


_HEADER_WORDS = {
    "id", "date", "name", "product", "sales", "inventory", "quantity", "amount", "price", "status", "customer", "order", "unit", "code", "value",
    "ma", "ngay", "ten", "so luong", "don gia", "thanh tien", "tong tien", "trang thai", "don vi", "gia", "ton", "doanh thu",
    "ban", "nhap", "xuat", "nam", "thang", "ghi chu", "dien thoai", "email", "sdt", "stt", "khach", "hang", "sku", "san pham",
    "so ngay", "han", "so tien", "du no", "con no", "chung tu", "so phieu", "nhan vien", "noi dung", "chiet khau", "vat", "giam gia", "receipt", "payment", "cost", "created at", "paid at", "thu", "chi", "han su dung", "tien",
}


def _header_label(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 100:
        return False
    folded = _fold(value)
    return any(folded == word or folded.startswith(word + " ") or folded.endswith(" " + word) for word in _HEADER_WORDS)


def _header_strength(values: list) -> float:
    cells = [v for v in _trim(values) if _present(v)]
    if len(cells) < 2 or any(not isinstance(v, str) for v in cells):
        return 0.0
    hits = sum(_header_label(v) for v in cells)
    return hits / len(cells) if hits >= 2 else 0.0


def _labels(rows: list[tuple[int, list]], header_rows: list[int], width: int, merges: list[tuple[int, int, int, int]]) -> list[str]:
    selected = {n: list(v) + [None] * (width - len(v)) for n, v in rows if n in header_rows}
    for left, top, right, bottom in merges:
        if top not in selected:
            continue
        value = selected[top][left - 1]
        for n in range(top, bottom + 1):
            if n in selected:
                for col in range(left - 1, min(right, width)):
                    selected[n][col] = value
    # Sparse parent header bands conventionally span until the next parent
    # label, including exports which lost their Excel merge metadata. Propagate
    # within header bands only; never fill record cells here.
    for n in header_rows[:-1]:
        parent = selected.get(n, [])
        if 1 < sum(_present(value) for value in parent) < width:
            previous = None
            for col, value in enumerate(parent):
                if _present(value):
                    previous = value
                elif previous is not None:
                    parent[col] = previous
    result, seen = [], Counter()
    for col in range(width):
        pieces: list[str] = []
        for n in header_rows:
            value = selected[n][col]
            if _present(value) and str(value).strip() not in pieces:
                pieces.append(str(value).strip())
        label = " / ".join(pieces) or f"column_{col + 1}"
        seen[label] += 1
        result.append(label if seen[label] == 1 else f"{label}__{seen[label]}")
    return result


def _choose_header(sample: list[tuple[int, list]], merges: list[tuple[int, int, int, int]], override: dict) -> tuple[list[int], int, list[str]]:
    if override.get("header_rows"):
        headers = [int(n) for n in override["header_rows"]]
        width = int(override.get("width", max((len(_trim(v)) for n, v in sample if n in headers), default=0)))
        return headers, width, []
    candidates = [(n, v, _header_strength(v)) for n, v in sample]
    strong = [(n, v, score) for n, v, score in candidates if score >= 0.5]
    issues: list[str] = []
    if strong:
        # Prefer the earliest supported table. A two-cell report filter followed
        # by a wider header is not a table, but an earlier narrow table followed
        # by a wider *different* table must not be swallowed as report text.
        viable = []
        for candidate in strong:
            cn, cv, _ = candidate
            following = next(((nn, nv) for nn, nv in sample if nn > cn and _trim(nv)), None)
            if following:
                nn, nv = following
                grouped = nn == cn + 1 and any(top == cn and right > left for left, top, right, bottom in merges)
                record_shape = _header_strength(nv) < 0.5 and 1 < len(_trim(nv)) <= len(_trim(cv))
                if grouped or record_shape:
                    viable.append(candidate)
        n, values, score = min(viable, key=lambda item: item[0]) if viable else max(strong, key=lambda item: (sum(_header_label(v) for v in item[1]), item[2], -item[0]))
    else:
        nonempty = [(n, v) for n, v in sample if _trim(v)]
        if not nonempty:
            return [], 0, ["empty_source"]
        # A fully textual, rectangular first row is a conventional weak header.
        n, values = nonempty[0]
        if len(_trim(values)) < 2 or any(not isinstance(v, str) for v in _trim(values)):
            return [], max((len(_trim(v)) for _, v in sample), default=0), ["unresolved_header"]
        issues.append("weak_header_evidence")
    width = len(_trim(values))
    headers = [n]
    previous = next(((pn, pv) for pn, pv in reversed(sample) if pn < n and _trim(pv)), None)
    if previous and previous[0] == n - 1:
        pn, pv = previous
        span = any(top == pn and right > left and bottom < n + 1 for left, top, right, bottom in merges)
        sparse_group = len(_trim(pv)) <= width and 1 < sum(_present(v) for v in pv) < width and _header_strength(pv) >= 0.5
        if span or sparse_group:
            headers.insert(0, pn)
    # A merged parent row can itself look strongest. Include its textual child.
    next_row = next(((nn, nv) for nn, nv in sample if nn == n + 1), None)
    if next_row and any(top == n and right > left for left, top, right, bottom in merges):
        nn, nv = next_row
        if len(_trim(nv)) >= width and all(isinstance(v, str) for v in nv if _present(v)) and _header_strength(nv) >= 0.5:
            headers.append(nn)
            width = max(width, len(_trim(nv)))
    return headers, width, issues


def _workbook_layout(path: Path) -> tuple[dict[str, str], str]:
    """Read workbook metadata without loading its potentially huge shared strings."""
    with zipfile.ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for relation in relationships:
        if relation.attrib.get("TargetMode") == "External":
            continue
        target = relation.attrib["Target"]
        targets[relation.attrib["Id"]] = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
    sheets = {}
    epoch = "1900"
    for element in workbook.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local == "workbookPr" and element.attrib.get("date1904", "").casefold() in {"1", "true"}:
            epoch = "1904"
        elif local == "sheet":
            reference = element.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            if reference not in targets:
                raise SourceReadError("Workbook sheet references an unavailable worksheet")
            sheets[element.attrib["name"]] = targets[reference]
    return sheets, epoch


def _xlsx_metadata(path: Path, sheet: str) -> dict:
    sheets, _ = _workbook_layout(path)
    xml_path = sheets[sheet]
    merges, uncached = [], set()
    formulas: dict[str, dict] = {}
    # Most exports have neither formulas nor merged cells. Scan compressed XML
    # sequentially first rather than constructing millions of cell elements.
    special = False
    with zipfile.ZipFile(path) as archive, archive.open(xml_path) as handle:
        carry = b""
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            probe = carry + chunk
            if re.search(rb"<(?:[A-Za-z_][\w.-]*:)?(?:mergeCell|f)(?:[\s/>])", probe):
                special = True
                break
            carry = probe[-128:]
    if not special:
        return {"merged_ranges": [], "uncached_formula_rows": [], "formula_cells": {}}
    with zipfile.ZipFile(path) as archive, archive.open(xml_path) as handle:
        for event, element in ElementTree.iterparse(handle, events=("end",)):
            local = element.tag.rsplit("}", 1)[-1]
            if local == "mergeCell":
                merges.append(range_boundaries(element.attrib["ref"]))
            elif local == "c":
                children = {child.tag.rsplit("}", 1)[-1]: child for child in element}
                if "f" in children:
                    coordinate = element.attrib.get("r", "")
                    cached = children.get("v")
                    formulas[coordinate] = {"formula": children["f"].text, "cached_value": cached.text if cached is not None else None}
                    if cached is None or cached.text is None:
                        match = re.search(r"\d+", coordinate)
                        if match:
                            uncached.add(int(match.group()))
                element.clear()
            elif local == "row":
                element.clear()
    return {"merged_ranges": merges, "uncached_formula_rows": sorted(uncached), "formula_cells": formulas}


def _csv_settings(path: Path) -> dict:
    with path.open("rb") as handle:
        start = handle.read(4)
    encoding = "utf-16" if start.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    # Validate the whole stream: a late decoding error must not produce partial data.
    try:
        with path.open(encoding=encoding, newline="") as handle:
            for _ in iter(lambda: handle.read(1024 * 1024), ""):
                pass
    except UnicodeDecodeError as exc:
        raise SourceReadError("Unsupported CSV encoding; supply an explicit encoding override") from exc
    with path.open(encoding=encoding, newline="") as handle:
        sample = handle.read(65536)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter, quotechar = dialect.delimiter, dialect.quotechar
    except csv.Error:
        # Report titles and blank separators make Sniffer reject otherwise
        # ordinary CSVs. Prefer the delimiter exposing actual header labels.
        choices = []
        for candidate in (",", ";", "\t", "|"):
            preview = list(itertools.islice(csv.reader(io.StringIO(sample), delimiter=candidate), 64))
            hits = max((sum(_header_label(v) for v in row) for row in preview if len(row) > 1), default=0)
            widths = Counter(len(row) for row in preview if len(row) > 1)
            regularity = max(widths.values(), default=0)
            choices.append((hits, regularity, candidate))
        delimiter = max(choices, key=lambda item: (item[0], item[1], item[2] == ","))[2]
        quotechar = '"'
    return {"encoding": encoding, "delimiter": delimiter, "quotechar": quotechar}


# Consecutive monthly sheets otherwise repeatedly decode the complete workbook
# string catalog. Keep at most two idle read-only workbooks; active iterators pin
# their entry so a concurrent reader cannot close a workbook beneath them.
_WORKBOOKS: OrderedDict[tuple, list] = OrderedDict()
_WORKBOOK_LOCK = threading.RLock()


def _trim_workbook_cache(limit: int = 2) -> None:
    for key, entry in list(_WORKBOOKS.items()):
        if len(_WORKBOOKS) <= limit:
            break
        if entry[1] == 0:
            entry[0].close()
            del _WORKBOOKS[key]


def _close_workbook_cache() -> None:
    with _WORKBOOK_LOCK:
        for workbook, _ in _WORKBOOKS.values():
            workbook.close()
        _WORKBOOKS.clear()


atexit.register(_close_workbook_cache)


@contextmanager
def _read_workbook(path: Path, source_hash: str):
    # Stat protects against reuse after an external mutation despite a stale
    # SourceInfo. Content hashes remain the authoritative bundle identity.
    stat = path.stat()
    key = (str(path.resolve()), source_hash, stat.st_size, stat.st_mtime_ns)
    with _WORKBOOK_LOCK:
        entry = _WORKBOOKS.get(key)
        if entry is None:
            entry = [load_workbook(path, read_only=True, data_only=True, keep_links=False), 0]
            _WORKBOOKS[key] = entry
        _WORKBOOKS.move_to_end(key)
        entry[1] += 1
        _trim_workbook_cache()
    try:
        yield entry[0]
    finally:
        with _WORKBOOK_LOCK:
            entry[1] -= 1
            _trim_workbook_cache()


def _rows(path: Path, source: SourceInfo, metadata: dict) -> Iterator[tuple[int, list]]:
    if source.format == "csv":
        with path.open(encoding=metadata["encoding"], newline="") as handle:
            reader = csv.reader(handle, delimiter=metadata["delimiter"], quotechar=metadata["quotechar"], strict=True)
            while True:
                number = reader.line_num + 1
                try:
                    values = next(reader)
                except StopIteration:
                    return
                except csv.Error as exc:
                    raise SourceReadError(f"Malformed CSV near physical row {number}: {exc}") from exc
                yield number, values
    elif source.format == "xlsx":
        with _read_workbook(path, source.source_hash) as workbook:
            worksheet = workbook[source.sheet]
            worksheet.reset_dimensions()
            for number, values in enumerate(worksheet.iter_rows(values_only=True), 1):
                yield number, list(values)
    elif source.format == "sqlite":
        connection = _sqlite(path)
        try:
            cursor = connection.execute(f"SELECT * FROM {_quote(source.table or '')}")
            for number, values in enumerate(cursor, 1):
                yield number, list(values)
        finally:
            connection.close()
    else:
        raise SourceReadError(f"Unsupported source format: {source.format}")


def _classify(number: int, values: list, *, headers: list[int], labels: list[str], original_headers: list[list], width: int, uncached: set[int], unresolved_header: bool) -> RawRow:
    trimmed = _trim(values)
    if number in uncached:
        return RawRow(number, values, "unresolved", "formula_without_cached_value")
    if not trimmed:
        return RawRow(number, values, "blank", "blank_separator")
    if number in headers:
        return RawRow(number, values, "header", "column_header")
    if headers and number < min(headers):
        return RawRow(number, values, "title", "before_column_header")
    if unresolved_header:
        return RawRow(number, values, "unresolved", "unresolved_header")
    populated_count = sum(_present(v) for v in trimmed)
    repeated_candidate = any(_fold(trimmed[0]) == _fold(h[0] if h else None) for h in original_headers)
    possible_control = any(isinstance(v, str) and re.search(r"tổng|tong|cộng|cong|total|ghi\s*ch|notes?|ch[uú]\s*th[ií]ch", v, re.IGNORECASE) for v in trimmed)
    if not repeated_candidate and not possible_control and number not in uncached and len(trimmed) <= width and (populated_count > 1 or width <= 1):
        return RawRow(number, values + [None] * max(0, width - len(values)))
    folded = [_fold(v) for v in trimmed]
    if any(folded == [_fold(v) for v in _trim(row)] for row in original_headers):
        return RawRow(number, values, "header", "repeated_header")
    populated = [(i, v) for i, v in enumerate(trimmed) if _present(v)]
    summary_pattern = r"(?:tong(?: cong| ngay| thang| quy| nam)?|cong(?: ngay| thang| quy| nam)?|grand total|subtotal|total)(?: \d{1,4}(?: \d{1,4}){0,2})?"
    summaries = [(i, v) for i, v in populated if isinstance(v, str) and re.fullmatch(summary_pattern, _fold(v))]
    identifying = []
    for i, label in enumerate(labels):
        label = _fold(label.rsplit(" / ", 1)[-1])
        if label == "id" or label.startswith(("ma ", "ngay ", "date ")) or label.endswith((" id", " date")) or label in {"ngay", "date", "order id", "code"}:
            identifying.append(i)
    note_positions = [i for i, v in populated if isinstance(v, str) and (_fold(v).startswith(("ghi chu ", "chu thich ", "note ", "notes ")) or _fold(v) in {"ghi chu", "note", "notes", "chu thich"})]
    has_record_identity = any(i < len(values) and _present(values[i]) and i not in note_positions and not any(i == position for position, _ in summaries) for i in identifying)
    # Export totals often put their label in a customer/product column. Require
    # an exact label plus numeric evidence and no populated identity/date field.
    # Missing IDs alone never remove a row; a real product named "Tổng ngày"
    # with a source ID or date remains a record.
    numeric_companion = any(isinstance(v, (int, float)) or re.match(r"^[+-]?\d", str(v).strip()) for i, v in populated if not any(i == position for position, _ in summaries))
    if summaries and not has_record_identity and numeric_companion and (len(populated) < width or summaries[0][0] == 0):
        return RawRow(number, values, "summary", "explicit_summary_label_without_record_identity")
    first = _fold(populated[0][1])
    if not has_record_identity and (first.startswith(("ghi chu ", "chu thich ", "note ", "notes ")) or first in {"ghi chu", "note", "notes", "chu thich"}) and len(populated) <= 2:
        return RawRow(number, values, "note", "explicit_note_label")
    if len(trimmed) > width:
        return RawRow(number, values, "unresolved", "extra_populated_columns")
    if width > 1 and len(populated) == 1:
        # A sparse row can be a note or an incomplete record; never guess it away.
        return RawRow(number, values, "unresolved", "sparse_record_or_note")
    return RawRow(number, values + [None] * max(0, width - len(values)))


def iter_tables(business: Path, source: SourceInfo, *, layout_overrides: dict | None = None) -> Iterator[RawTable]:
    """Yield regions with complete, streaming row accounting and original coordinates.

    Overrides may be supplied directly or under source_id. Supported keys are
    header_rows, width, labels, encoding, delimiter, quotechar. Specifications are
    data only; no expressions or executable transformations are accepted.
    """
    path = _safe_path(Path(business), source.path)
    overrides = dict(layout_overrides or {})
    override = overrides.get(source.source_id, overrides)
    if not isinstance(override, dict):
        raise SourceReadError("Layout override must be an object")
    metadata: dict = {"reader_version": "1", "source_labels": [], "column_positions": [], "layout_issues": [], "scan_coverage": "full"}
    if source.format == "csv":
        if override.get("encoding"):
            metadata.update({"encoding": override["encoding"], "delimiter": override.get("delimiter", ","), "quotechar": override.get("quotechar", '"')})
        else:
            metadata.update(_csv_settings(path))
        metadata.update({k: override[k] for k in ("delimiter", "quotechar") if k in override})
    elif source.format == "xlsx":
        if source.sheet is None:
            raise SourceReadError("Workbook inventory failed; worksheet is unavailable")
        metadata.update(_xlsx_metadata(path, source.sheet))
    elif source.format == "sqlite":
        if source.table is None:
            raise SourceReadError("Database inventory failed or contains no tables")
        connection = _sqlite(path)
        try:
            columns = connection.execute(f"PRAGMA table_info({_quote(source.table)})").fetchall()
        finally:
            connection.close()
        labels = [str(row[1]) for row in columns]
        metadata.update({"source_labels": labels, "column_positions": list(range(1, len(labels) + 1)), "declared_types": [row[2] for row in columns], "declared_primary_key": [row[1] for row in columns if row[5]], "extraction": {"header_rows": [], "width": len(labels)}})
        yield RawTable(source, "region_1", labels, [], (RawRow(n, v) for n, v in _rows(path, source, metadata)), metadata)
        return
    rows = iter(_rows(path, source, metadata))
    sample = list(itertools.islice(rows, 64))
    merges = metadata.get("merged_ranges", [])
    headers, width, issues = _choose_header(sample, merges, override)
    labels = list(override.get("labels", _labels(sample, headers, width, merges)))
    if len(labels) != width:
        raise SourceReadError("Override labels do not match the table width")
    source_headers = [v for n, v in sample if n in headers]
    start = 1
    region = 0
    pending = list(sample)
    rows = itertools.chain(pending, rows)
    # Each region shares the underlying iterator. A one-row boundary buffer is
    # retained, so even large/multiple tables do not require materializing rows.
    while pending:
        region += 1
        boundary: list[tuple[int, list]] = []
        region_metadata = dict(metadata)
        region_metadata.update({"source_header_values": {str(n): v for n, v in pending if n in headers}, "source_labels": labels, "column_positions": list(range(1, width + 1)), "layout_issues": list(issues), "extraction": {"header_rows": list(headers), "width": width, "start_row": start, "summary_policy": "exact_summary_label_sparse_numeric_row_without_record_identity", "note_policy": "explicit_note_label", "repeated_header_policy": "exact_normalized_header"}})
        current_headers, current_labels, current_width = list(headers), list(labels), width
        current_original = list(source_headers)
        uncached_rows = set(metadata.get("uncached_formula_rows", []))
        stream = rows
        finished = False

        def classified() -> Iterator[RawRow]:
            nonlocal finished
            last = max(current_headers, default=0)
            for n, values in stream:
                normalized = [_fold(v) for v in _trim(values)]
                repeated = any(normalized == [_fold(v) for v in _trim(h)] for h in current_original)
                if n > last and _header_strength(values) >= 0.75 and not repeated and len(_trim(values)) >= 2 and current_headers:
                    boundary.append((n, values))
                    break
                yield _classify(n, values, headers=current_headers, labels=current_labels, original_headers=current_original, width=current_width, uncached=uncached_rows, unresolved_header="unresolved_header" in issues)
            finished = True

        table = RawTable(source, f"region_{region}", current_labels, current_headers, classified(), region_metadata)
        yield table
        if not finished:
            # Preserve the streaming contract even when a consumer only inspects
            # metadata; drain the region before discovering a following region.
            for _ in table.rows:
                pass
        if not boundary:
            break
        pending = boundary + list(itertools.islice(rows, 63))
        rows = itertools.chain(pending, rows)
        start = boundary[0][0]
        headers, width, issues = _choose_header(pending, merges, {})
        labels = _labels(pending, headers, width, merges)
        source_headers = [v for n, v in pending if n in headers]
