"""Load question context without exposing reference answers to inference."""
from dataclasses import dataclass
from pathlib import Path

TABLE_SUFFIXES = {".csv", ".xlsx", ".sqlite", ".sqlite3", ".db"}


@dataclass(frozen=True)
class BusinessContext:
    directory: Path
    calendar: Path
    readme: str
    aliases: dict[str, str]

    @classmethod
    def load(cls, directory: Path, calendar: Path) -> "BusinessContext":
        directory, calendar = directory.resolve(), calendar.resolve()
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if any(path.is_symlink() or path.name == "answers.jsonl" for path in directory.rglob("*")):
            raise ValueError("Business directory contains unsafe links or reference answers")
        names = [str(path.relative_to(directory)) for path in files
                 if path.suffix.lower() in TABLE_SUFFIXES]
        aliases = {f"F{index}": name for index, name in enumerate(names, 1)}
        readme = directory / "README.md"
        return cls(directory, calendar, readme.read_text(encoding="utf-8") if readme.exists() else "", aliases)
