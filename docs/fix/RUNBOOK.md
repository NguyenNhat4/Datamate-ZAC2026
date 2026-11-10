# Runbook: measure a commit on the frozen question sets

**Status.** Written on 10 November 2026. The sources are:

- the commands of the session that ran the 10 November rounds;
- the manifests of those runs;
- `scripts/bench-local.sh` and `tabular_agent/cli.py` at `5e1507b`.

What was tested on 10 November:

- **Worktree.** Set-up (section 3) and clean-up (section 8), run as written on a test worktree at `5e1507b`. Without the exclude entries the links make the tree dirty; with them it is clean, and the code is imported from the worktree. The worktree is removed without `--force`, and the exclude file is restored byte for byte.
- **Set selection.** The id list and flags of section 4 select all 10 sample10 questions, with 6 steps and temperature 0.
- **Reading results.** The outcome comparison (section 5) and the totals-only report (section 6), on existing runs.

- **A full set.** sample10 was run with section 4's commands at `506d567`, whose answering code is unchanged since `5e1507b`. Every model reply matched `runs/sample10-r2s2b`, at all 21 steps.

Why each condition exists is explained in the [unseen businesses record](2026-11-10_unseen_businesses/README.md), decisions 5.1 and 5.3, and in the [rankings record](2026-11-10_rankings_and_patterns/README.md), decision 3.1. Start from [the index](README.md).

## 1. Fixed settings

| Setting | Value | Where it is set |
|---|---|---|
| Model | `local-coder-7b`: Qwen2.5-Coder-7B-Instruct, Q4_K_M, weights sha256 `60e05f21…c2463` | `MODEL_FILE` in `scripts/local-model.sh`, which `serve-local.sh`, `bench-local.sh` and `predict.sh` read |
| Server | llama-server b12015 on `Vulkan1`; all GPU layers; context 16,384; q8_0 KV cache; `--parallel 1`; no context shift; prompt cache on | the same file |
| Port | 18234 | `LOCAL_PORT` |
| Temperature | 0 | CLI default since `82ebdb6`; older commits do not send it |
| DuckDB threads for answering | 1 | in code since `c509cba` |
| Seed | 42 | `--seed` |
| Steps per question | 6 | `--max-steps`; the CLI default since the cleanup (10 November). **Commits before it default to 4**: pass it |
| Questions | all selected | `--limit 0`; the default since the cleanup. **Before it, `bench` kept 20**: pass it |
| Question timeout, reply tokens | 60 s, 2,048 | CLI defaults |
| Split | `data/train` | `--split` |

A valid run's `manifest.json` shows all of the following:

- `dirty: false`
- `agent_mode: "sql"`
- `temperature: 0.0`
- `max_steps: 6`
- `base_url` on port 18234
- `weight_sha256: 60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463`

## 2. Bundles

The current generation is `artifacts/prepared-r2-*`:

| Folder | Businesses | Used by |
|---|---|---|
| `prepared-r2-dev` | biz_104, 106, 107, 111, 112, 120, 121, 127 | sample10, setA, setB, types16 |
| `prepared-r2-unseen` | the other 22 training businesses | unseen48, holdout48 |
| `prepared-r2-public` | the 14 public-test businesses | public predictions |

**`--prepared` takes one folder.** A question whose business has no bundle in that folder is answered `abstain` with the reason "No prepared data for this business." No error is raised (`tabular_agent/cli.py:352`). A sample that spans both training groups, such as the proposed gate100 and work100, needs one folder with all 30 businesses (section 7).

**Re-prepare after any change to the preparation code.** Write each new generation into new folders, run from the code tree being measured. Preparation reads no questions or answers.

```bash
P=".venv-native/bin/python -m tabular_agent.cli prepare"
G=r3   # name of the new generation
nice -n 19 $P --split data/train --output artifacts/prepared-$G-dev \
  --business biz_104,biz_106,biz_107,biz_111,biz_112,biz_120,biz_121,biz_127 > artifacts/.prep-$G-dev.log 2>&1
nice -n 19 $P --split data/train --output artifacts/prepared-$G-unseen \
  --business biz_101,biz_102,biz_103,biz_105,biz_108,biz_109,biz_110,biz_113,biz_114,biz_115,biz_116,biz_117,biz_118,biz_119,biz_122,biz_123,biz_124,biz_125,biz_126,biz_128,biz_129,biz_130 \
  > artifacts/.prep-$G-unseen.log 2>&1
nice -n 19 $P --split data/public_test --output artifacts/prepared-$G-public > artifacts/.prep-$G-public.log 2>&1
```

Before measuring, compare the new generation with the previous one: every relation's columns, row count and content hash, the issues, and the schema text the model sees (unseen record 5.5). The script that did this was not kept (workflow review 3.6).

## 3. Pin the code in a worktree

A set imports the code when it starts. An edit made in the main folder during a long run would leak into the sets that start after it. So every set runs from a worktree checked out at the measured commit. Commit first: the measured commit must hold every change.

```bash
MAIN=$PWD                 # the main folder
C=5e1507b                 # commit to measure
T=/path/outside/the/repo/bench-$C
git worktree add --detach "$T" "$C"
for d in data artifacts runs models .venv-native; do ln -s "$MAIN/$d" "$T/$d"; done
# The links are not directories, so .gitignore's "/data/" rules do not cover them.
# Exclude them for the duration of the round (this file is shared by all worktrees):
printf '/data\n/artifacts\n/runs\n/models\n/.venv-native\n' >> .git/info/exclude
(cd "$T" && git status --porcelain)        # must print nothing
(cd "$T" && .venv-native/bin/python -c "import tabular_agent; print(tabular_agent.__file__)")   # must be inside $T
```

## 4. Run each set on a fresh server

A fresh server per set gives every set the same server state. With the prompt cache on, a reply can otherwise depend on the request before it (unseen record 5.3).

`scripts/bench-local.sh` starts its own server, waits until it is ready, runs `cli bench` with the model and hardware metadata, and stops the server when it exits. One call is therefore one set on a fresh server. It refuses to start if port 18234 is taken. Stop the other server by its PID; never use `pkill -f`, which can match the calling shell. On 10 November the same server flags were started through `serve-local.sh`, and the CLI was called directly with the same arguments.

```bash
cd "$T"
SETS="$MAIN/docs/fix/2026-11-10_unseen_businesses/question_sets.json"
TAG=r3s0                  # one tag per measured commit, recorded in the round's PROGRESS file
for SET in sample10 setA setB types16 unseen48; do
  case $SET in unseen48|holdout48) PREP=artifacts/prepared-r2-unseen ;; *) PREP=artifacts/prepared-r2-dev ;; esac
  IDS=$(.venv-native/bin/python -c "import json,sys; print(' '.join('--question-id ' + i for i in json.load(open(sys.argv[1]))[sys.argv[2]]))" "$SETS" "$SET")
  LOCAL_PORT=18234 ./scripts/bench-local.sh --split data/train --limit 0 --seed 42 --temperature 0 \
    --max-steps 6 --prepared "$PREP" --output "runs/$SET-$TAG" $IDS > "runs/.$SET-$TAG.stdout" 2>&1
  echo "$SET exit $?"
done
```

- **Closed sample.** Run holdout48 the same way, as the last set of the round, and read it only as in section 6.
- **Time.** A 40-question set takes about 5 minutes at the recorded medians of 6–7 s per question, plus the model load.
- **Output folder.** The folder under `runs/` must not exist yet. `runs` is shared with the main folder, so never reuse a name.

## 5. Read an open set

To print the totals, use section 6. To compare per-question outcomes with an earlier run of the same set:

```bash
python3 - runs/unseen48-r2s1 runs/unseen48-r2s2b <<'EOF'
import json, sys
a, b = (json.load(open(f'{d}/score.json')) for d in sys.argv[1:3])
old = {q['id']: q['outcome'] for q in a['questions']}
new = {q['id']: q['outcome'] for q in b['questions']}
changed = [(i, old.get(i), new[i]) for i in new if old.get(i) != new[i]]
for i, o, n in changed: print(f'{i}  {o} -> {n}')
print(f'{len(changed)} of {len(new)} changed; PA {a["penalized_accuracy"]:+.3f} -> {b["penalized_accuracy"]:+.3f}')
EOF
```

This example prints the six wrong-to-correct changes reported in the rankings record. Use it on open sets only: its output names closed-sample questions one by one.

**Where to find each part of a run:**

| Part | Where |
|---|---|
| Question text | `questions.jsonl` |
| Submitted answers | `submission.jsonl` |
| One question's log | `logs/NNNNNN.jsonl`, in the order of `questions.jsonl`. Stages: `schema`, `memory_step` (model reply per step), `sql`, `observation` (result or refusal), `question_timing` |
| Reference answers | `data/train/answers.jsonl`; `score.json` does not hold them |

The record of a round lists, for each open set, the questions that changed and why. It ends with a failure table (see [the index](README.md)).

## 6. Read a closed sample: totals only

Do not open its `questions.jsonl`, `submission.jsonl`, `logs/` or the `questions` list in `score.json`. Print only this:

```bash
python3 - runs/holdout48-r3s0 <<'EOF'
import json, sys
d = sys.argv[1]
s = json.load(open(f'{d}/score.json'))
t = json.load(open(f'{d}/timing-summary.json'))
print(f"{d}: {s['correct']} / {s['wrong']} / {s['abstain']} of {s['total']}, PA {s['penalized_accuracy']:+.3f}; "
      f"median {t['p50_seconds']:.1f} s, p90 {t['p90_seconds']:.1f} s")
EOF
```

Read it once per round, after the round's final commit, and add one to the read count in [the index](README.md). Never choose between two changes by their closed-sample totals.

## 7. A sample over all 30 training businesses

The proposed gate100 and work100 span both training groups. Use one of these:

- **One folder with all 30 businesses.** Prepare the training split into it at the measured code, without `--business`:

  ```bash
  nice -n 19 $P --split data/train --output artifacts/prepared-$G-train > artifacts/.prep-$G-train.log 2>&1
  ```

  Check that its bundles match `-dev` and `-unseen` (section 2) before use.
- **A merged folder.** Link both folders' bundle directories into one folder and copy both folders' `run-*.json` reports there. `find_bundles` reads those reports to find the bundles.

After a run, check that no question abstained for a missing bundle. This prints a count only, so it is also safe for a closed sample:

```bash
grep -c 'No prepared data for this business' runs/<set>-<tag>/submission.jsonl   # must print 0
```

## 8. Clean up after the round

```bash
cd "$MAIN"
for d in data artifacts runs models .venv-native; do rm "${T:?}/${d:?}"; done   # removes the links only; ${…:?} stops if a name is empty
git worktree remove "$T" && git worktree prune
python3 - <<'EOF'
from pathlib import Path
p = Path('.git/info/exclude')
added = {'/data\n', '/artifacts\n', '/runs\n', '/models\n', '/.venv-native\n'}
p.write_text(''.join(l for l in p.read_text().splitlines(keepends=True) if l not in added))
EOF
pgrep -x llama-server || echo 'no model server running'
nvidia-smi --query-compute-apps=pid,process_name --format=csv   # should list no model server
```

Then:

- Add the results row to the round's PROGRESS file, with the run folder names.
- Update "Where we stand" in [the index](README.md).
- Leave the model server off. To use the UI afterwards: `make serve`, then `make ui`.

## 9. Run tags so far

Each run folder is named `<set>-<tag>`. Only the manifest links a tag to its code, so this table lists the tags in use.

| Tag | Commit | Bundles: development sets / unseen sets | Results in |
|---|---|---|---|
| `205dee8` | `205dee8`, temperature 0.8 | – / `prepared-unseen` (unseen48 only) | unseen record 6.1 |
| `t0` | `82ebdb6` | `prepared-fix6` / `prepared-unseen` | unseen record 6.1. The setA run reports a dirty tree; see the note there |
| `t0rep` | `82ebdb6` | repeats of setB and unseen48 | unseen record 5.3 |
| `s1` | `67d4d2c` | `prepared-fix6` / `prepared-unseen` | unseen record 6.2 |
| `s1b` | `2eef9af` | – / `prepared-unseen` (unseen48, holdout48) | unseen record 6.2 |
| `s2` | `f746981` | `prepared-step2-dev` / `prepared-step2-unseen` | unseen record 6.3 |
| `r2s1` | `c509cba` | `prepared-step2-dev` / `prepared-step2-unseen` | rankings record 4 |
| `r2s2` | `a77671d` | `prepared-r2-dev` / `prepared-r2-unseen` | rankings record 4 |
| `r2s2b` | `5e1507b` | `prepared-r2-dev` / `prepared-r2-unseen` | rankings record 4 |
| `e1` | `506d567` | `prepared-r2-dev` (sample10 only). The public dry run `public-dry-e1` was stopped after 49 questions; its run-time bundles are in `artifacts/prepared-dry-e1/prepared` | [entrypoint record](2026-11-10_submission_entrypoint/README.md) |

Older runs (`fix2` to `fix7`, `step1*`, `v2`, `baseline`, `check-*`) ran at the server's default temperature (0.8) on trees with uncommitted changes. Their manifests name the base commit only.

## 10. Traps

1. **Selection limit.** At commits before the cleanup (10 November), `cli bench` keeps only 20 questions unless `--limit 0` is given. Keep passing it, so a command also works on an older worktree.
2. **Step limit.** At commits before the cleanup the default is 4; SQL runs use `--max-steps 6`. Keep passing it.
3. **Missing bundles.** A business missing from the `--prepared` folder abstains silently (section 2).
4. **Old ports and the raw-file agent.** Since the cleanup, the CLI, `make bench` and `serve-local.sh` default to port 18234 (`LOCAL_PORT`) and `make bench` needs `BUNDLES`. Older commits default to port 8000 and answer with the retired raw-file agent when `--prepared` is missing.
5. **Dirty tree.** A run from a dirty tree or from the main folder is not attributable. Check `dirty` in the manifest.
6. **Older commits.** Commits before `82ebdb6` send no temperature, and commits before `c509cba` order tied rows arbitrarily. Neither repeats exactly.
7. **Question order.** Selected questions run in file order, not in the order of the ids given. The order is still fixed, so runs repeat.
8. **Prompt-size failures.** A question over the 16,384-token context fails at the server and is recorded as an abstention. In dev_006 this already happens to 12 public questions.
9. **Preparation output paths.** Preparation refuses an output path that passes through a symbolic link. In a worktree, write bundles under the main folder's `artifacts`, not under the worktree's linked `artifacts`.

## 11. Public-test dry run through the entrypoint

`predict.sh` is what the organizers run. It starts its own server, prepares the split, answers with the settings of section 1, and stops the server; see [the entrypoint record](2026-11-10_submission_entrypoint/README.md). From the pinned worktree:

```bash
cd "$T"
TAG=e1b                   # a name whose run and work folders do not exist yet
nohup env DATA_DIR="$MAIN/data/public_test" RESULT_DIR="$MAIN/runs/public-dry-$TAG" \
  WORK_DIR="$MAIN/artifacts/prepared-dry-$TAG" ./predict.sh > "$MAIN/runs/.public-dry-$TAG.stdout" 2>&1 &
```

- **Ports.** Port 18234 must be free: stop `make serve` first. `predict.sh` refuses an occupied port.
- **Progress.**
  - Phase times: `cat "$MAIN/artifacts/prepared-dry-$TAG/pipeline.log"`.
  - Questions answered so far: `wc -l < "$MAIN/runs/public-dry-$TAG/submission.jsonl"`.
- **Stopping early.** Stop the answering process by its PID, which `pgrep -af 'tabular_agent.cli predict'` lists. `predict.sh` then stops the server itself.
- **Cost.** About 6.5 minutes of preparation, then answering. The answering time is an estimate from the sets' medians (about two hours); it has not been measured yet.
- **Work folder.** Reusing a work folder reuses its bundles, so the preparation time is not measured.
- **What to read.** Read the run as counts only:

  ```bash
  .venv-native/bin/python scripts/dry-run-report.py "runs/public-dry-$TAG" "artifacts/prepared-dry-$TAG" artifacts/prepared-r2-public
  ```

  This prints answered and abstained per business, abstention reasons, errors, timing, and whether the run-time bundles match the last prepared public bundles. It never prints question text or answer values.
- **What not to read.** Do not read public questions or answers to choose fixes; the public test is not a development set.
- **Uploading.** The run's `submission.jsonl` is in the upload format. Uploading it is a team decision. Record each upload in the index's upload log.
