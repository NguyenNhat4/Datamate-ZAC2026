"""CLI entry point. References are opened only after inference has finished."""
import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from .agent import Agent
from .contracts import read_jsonl
from .evaluate import score
from .model import LocalModel
from .sandbox import Sandbox
from .splits import select_questions
from .diagnostics import trace_signals
from .calibration import check_compatible, fit_policy, load_run, permits


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def source_digest():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode() + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def safe_sources(split, questions):
    split = split.resolve()
    for required in ('rulebook.md', 'calendar.csv', 'questions.jsonl'):
        p = split / required
        if not p.is_file() or p.is_symlink():
            raise ValueError(f'Missing or symlinked input: {required}')
    businesses = {}
    for q in questions:
        name = q.get('business_id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid business_id path')
        if not isinstance(q.get('question'), str):
            raise ValueError('Missing question text')
        datetime.date.fromisoformat(q['as_of_date'])
        path = split / 'data' / name
        if not path.is_dir() or path.resolve() != path or any(p.is_symlink() for p in path.rglob('*')):
            raise ValueError(f'Missing/unsafe business directory: {name}')
        if any(p.name == 'answers.jsonl' for p in path.rglob('*')):
            raise ValueError('Reference answer files must not be in a business directory')
        businesses[name] = path
    return businesses


def run(args):
    start = time.monotonic()
    split = Path(args.split).resolve()
    questions = read_jsonl(split / 'questions.jsonl')
    questions = select_questions(questions, partition=args.partition, partition_seed=args.partition_seed,
                                 businesses=set(args.business.split(',')) if args.business else None,
                                 limit=args.limit, seed=args.seed, sample=args.sample)
    if not questions:
        raise ValueError('No questions selected')
    businesses = safe_sources(split, questions)
    if args.command == 'bench' and not (split / 'answers.jsonl').is_file():
        raise ValueError('bench requires training references; use predict for unlabeled splits')
    sandbox = Sandbox(args.exec_timeout)
    sandbox.check()
    model = LocalModel(args.backend, args.base_url, args.model or None, args.model_timeout, args.seed,
                       args.max_tokens, args.context)
    metadata = model.check()
    metadata['weight_sha256_provided'] = args.weight_sha256
    metadata['gpu_layers_provided'] = args.gpu_layers
    metadata['cache_types_provided'] = {'key': args.cache_type_k, 'value': args.cache_type_v}
    rulebook = (split / 'rulebook.md').read_text(encoding='utf-8')
    configuration = {'arguments': vars(args), 'model': metadata,
                     'rulebook_sha256': hashlib.sha256(rulebook.encode()).hexdigest(),
                     'agent_source_sha256': source_digest(),
                     'selected_questions_sha256': hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest(),
                     'python': sys.version,
                     'dependencies': {p: importlib.metadata.version(p) for p in ('pandas', 'numpy', 'openpyxl')},
                     'eligibility': 'Model licence/release/parameter budget and reference hardware not certified'}
    policy = None
    if args.abstention_policy:
        policy = json.loads(Path(args.abstention_policy).read_text(encoding='utf-8'))
        check_compatible(policy, configuration)
    default = Path('runs') / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output = Path(args.output or default).resolve()
    if output.is_relative_to(split) or split.is_relative_to(output):
        raise ValueError('Output directory must be separate from source data')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Output directory must be empty; existing runs are never overwritten')
    # Exclusive lock prevents concurrent runs from sharing an output directory.
    with (output / '.run-lock').open('x') as lock:
        lock.write(str(os.getpid()))
    logs = output / 'logs'
    logs.mkdir()
    print(f'Run: {output}\nModel: {model.model}\nQuestions: {len(questions)}', flush=True)
    write_json(output / 'config.json', configuration)
    with (output / 'questions.jsonl').open('w', encoding='utf-8') as f:
        for q in questions:
            f.write(json.dumps(q, ensure_ascii=False) + '\n')
    agent = Agent(model, sandbox, args.max_steps, args.max_executions, args.question_timeout,
                  args.schema_mode, args.verification)
    predictions, timings, signals_by_id = [], [], {}
    status = 'completed'
    try:
        with (output / 'submission.jsonl').open('w', encoding='utf-8') as stream:
            for i, question in enumerate(questions):
                remaining = args.run_timeout - (time.monotonic() - start)
                if remaining <= 0:
                    status = 'run_timeout'
                    break
                agent.question_seconds = min(args.question_timeout, remaining)
                try:
                    answer, trace = agent.solve(question, businesses[question['business_id']], split / 'calendar.csv', rulebook)
                except Exception as exc:
                    answer = {'type': 'abstain', 'reason': 'Agent execution failed'}
                    trace = {'error': f'{type(exc).__name__}: {exc}', 'elapsed_seconds': 0}
                signals_by_id[question['id']] = trace_signals(trace, answer)
                if policy and answer['type'] != 'abstain' and not permits(policy['policy'], trace, signals_by_id[question['id']]):
                    trace['answer_before_calibration'] = answer
                    answer = {'type': 'abstain', 'reason': 'Calibrated abstention policy'}
                row = {'id': question['id'], **answer}
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
                predictions.append(row)
                timings.append(trace['elapsed_seconds'])
                write_json(logs / f'{i:06d}.json', {'id': question['id'], 'final_answer': row,
                           'diagnostic_signals': signals_by_id[question['id']], **trace})
                print(f'[{i + 1}/{len(questions)}] {question["id"]}: {answer["type"]} ({trace["elapsed_seconds"]:.1f}s)', flush=True)
    except KeyboardInterrupt:
        status = 'interrupted'
    report = {'status': status, 'selected': len(questions), 'written': len(predictions),
              'total_wall_seconds': round(time.monotonic() - start, 3),
              'median_question_seconds': statistics.median(timings) if timings else None,
              'max_question_seconds': max(timings) if timings else None,
              'measurement_scope': 'local client run including preflight and lazy model loading; excludes prior server startup'}
    if args.command == 'bench':
        # Never pass references into model, tools, or execution sandbox.
        report.update(score(questions, predictions, read_jsonl(split / 'answers.jsonl')))
        by_outcome = defaultdict(Counter)
        for detail in report['questions']:
            for signal in signals_by_id.get(detail['id'], []):
                by_outcome[detail['outcome']][signal] += 1
        report['diagnostic_signals_by_outcome'] = {outcome: dict(counts) for outcome, counts in by_outcome.items()}
    write_json(output / 'report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in {'questions', 'by_type', 'by_business'}}, indent=2))
    print(f'Report: {output / "report.json"}')
    return 130 if status == 'interrupted' else (2 if status != 'completed' else 0)


def parser():
    p = argparse.ArgumentParser(description='Local ReAct Python analysis agent')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('bench', 'predict', 'doctor'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--backend', choices=['ollama', 'openai'], default=os.getenv('BACKEND', 'ollama'))
        cmd.add_argument('--base-url', default=os.getenv('BASE_URL') or None)
        cmd.add_argument('--model', default=os.getenv('MODEL') or None)
        cmd.add_argument('--weight-sha256', help='Weight digest provided by the model launcher')
        cmd.add_argument('--gpu-layers', help='GPU layer count provided by the model launcher')
        cmd.add_argument('--cache-type-k', help='Key cache type provided by the model launcher')
        cmd.add_argument('--cache-type-v', help='Value cache type provided by the model launcher')
        cmd.add_argument('--split', default='data/train')
        cmd.add_argument('--output')
        cmd.add_argument('--business', help='Comma-separated business IDs for routing/filtering only')
        cmd.add_argument('--limit', type=int, default=20 if name == 'bench' else 0, help='0 means all; seeded sample otherwise')
        cmd.add_argument('--seed', type=int, default=42)
        cmd.add_argument('--partition', choices=['all', 'dev', 'validation', 'holdout'], default='all',
                         help='business-disjoint evaluation partition; all preserves original behavior')
        cmd.add_argument('--partition-seed', type=int, default=42)
        cmd.add_argument('--sample', choices=['random', 'balanced'], default='random',
                         help='balanced samples across businesses without using references')
        cmd.add_argument('--max-steps', type=int, default=12)
        cmd.add_argument('--max-executions', type=int, default=5)
        cmd.add_argument('--question-timeout', type=float, default=240)
        cmd.add_argument('--exec-timeout', type=float, default=40)
        cmd.add_argument('--model-timeout', type=float, default=120)
        cmd.add_argument('--run-timeout', type=float, default=21600)
        cmd.add_argument('--max-tokens', type=int, default=2048)
        cmd.add_argument('--context', type=int, default=32768)
        cmd.add_argument('--schema-mode', choices=['full', 'compact'], default='full',
                         help='compact reduces schema samples; the full rulebook is always retained')
        cmd.add_argument('--verification', choices=['review', 'high-risk', 'all'], default='high-risk',
                         help='independent recomputation policy; review always checks model-reported evidence')
        cmd.add_argument('--abstention-policy', help='Apply a compatible policy fitted on validation traces')
    ev = sub.add_parser('score')
    ev.add_argument('--questions', required=True)
    ev.add_argument('--predictions', required=True)
    ev.add_argument('--references', required=True)
    ev.add_argument('--output')
    cal = sub.add_parser('calibrate')
    cal.add_argument('--run', required=True, help='Completed training-validation benchmark directory')
    cal.add_argument('--output', required=True, help='New policy JSON file; never overwritten')
    cal.add_argument('--min-cases', type=int, default=20)
    cal.add_argument('--min-answered', type=int, default=10)
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == 'calibrate':
            policy = fit_policy(*load_run(args.run), min_cases=args.min_cases, min_answered=args.min_answered)
            with Path(args.output).open('x', encoding='utf-8') as stream:
                json.dump(policy, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
            print(json.dumps({k: policy[k] for k in ('policy', 'validation_count', 'validation_answered', 'candidates')}, indent=2))
            print(f'Policy: {args.output}')
            return 0
        if args.command == 'score':
            report = score(read_jsonl(args.questions), read_jsonl(args.predictions), read_jsonl(args.references))
            if args.output:
                write_json(Path(args.output), report)
            print(json.dumps({k: v for k, v in report.items() if k != 'questions'}, indent=2))
            return 0
        for name in ('max_steps', 'max_executions', 'question_timeout', 'exec_timeout', 'model_timeout', 'run_timeout', 'max_tokens', 'context'):
            if getattr(args, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if args.limit < 0:
            raise ValueError('limit cannot be negative')
        if args.weight_sha256 and not re.fullmatch(r'[0-9a-f]{64}', args.weight_sha256):
            raise ValueError('weight-sha256 must be a lowercase SHA-256 digest')
        if args.command == 'doctor':
            Sandbox(args.exec_timeout).check()
            model = LocalModel(args.backend, args.base_url, args.model)
            print(json.dumps(model.check(), indent=2))
            print('Sandbox and model discovery passed; generation and answer quality remain untested.')
            return 0
        return run(args)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
