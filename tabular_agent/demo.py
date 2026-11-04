"""Create an invented cross-month sales example and optionally run the new agent."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess


def create_demo_split(root: Path):
    import openpyxl
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Demo input directory must be empty")
    business = root / "data" / "shop"
    business.mkdir(parents=True)
    (root / "calendar.csv").write_text("date,lunar_day,lunar_month,lunar_year\n", encoding="utf-8")
    (root / "rulebook.md").write_text(
        "Resolve relative time against as_of_date. Last week is the previous complete Monday-Sunday week.\n"
        "Best selling means quantity in base units, excluding entire cancelled and returned orders.\n"
        "Use the conversion table; group by product. Ties sort by product name ascending.\n"
        "An unsupported calculation is unanswerable only after inspecting the relevant full sources.\n",
        encoding="utf-8",
    )
    (business / "README.md").write_text(
        "This is an invented sales example. sales.xlsx has one sheet per month. Each row is a distinct order.\n"
        "Dates use YYYY-MM-DD. status is completed, returned, or cancelled.\n"
        "quantity uses the row's unit. conversions.csv gives base bottles per box for each product.\n"
        "A bottle has factor 1. There is no purchase cost or profit data.\n", encoding="utf-8",
    )
    (business / "conversions.csv").write_text(
        "product,unit,factor\nTrà đóng chai,box,12\nNước cam,box,12\nNước suối,box,24\n", encoding="utf-8-sig",
    )
    book = openpyxl.Workbook()
    february = book.active
    february.title = "2026-02"
    march = book.create_sheet("2026-03")
    header = ["order", "date", "product", "quantity", "unit", "status"]
    february.append(header)
    march.append(header)
    february.append(["A", "2026-02-24", "Trà đóng chai", 2, "box", "completed"])
    february.append(["B", "2026-02-25", "Nước cam", 40, "bottle", "returned"])
    february.append(["C", "2026-02-26", "Nước suối", 60, "bottle", "cancelled"])
    march.append(["D", "2026-03-01", "Trà đóng chai", 3, "bottle", "completed"])
    book.save(business / "sales.xlsx")
    questions = [
        {"id": "example_top", "business_id": "shop", "question": "Tuần rồi mặt hàng nào bán chạy nhất?", "as_of_date": "2026-03-04"},
        {"id": "example_quantity", "business_id": "shop", "question": "Tuần rồi bán được bao nhiêu chai Trà đóng chai?", "as_of_date": "2026-03-04"},
        {"id": "example_missing", "business_id": "shop", "question": "Lãi gộp tuần rồi là bao nhiêu?", "as_of_date": "2026-03-04"},
    ]
    references = [
        {"id": "example_top", "type": "string", "answer": "Trà đóng chai"},
        {"id": "example_quantity", "type": "number", "answer": 27},
        {"id": "example_missing", "type": "unanswerable"},
    ]
    for name, values in (("questions.jsonl", questions), ("answers.jsonl", references)):
        (root / name).write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")
    return business


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start the provisioned local server and run the native agent")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    source = Path("runs") / f"demo-input-{stamp}"
    output = Path("runs") / f"demo-output-{stamp}"
    create_demo_split(source)
    print(f"Synthetic inputs: {source}", flush=True)
    if args.run:
        return subprocess.call([
            "./scripts/bench-local.sh", "--split", str(source), "--output", str(output),
            "--limit", "0", "--max-steps", "8", "--question-timeout", "180",
        ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
