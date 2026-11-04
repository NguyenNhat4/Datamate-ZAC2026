"""Native runner: select inputs, enforce budgets, flush answers, then score."""
import argparse
import datetime
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import time

from .contracts import read_jsonl
from .data import BusinessContext
from .evaluate import score
from .executor import IsolatedExecutor
from .model import LocalCodeModel
from .agent import NativeAgent
from .runtime import Deadline, QuestionStopped, enforce_deadline
from .splits import select_questions
from .trace import write_question_trace, write_trace_index


def load_env(path=Path(".env")):
    """Read simple local settings without executing shell code or printing keys."""
    if not path.exists():
        return
    allowed = {"BACKEND", "BASE_URL", "MODEL", "LOCAL_MODEL_API_KEY", "OPENAI_API_KEY"}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name.strip() not in allowed:
            raise ValueError(f"Unsupported .env setting on line {number}")
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError(f"Invalid .env value on line {number}")
        os.environ.setdefault(name.strip(), parts[0] if parts else "")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def source_digest():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def safe_sources(split, questions):
    for required in ("questions.jsonl", "calendar.csv", "rulebook.md"):
        path = split / required
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked input: {required}")
    businesses = {}
    for question in questions:
        name = question.get("business_id")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Invalid business_id path")
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise ValueError("Missing question text")
        date = datetime.date.fromisoformat(question["as_of_date"])
        if date.isoformat() != question["as_of_date"]:
            raise ValueError("as_of_date must be YYYY-MM-DD")
        directory = split / "data" / name
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError(f"Missing or unsafe business directory: {name}")
        BusinessContext.load(directory, split / "calendar.csv")
        businesses[name] = directory
    return businesses


def create_model(args):
    if args.backend not in {"openai", "ollama"}:
        raise ValueError("Native inference uses a local server; set BACKEND=openai or ollama")
    endpoint = args.base_url or ("http://127.0.0.1:11434/v1" if args.backend == "ollama"
                                 else "http://127.0.0.1:8000/v1")
    return LocalCodeModel(args.model or None, endpoint, seed=args.seed,
                          max_tokens=args.max_tokens, context=args.context, timeout=args.model_timeout)


def run(args, model_factory=create_model, agent_factory=NativeAgent):
    started = time.monotonic()
    # Leave time to close files and write the final report after inference stops.
    run_deadline = Deadline.after(args.run_timeout - 2)
    split = Path(args.split).resolve()
    questions = select_questions(
        read_jsonl(split / "questions.jsonl"), partition=args.partition,
        partition_seed=args.partition_seed, limit=args.limit, seed=args.seed, sample=args.sample,
        businesses=set(args.business.split(",")) if args.business else None,
    )
    if not questions:
        raise ValueError("No questions selected")
    businesses = safe_sources(split, questions)
    if args.command == "bench" and not (split / "answers.jsonl").is_file():
        raise ValueError("bench requires references; use predict for unlabeled inputs")
    default = Path("runs") / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = Path(args.output or default).resolve()
    if output.is_relative_to(split) or split.is_relative_to(output):
        raise ValueError("Outputs must be separate from source data")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output directory must be empty; existing runs are never overwritten")
    with (output / ".run-lock").open("x") as lock:
        lock.write(str(os.getpid()))
    logs = output / "logs"
    logs.mkdir()
    (output / "submission.jsonl").touch()
    (output / "questions.jsonl").write_text(
        "".join(json.dumps(question, ensure_ascii=False) + "\n" for question in questions), encoding="utf-8",
    )
    rulebook = (split / "rulebook.md").read_text(encoding="utf-8")
    config = {
        "arguments": vars(args), "engine": "native-codeagent", "python": sys.version,
        "agent_source_sha256": source_digest(),
        "rulebook_sha256": hashlib.sha256(rulebook.encode()).hexdigest(),
        "selected_questions_sha256": hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest(),
        "dependencies": {name: importlib.metadata.version(name)
                         for name in ("smolagents", "openai", "pandas", "openpyxl", "pydantic")},
        "eligibility": "Model licence/release/parameters and reference hardware are unverified",
    }
    write_json(output / "config.json", config)
    predictions, timings = [], []
    status = "completed"
    model = None
    print(f"Run: {output}\nEngine: native CodeAgent\nQuestions: {len(questions)}", flush=True)
    try:
        with enforce_deadline(run_deadline):
            model = model_factory(args)
            config["model"] = model.check()
            write_json(output / "config.json", config)
            agent = agent_factory(model, max_steps=args.max_steps, action_seconds=args.exec_timeout)
            with (output / "submission.jsonl").open("w", encoding="utf-8") as submission:
                for index, question in enumerate(questions):
                    agent.question_seconds = min(args.question_timeout, run_deadline.remaining())
                    with (logs / f"{index:06d}.events.jsonl").open("w", encoding="utf-8") as events:
                        def save_event(event):
                            events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                            events.flush()

                        answer, trace = agent.solve(question, businesses[question["business_id"]],
                                                    split / "calendar.csv", rulebook, emit=save_event)
                    row = {"id": question["id"], **answer}
                    submission.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                    submission.flush()
                    os.fsync(submission.fileno())
                    predictions.append(row)
                    timings.append(trace["elapsed_seconds"])
                    write_json(logs / f"{index:06d}.json", {"id": question["id"], "final_answer": row, **trace})
                    write_question_trace(logs / f"{index:06d}.html", question["id"], row, trace, question=question)
                    print(f'[{index + 1}/{len(questions)}] {question["id"]}: {answer["type"]} ({timings[-1]:.1f}s)', flush=True)
    except QuestionStopped:
        status = "run_timeout"
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception as error:
        status = "failed"
        config["failure"] = f"{type(error).__name__}: {error}"
        write_json(output / "config.json", config)
    finally:
        if model is not None and hasattr(model, "client"):
            config.setdefault("model", {})["context_counting"] = getattr(model, "context_counting", "unavailable")
            try:
                write_json(output / "config.json", config)
            finally:
                model.client.close()
    report = {
        "status": status, "selected": len(questions), "written": len(predictions),
        "total_wall_seconds": round(time.monotonic() - started, 3),
        "median_question_seconds": statistics.median(timings) if timings else None,
        "max_question_seconds": max(timings) if timings else None,
        "measurement_scope": "Local CLI wall time; excludes a server started before the CLI",
    }
    if args.command == "bench":
        # A separate process opens references after inference has stopped.
        scored = subprocess.run([
            sys.executable, "-m", "tabular_agent.runner", "score",
            "--questions", str(output / "questions.jsonl"),
            "--predictions", str(output / "submission.jsonl"),
            "--references", str(split / "answers.jsonl"), "--output", str(output / "score.json"),
        ], capture_output=True, text=True, timeout=120)
        if scored.returncode == 0:
            report.update(json.loads((output / "score.json").read_text(encoding="utf-8")))
        else:
            status = report["status"] = "scoring_failed"
            report["scoring_error"] = scored.stderr[-2000:]
    write_json(output / "report.json", report)
    write_trace_index(output, predictions)
    # Render references only after inference, in a separate reporting process.
    render_command = [sys.executable, "-m", "tabular_agent.trace", str(output)]
    if args.command == "bench":
        render_command += ["--references", str(split / "answers.jsonl")]
    rendered = subprocess.run(render_command, capture_output=True, text=True, timeout=120)
    if rendered.returncode:
        report["trace_render_error"] = rendered.stderr[-2000:]
        write_json(output / "report.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "questions"}, indent=2))
    print(f"Trace: {output / 'trace.html'}", flush=True)
    return 0 if status == "completed" else 130 if status == "interrupted" else 2


def parser():
    result = argparse.ArgumentParser(description="Native tabular CodeAgent with isolated Python execution")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("bench", "predict", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--backend", choices=["openai", "ollama"], default=os.environ.get("BACKEND", "openai"))
        command.add_argument("--base-url", default=os.environ.get("BASE_URL"))
        command.add_argument("--model", default=os.environ.get("MODEL"))
        command.add_argument("--split", default="data/train")
        command.add_argument("--output")
        command.add_argument("--business")
        command.add_argument("--limit", type=int, default=20 if name == "bench" else 0)
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--partition", choices=["all", "dev", "validation", "holdout"], default="all")
        command.add_argument("--partition-seed", type=int, default=42)
        command.add_argument("--sample", choices=["random", "balanced"], default="random")
        command.add_argument("--max-steps", type=int, default=8)
        command.add_argument("--max-tokens", type=int, default=2048)
        command.add_argument("--context", type=int, default=32768)
        command.add_argument("--question-timeout", type=float, default=180)
        command.add_argument("--exec-timeout", type=float, default=30)
        command.add_argument("--model-timeout", type=float, default=120)
        command.add_argument("--run-timeout", type=float, default=21600)
        for flag in ("weight-sha256", "gpu-layers", "cache-type-k", "cache-type-v"):
            command.add_argument("--" + flag)
    command = commands.add_parser("score")
    for flag in ("questions", "predictions", "references"):
        command.add_argument("--" + flag, required=True)
    command.add_argument("--output")
    return result


def main():
    try:
        load_env()
        args = parser().parse_args()
        if args.command == "score":
            report = score(read_jsonl(args.questions), read_jsonl(args.predictions), read_jsonl(args.references))
            if args.output:
                write_json(Path(args.output), report)
            print(json.dumps({key: value for key, value in report.items() if key != "questions"}, indent=2))
            return 0
        for name in ("max_steps", "max_tokens", "context", "question_timeout", "exec_timeout", "model_timeout", "run_timeout"):
            value = getattr(args, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if args.run_timeout <= 2 or args.limit < 0:
            raise ValueError("run-timeout must exceed 2 seconds and limit must be nonnegative")
        if args.weight_sha256 and not re.fullmatch(r"[0-9a-f]{64}", args.weight_sha256):
            raise ValueError("weight-sha256 must be a lowercase SHA-256 digest")
        if args.command == "doctor":
            model = create_model(args)
            try:
                print(json.dumps(model.check(), indent=2))
            finally:
                model.client.close()
            print("Model discovery passed. Run make test-sandbox to check worker isolation.")
            return 0
        return run(args)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
