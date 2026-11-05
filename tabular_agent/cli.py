"""Command-line entry point for local inference and scoring."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import sys
import time

from .agent import answer_question
from .contracts import read_jsonl
from .evaluate import score
from .model import create_model, create_openai_model, openai_model_name
from .telemetry import tracing


@contextmanager
def time_limit(seconds):
    def stop(signum, frame):
        raise TimeoutError('Question deadline exceeded')
    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def parser():
    parser = argparse.ArgumentParser(description='Local tabular CodeAgent')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('bench', 'predict', 'doctor'):
        command = commands.add_parser(name)
        command.add_argument('--provider', choices=['local', 'openai'], default='local',
                             help='Model provider: local server (default) or OpenAI API')
        command.add_argument('--backend', choices=['openai', 'ollama'], default=os.getenv('BACKEND', 'openai'))
        command.add_argument('--base-url', help='Local server URL (only with --provider local)')
        command.add_argument('--model', help='Model name; defaults to OPENAI_MODEL for OpenAI or MODEL for local')
        command.add_argument('--split', default='data/train')
        command.add_argument('--output')
        command.add_argument('--limit', type=int, default=20 if name == 'bench' else 0)
        command.add_argument('--seed', type=int, default=42)
        command.add_argument('--business')
        command.add_argument('--question-id', action='append', default=[])
        command.add_argument('--max-steps', type=int, default=8)
        command.add_argument('--max-tokens', type=int, default=2048)
        command.add_argument('--question-timeout', type=float, default=180)
        command.add_argument('--exec-timeout', type=float, default=30)
        command.add_argument('--model-timeout', type=float, default=120)
        command.add_argument('--telemetry', action='store_true', help='Send traces to local Phoenix on port 6006')
        command.add_argument('--run-timeout', type=float, default=21600)
        command.add_argument('--context', type=int, default=16384)
        command.add_argument('--partition', choices=['all', 'dev', 'validation', 'holdout'], default='all')
        command.add_argument('--partition-seed', type=int, default=42)
        command.add_argument('--sample', choices=['random', 'balanced'], default='random')
        for flag in ('weight-sha256', 'gpu-layers', 'cache-type-k', 'cache-type-v'):
            command.add_argument('--' + flag)
    sub = commands.add_parser('score')
    for flag in ('questions', 'predictions', 'references'):
        sub.add_argument('--' + flag, required=True)
    sub.add_argument('--output')
    return parser


def select_questions(questions, args):
    import hashlib
    filtered = []
    for q in questions:
        business = q.get('business_id')
        if not isinstance(business, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', business):
            raise ValueError('Invalid business ID')
        if args.business and business not in args.business.split(','):
            continue
        if args.question_id and q['id'] not in args.question_id:
            continue
        digest = hashlib.sha256(f'{args.partition_seed}:{business}'.encode()).digest()
        n = int.from_bytes(digest[:8], 'big') % 10
        partition = 'dev' if n < 6 else 'validation' if n < 8 else 'holdout'
        if args.partition != 'all' and partition != args.partition:
            continue
        if not isinstance(q.get('question'), str) or not q['question'].strip():
            raise ValueError('Invalid question')
        if date.fromisoformat(q['as_of_date']).isoformat() != q['as_of_date']:
            raise ValueError('Invalid reference date')
        filtered.append(q)
    if not args.limit or args.limit >= len(filtered):
        return filtered
    rng = random.Random(args.seed)
    if args.sample == 'random':
        return rng.sample(filtered, args.limit)
    groups = {}
    for question in filtered:
        groups.setdefault(question['business_id'], []).append(question)
    names = sorted(groups)
    rng.shuffle(names)
    for group in groups.values():
        rng.shuffle(group)
    selected = []
    while len(selected) < args.limit:
        for name in names:
            if groups[name] and len(selected) < args.limit:
                selected.append(groups[name].pop())
    return selected


def run(args):
    if any(not math.isfinite(x) or x <= 0 for x in (args.max_steps, args.max_tokens, args.exec_timeout,
                                                     args.question_timeout, args.model_timeout, args.run_timeout)) or args.limit < 0:
        raise ValueError('Timeouts and step/token limits must be positive; limit cannot be negative')
    if args.provider == 'openai':
        if args.base_url:
            raise ValueError('--base-url is for local servers; OpenAI uses https://api.openai.com/v1')
        model_name = args.model or openai_model_name()
        model = create_openai_model(model_name, max_tokens=args.max_tokens, timeout=args.model_timeout)
    else:
        base = args.base_url or os.getenv('BASE_URL') or ('http://127.0.0.1:11434/v1' if args.backend == 'ollama' else 'http://127.0.0.1:8000/v1')
        local_name = args.model or os.getenv('MODEL')
        if not local_name:
            raise ValueError('Set --model to the local model server alias')
        model = create_model(local_name, base, max_tokens=args.max_tokens, timeout=args.model_timeout, seed=args.seed)
    if args.command == 'doctor':
        print('Configured provider:', 'OpenAI API' if args.provider == 'openai' else f'local ({base})')
        return 0
    split = Path(args.split).resolve()
    for name in ('questions.jsonl', 'rulebook.md', 'calendar.csv'):
        if not (split / name).is_file() or (split / name).is_symlink():
            raise ValueError(f'Missing or unsafe input: {name}')
    questions = select_questions(read_jsonl(split / 'questions.jsonl'), args)
    if not questions:
        raise ValueError('No questions selected')
    if args.command == 'bench' and not (split / 'answers.jsonl').is_file():
        raise ValueError('bench requires answers.jsonl')
    output = Path(args.output or ('runs/' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    if output.is_relative_to(split) or split.is_relative_to(output):
        raise ValueError('Output must be separate from input data')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Output directory must be empty')
    (output / 'logs').mkdir()
    (output / 'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False) + '\n' for q in questions), encoding='utf-8')
    rulebook = (split / 'rulebook.md').read_text(encoding='utf-8')
    predictions = []
    stop_at = time.monotonic() + args.run_timeout
    with (output / 'submission.jsonl').open('w', encoding='utf-8') as submission:
        for index, question in enumerate(questions):
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                print('Run deadline exceeded; completed answers were saved', flush=True)
                break
            business = split / 'data' / question['business_id']
            if not business.is_dir() or business.is_symlink() or any(p.is_symlink() or p.name == 'answers.jsonl' for p in business.rglob('*')):
                raise ValueError('Unsafe or missing business directory')
            with (output / 'logs' / f'{index:06}.jsonl').open('w', encoding='utf-8') as events:
                def emit(event):
                    events.write(json.dumps(event, ensure_ascii=False) + '\n')
                    events.flush()
                try:
                    with time_limit(min(args.question_timeout, remaining)):
                        answer = answer_question(model, question, business, split / 'calendar.csv', rulebook,
                                                 max_steps=args.max_steps, action_seconds=args.exec_timeout, emit=emit)
                except Exception as error:
                    answer = {'type': 'abstain', 'reason': f'{type(error).__name__}: {error}'}
            row = {'id': question['id'], **answer}
            predictions.append(row)
            submission.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            submission.flush()
            os.fsync(submission.fileno())
            print(f"[{index+1}/{len(questions)}] {question['id']}: {row['type']}", flush=True)
    if args.command == 'bench':
        report = score(questions, predictions, read_jsonl(split / 'answers.jsonl'))
        (output / 'score.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Results:', output)
    return 0


def main():
    try:
        args = parser().parse_args()
        if args.command == 'score':
            result = score(read_jsonl(args.questions), read_jsonl(args.predictions), read_jsonl(args.references))
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(text + '\n', encoding='utf-8')
            print(text)
            return 0
        with tracing(args.telemetry):
            return run(args)
    except (ValueError, OSError, RuntimeError) as error:
        print('ERROR:', error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
