"""Trusted table tools loaded inside the isolated Python worker only."""
import csv
from pathlib import Path
import sqlite3

import data_tools


class DataTools:
    def __init__(self, aliases: dict[str, str]):
        self.aliases = aliases
        self.reads: list[dict] = []

    def source(self, name: str) -> tuple[str, Path]:
        filename = self.aliases.get(name, name)
        path = data_tools.source_path(filename)
        alias = next((alias for alias, value in self.aliases.items() if value == filename), filename)
        return alias, path

    def read_table(self, name, **options):
        alias, path = self.source(name)
        frame = data_tools.read_table(str(path.relative_to(data_tools.ROOT)), **options)
        self.reads.append({"source": alias, "sheet": options.get("sheet"),
                           "table": options.get("table"), "rows": len(frame), "full": True})
        # Retain a bounded audit, including reads from earlier successful actions.
        self.reads = self.reads[-200:]
        return frame

    def inspect_table(self, name, sheet=None, table=None, encoding="utf-8-sig", delimiter=None):
        """Inspect a few raw rows without guessing a header or loading all sheets."""
        alias, path = self.source(name)
        if path.suffix.lower() == ".xlsx":
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                if sheet is None:
                    return {"source": alias, "sheets": book.sheetnames}
                if sheet not in book.sheetnames:
                    raise ValueError(f"Choose a sheet from {book.sheetnames}")
                rows = list(book[sheet].iter_rows(min_row=1, max_row=8, values_only=True))
                return {"source": alias, "sheet": sheet, "sample_rows": rows,
                        "header_candidates": data_tools.header_candidates(rows),
                        "reported_rows": book[sheet].max_row}
            finally:
                book.close()
        if path.suffix.lower() == ".csv":
            with path.open(encoding=encoding, newline="") as stream:
                if delimiter is None:
                    delimiter = csv.Sniffer().sniff(stream.read(8192), delimiters=",;\t|").delimiter
                    stream.seek(0)
                reader = csv.reader(stream, delimiter=delimiter)
                rows = [row for _, row in zip(range(8), reader)]
            return {"source": alias, "sample_rows": rows,
                    "header_candidates": data_tools.header_candidates(rows)}
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
            names = [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if table is None:
                return {"source": alias, "tables": names}
            if table not in names:
                raise ValueError(f"Choose a table from {names}")
            quoted = '"' + table.replace('"', '""') + '"'
            cursor = database.execute(f"SELECT * FROM {quoted} LIMIT 5")
            return {"source": alias, "table": table,
                    "columns": [column[0] for column in cursor.description], "sample_rows": cursor.fetchall()}

    def table_profile(self, name, date_column=None, dayfirst=None, year=None, **options):
        """Report full-table counts and explicit date coverage; no business filters."""
        frame = self.read_table(name, **options)
        profile = {"rows": len(frame), "columns": list(frame.columns),
                   "null_counts": {str(column): int(frame[column].isna().sum()) for column in frame.columns}}
        if date_column is not None:
            parsed = data_tools.parse_dates(frame[date_column], dayfirst=dayfirst, year=year)
            profile["date_min"] = None if parsed.dropna().empty else parsed.min().date().isoformat()
            profile["date_max"] = None if parsed.dropna().empty else parsed.max().date().isoformat()
            profile["missing_dates"] = int(parsed.isna().sum())
        return profile

    def manifest(self):
        files = []
        for alias, name in list(self.aliases.items())[:40]:
            try:
                description = self.inspect_table(alias)
            except Exception as error:
                description = {"source": alias, "inspection_error": str(error)[:500]}
            files.append({"alias": alias, "name": name, **description})
        return {"files": files, "omitted_previews": max(0, len(self.aliases) - len(files)),
                "note": "Previews are incomplete. Read every relevant sheet/table for calculations."}
