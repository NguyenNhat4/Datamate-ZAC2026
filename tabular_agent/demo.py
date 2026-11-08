"""Create a small invented dataset and optionally run the local model."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess


def create_demo_split(root):
    """Write a tiny synthetic split for testing a total-sales question.

    Args:
        root: Path to the demo split directory, created when needed.

    Returns:
        Path to the created data/example business directory.

    Raises:
        OSError: A directory or fixture file cannot be written.

    Notes:
        Writes sales.csv, a business README, rulebook, calendar, questions and
        reference answers. Existing fixture files at these paths are overwritten.
    """
    root = Path(root)
    business = root / 'data' / 'example'
    business.mkdir(parents=True, exist_ok=True)
    (business / 'sales.csv').write_text('item,amount\nTea,12\nCoffee,15\n', encoding='utf-8')
    (business / 'README.md').write_text('sales.csv contains one sale per row. amount is VND.\n', encoding='utf-8')
    (root / 'rulebook.md').write_text('Sum the amount column for total sales.\n', encoding='utf-8')
    (root / 'calendar.csv').write_text('date\n', encoding='utf-8')
    question = {'id':'example_total', 'business_id':'example', 'question':'Tổng tiền bán hàng là bao nhiêu?', 'as_of_date':'2026-01-01'}
    (root / 'questions.jsonl').write_text(json.dumps(question, ensure_ascii=False) + '\n', encoding='utf-8')
    (root / 'answers.jsonl').write_text(json.dumps({'id':'example_total', 'type':'number', 'answer':27}) + '\n', encoding='utf-8')
    return business


def main():
    """Create timestamped demo inputs and optionally run the local benchmark.

    Returns:
        Integer exit code 0 after creating inputs under runs/, or the benchmark
        launcher's exit code when process arguments include --run.

    Raises:
        OSError: Fixture creation or the local benchmark launcher fails to start.
        SystemExit: argparse handles help or invalid command-line syntax.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    source = Path('runs') / f'demo-input-{stamp}'
    output = Path('runs') / f'demo-output-{stamp}'
    create_demo_split(source)
    print('Synthetic inputs:', source, flush=True)
    if args.run:
        return subprocess.call(['./scripts/bench-local.sh', '--split', str(source), '--output', str(output), '--limit', '0'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
