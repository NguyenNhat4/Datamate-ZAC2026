# What happens to one question

This sequence shows the single CodeAgent used for every question. Source discovery uses a separate preview worker; its state is not reused by the execution worker.

```mermaid
sequenceDiagram
    participant R as Runner
    participant P as Separate preview worker
    participant A as CodeAgent
    participant M as Model
    participant E as Isolated executor
    participant W as Bubblewrap worker
    participant D as Business files and calendar
    alt No cached preview for unchanged business files
        R->>P: Gather bounded source preview
        P->>D: Inspect files, sheets and tables
        D-->>P: Metadata and sample rows
        P-->>R: Bounded preview
        Note over R,P: Cache preview text and clean up preview worker
    else Cached preview available
        Note over R: Reuse preview text
    end
    R->>A: Question, as_of_date, rulebook, README and preview
    Note over A,E: A fresh agent and executor are created for each question
    loop Until final answer or step limit
        A->>M: Task, tool descriptions and previous observations
        M-->>A: Thought and Python code
        A->>E: Code action
        E->>W: Start worker if needed and send code
        W->>D: Tools read relevant data
        D-->>W: Data
        W-->>E: Printed observations, result or error
        E-->>A: Observations or error
        alt Worker error
            Note over E,W: Worker is discarded and a later action must reload data
        else Successful action
            Note over W: Python variables persist for the next action
        end
    end
    A->>A: Validate final answer format
    A->>E: Clean up worker
    A-->>R: Typed answer, or abstain when step limit is reached
    R->>R: Add question id and flush submission row
```

The worker executes the model's Python and provides the registered tools. Printed observations go back to the agent; dataframes and parsed-table cache entries can stay in the worker across successful actions. The CLI allows at most four steps, one repair, and 60 seconds per question, subject to smaller configured limits. A valid answer format does not guarantee a correct calculation. An exception reaching the runner becomes an `abstain`; if the overall run deadline is reached before starting a question, remaining questions are not written.

Source: [agent.py](../../tabular_agent/agent.py), [executor.py](../../tabular_agent/executor.py), [worker.py](../../tabular_agent/worker.py), [cli.py](../../tabular_agent/cli.py).
