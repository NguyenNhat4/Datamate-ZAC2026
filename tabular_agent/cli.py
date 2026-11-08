"""Command-line entry point for local inference and scoring."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time

from .agent import answer_question, source_preview
from .contracts import read_jsonl
from .evaluate import score
from .model import create_model, create_openai_model, openai_model_name
from .telemetry import tracing


class QuestionDeadlineExceeded(BaseException):
    """Deadline signal that agent-level Exception handlers cannot swallow."""


@contextmanager
def time_limit(seconds):
    def stop(signum, frame):
        raise QuestionDeadlineExceeded('Question deadline exceeded')
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
        command.add_argument('--max-steps', type=int, default=4)
        command.add_argument('--max-tokens', type=int, default=2048)
        command.add_argument('--question-timeout', type=float, default=60)
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
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, check=True).stdout)
    except (OSError, subprocess.SubprocessError):
        revision = None
        dirty = None
    dependencies = {}
    for name in ('smolagents', 'pandas', 'openpyxl', 'openai', 'httpx'):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    manifest = {'revision': revision, 'python': sys.version.split()[0], 'model': model_name if args.provider == 'openai' else local_name,
                'dirty': dirty, 'dependencies': dependencies, 'provider': args.provider,
                'base_url': base if args.provider == 'local' else None, 'agent_mode': 'single', 'seed': args.seed,
                'partition': args.partition, 'partition_seed': args.partition_seed, 'sample': args.sample,
                'max_steps': args.max_steps, 'max_tokens': args.max_tokens, 'question_timeout': args.question_timeout,
                'weight_sha256': args.weight_sha256, 'gpu_layers': args.gpu_layers, 'context': args.context,
                'cache_type_k': args.cache_type_k, 'cache_type_v': args.cache_type_v}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    rulebook = (split / 'rulebook.md').read_text(encoding='utf-8')
    predictions = []
    timings = []
    previews = {}
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
                started = time.monotonic()
                measurements = {'model_calls': 0, 'model_seconds': 0.0, 'worker_start_seconds': 0.0,
                                'execution_seconds': 0.0, 'file_read_seconds': 0.0, 'cache_hits': 0,
                                'errors': [], 'input_tokens': 0, 'output_tokens': 0, 'retries': 0}
                pending_repair = [False]
                def emit(event):
                    if event.get('stage') == 'timing':
                        operation = event.get('operation')
                        if operation in ('worker_start', 'execution'):
                            measurements[operation + '_seconds'] += event.get('seconds', 0)
                        for tool in event.get('tools', []):
                            if tool.get('tool') in ('read_table', 'read_workbook'):
                                measurements['file_read_seconds'] += tool.get('seconds', 0)
                                measurements['cache_hits'] += int(tool.get('cache_hit', False))
                    if event.get('stage') == 'memory_step':
                        if event.get('error'):
                            measurements['errors'].append(event['error'].get('type', 'error'))
                            pending_repair[0] = True
                        usage = event.get('token_usage') or {}
                        for key in ('input_tokens', 'output_tokens'):
                            measurements[key] += usage.get(key) or 0
                    events.write(json.dumps(event, ensure_ascii=False) + '\n')
                    events.flush()
                original_generate = getattr(model, 'generate', None)
                def timed_generate(*call_args, **call_kwargs):
                    model_start = time.monotonic()
                    if pending_repair[0]:
                        measurements['retries'] += 1
                        pending_repair[0] = False
                    measurements['model_calls'] += 1
                    try:
                        return original_generate(*call_args, **call_kwargs)
                    except Exception as error:
                        cause = error.__cause__
                        measurements['errors'].append(type(cause or error).__name__)
                        raise
                    finally:
                        measurements['model_seconds'] += time.monotonic() - model_start
                if original_generate is not None:
                    model.generate = timed_generate
                try:
                    budget = min(args.question_timeout, remaining, 60)
                    with time_limit(budget):
                        preview_start = time.monotonic()
                        files = sorted(p for p in business.rglob('*') if p.is_file())
                        cache_key = (str(business), tuple((str(p.relative_to(business)), p.stat().st_size,
                                                           p.stat().st_mtime_ns) for p in files))
                        cache_hit = cache_key in previews
                        if not cache_hit:
                            previews[cache_key] = source_preview(business, split / 'calendar.csv', min(args.exec_timeout, budget), preview_rows=4)
                        measurements['preview_seconds'] = time.monotonic() - preview_start
                        measurements['preview_cache_hit'] = cache_hit
                        emit({'stage': 'source_preview', 'text': previews[cache_key]})
                        answer = answer_question(model, question, business, split / 'calendar.csv', rulebook,
                                                 max_steps=min(args.max_steps, 4),
                                                 action_seconds=min(args.exec_timeout, budget), emit=emit,
                                                 source_summary=previews[cache_key])
                except QuestionDeadlineExceeded:
                    answer = {'type': 'abstain', 'reason': 'Question deadline exceeded'}
                except Exception as error:
                    answer = {'type': 'abstain', 'reason': f'{type(error).__name__}: {error}'}
                finally:
                    if original_generate is not None:
                        model.generate = original_generate
                measurements.update({'id': question['id'], 'seconds': time.monotonic() - started,
                                     'answer_type': answer['type']})
                timings.append(measurements)
                emit({'stage': 'question_timing', **measurements})
            row = {'id': question['id'], **answer}
            predictions.append(row)
            submission.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            submission.flush()
            os.fsync(submission.fileno())
            print(f"[{index+1}/{len(questions)}] {question['id']}: {row['type']}", flush=True)
    if args.command == 'bench':
        report = score(questions, predictions, read_jsonl(split / 'answers.jsonl'))
        (output / 'score.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'timings.json').write_text(json.dumps(timings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if timings:
        durations = sorted(item['seconds'] for item in timings)
        def percentile(p):
            return durations[max(0, math.ceil(p * len(durations)) - 1)]
        answered = [item for item in timings if item['answer_type'] != 'abstain']
        abstained = [item for item in timings if item['answer_type'] == 'abstain']
        summary = {'count': len(timings), 'p50_seconds': percentile(.50), 'p90_seconds': percentile(.90),
                   'p95_seconds': percentile(.95), 'under_10_seconds': sum(x < 10 for x in durations) / len(durations),
                   'answered_under_10_seconds': sum(x['seconds'] < 10 for x in answered) / len(answered) if answered else None,
                   'abstained_under_10_seconds': sum(x['seconds'] < 10 for x in abstained) / len(abstained) if abstained else None}
        (output / 'timing-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
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
