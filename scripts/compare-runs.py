"""Compare completed local runs only when question set and rules are identical."""
import json
from pathlib import Path
import sys


def load(path):
    root = Path(path)
    return (json.loads((root / 'config.json').read_text()),
            json.loads((root / 'report.json').read_text()))


def main(paths):
    if len(paths) < 2:
        raise ValueError('Usage: compare-runs.py RUN_A RUN_B [RUN_C ...]')
    runs = [load(path) for path in paths]
    baseline = runs[0][0]
    for config, report in runs:
        for key in ('selected_questions_sha256', 'rulebook_sha256', 'agent_source_sha256', 'dependencies', 'python'):
            if baseline[key] != config[key]:
                raise ValueError(f'Runs have different {key}; comparison would be misleading')
        for key in ('schema_mode', 'verification', 'max_steps', 'max_executions', 'question_timeout',
                    'exec_timeout', 'model_timeout', 'max_tokens', 'context', 'seed', 'abstention_policy',
                    'cache_type_k', 'cache_type_v'):
            if baseline['arguments'][key] != config['arguments'][key]:
                raise ValueError(f'Runs have different {key}; comparison would be misleading')
        if report['status'] != 'completed':
            raise ValueError('All runs must be complete')
    for path, (config, report) in zip(paths, runs):
        row = {'run': path, 'model': config['model']['model'],
               **{k: report[k] for k in ('total', 'correct', 'wrong', 'abstain', 'pa', 'coverage',
                                          'answered_accuracy', 'total_wall_seconds')}}
        print(json.dumps(row, ensure_ascii=False))
    print('Internal evaluator; compare latency only on comparable hardware and runtime settings.')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit(f'ERROR: {exc}')
