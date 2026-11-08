# Runs, outputs and scoring

```mermaid
flowchart TD
    Start["UI selection or CLI command"] --> Select["Load and select questions"]
    Select --> SaveQ["Save selected questions.jsonl"]
    SaveQ --> Loop["Process each question"]
    Loop --> Answer["Answer or abstain on exception"]
    Answer --> Flush["Append and flush submission row"]
    Answer --> Log["Write per-question action log"]
    Flush --> More{"More questions before run deadline?"}
    More -->|"yes"| Loop
    More -->|"no"| Mode{"Run mode"}
    Mode -->|"predict"| Done["Run directory"]
    Mode -->|"bench"| Compare["Compare predictions with reference answers"]
    Ref["answers.jsonl"] --> Compare
    Compare --> Score["Save internal score.json"]
    Score --> Done
    SaveQ --> Done
    Flush --> Done
    Log --> Done
```

A run directory contains the selected `questions.jsonl`, `submission.jsonl`, and `logs/`; a benchmark also produces `score.json`. Results are written as questions finish, so an interrupted run can contain only a subset. In benchmark mode, reference answers are loaded for scoring after inference, rather than included in the agent's task context. The UI chooses benchmark mode when the split has `answers.jsonl`, otherwise prediction mode.

The scorer is an internal approximation. Its result does not establish an official score or real-model answer quality. Optional Phoenix tracing is separate from the saved run files and may contain business information.

Source: [cli.py](../../tabular_agent/cli.py), [evaluate.py](../../tabular_agent/evaluate.py), [streamlit_app.py](../../streamlit_app.py).
