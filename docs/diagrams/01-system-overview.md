# The big picture

Think of the agent as an analyst: it receives a question and a rulebook, writes Python to examine the business files, and returns a typed answer. A model decides what code to write; a separate isolated worker runs that code.

```mermaid
flowchart LR
    User["User"] --> UI["Streamlit UI"]
    User --> CLI["CLI: predict or bench"]
    UI -->|"starts CLI subprocess"| CLI
    Split["Dataset split: questions, rulebook, calendar and business files"] --> CLI
    CLI -->|"gather or reuse preview"| Preview["Separate isolated preview worker"]
    Split -->|"read-only business files and calendar"| Preview
    Preview -->|"bounded schema and sample rows"| CLI
    CLI -->|"question and context"| Agent["Single CodeAgent"]
    Agent <-->|"messages, code and observations"| Model["Local model server or OpenAI API"]
    Agent -->|"Python code"| Executor["Isolated executor"]
    Executor <-->|"code and result"| Worker["Bubblewrap worker and data tools"]
    Split -->|"selected business files and calendar, read-only"| Worker
    Agent -->|"validated typed answer"| CLI
    CLI --> Results["Submission, logs, manifest and timings"]
    CLI -->|"bench only, after inference"| Score["Internal scorer"]
    Split -->|"reference answers, bench only"| Score
    Score --> Report["score.json"]
    Results --> UI
    Report --> UI
    Agent -.->|"optional traces"| Phoenix["Local Phoenix"]
    Worker -.->|"optional tool-call traces through executor"| Phoenix
```

The rulebook supplies business meaning, the business README explains its files, and the files contain the records. The question's `as_of_date` supplies the reference date. The hosted API is an optional development provider and requires network access; it does not satisfy an offline-only evaluation profile.

Each question uses one CodeAgent and its own isolated execution worker. Before solving, a separate worker gathers a bounded source preview; unchanged business previews are reused during the run. The single-agent design aims to reduce model calls and latency. Neither the under-10-second target nor real-model accuracy is established.

Source: [agent.py](../../tabular_agent/agent.py), [cli.py](../../tabular_agent/cli.py), [streamlit_app.py](../../streamlit_app.py), [telemetry.py](../../tabular_agent/telemetry.py).
