# Architecture

How the T2 droids fit together. Solid boxes exist today; items marked *planned* are described in
[PLAN.md](PLAN.md).

## System overview

```mermaid
flowchart TB
    op(["Operator"])
    tg["Telegram bot"]

    subgraph hq["t2-hq: orchestrator (planned)"]
        agent["PydanticAI agent<br/>status, reports, dry runs, advice"]
        llm{{"LLM toggle<br/>Gemini Flash / Ollama"}}
        agent --- llm
    end

    subgraph advisory["Advisory layer: read-only"]
        c3po["t2-3po<br/>thesis, strategy and config review<br/>Gemini Flash / Ollama"]
    end

    subgraph exec["Execution layer: trades by rule, no LLM"]
        d2["t2-d2<br/>buys dips"]
        k2["t2-k2<br/>harvests gains"]
    end

    bb8["t2-bb8<br/>instrument harvester"]

    timers[["systemd timers<br/>(host machine)"]]
    t212[("Trading212 API")]
    yahoo[("Yahoo Finance")]

    op <-->|"questions, answers"| tg
    tg <-->|"long polling"| agent

    agent -->|"runs as subprocess"| c3po
    agent -.->|"dry runs only"| d2
    agent -.->|"dry runs only"| k2
    agent -->|"reads run reports"| exec

    c3po -->|"reads config and run reports"| exec
    c3po -->|"reads instrument list"| bb8
    c3po ==>|"suggestions"| agent

    timers --> d2
    timers --> k2
    timers --> bb8
    timers --> c3po

    d2 -->|"buy orders"| t212
    k2 -->|"sell orders"| t212
    bb8 -->|"metadata"| t212
    c3po -->|"GET only"| t212
    d2 & k2 & c3po --> yahoo

    d2 -.->|"session summaries"| tg
```

Three things to read from this:

- **Orders have one path:** timer → droid → Trading212. The orchestrator and the LLM aren't on it.
- **Advice has one path:** `t2-3po` → `t2-hq` → Telegram → the operator. Nothing changes until
  the operator acts.
- **The timers don't depend on the orchestrator.** If `t2-hq` or the LLM is down, trading continues.

## The advice loop

How a suggestion such as "move RIO from `mild` to `moderate`" or "add a new holding" travels.

```mermaid
sequenceDiagram
    autonumber
    participant T as systemd timer
    participant C as t2-3po
    participant R as droid repos<br/>(config, run reports)
    participant H as t2-hq
    participant S as Operator (Telegram)
    participant G as GitHub

    T->>C: weekly strategy review
    C->>R: read stocks.yaml, strategy.yaml,<br/>run reports, bb8 instruments
    C->>C: council proposes changes,<br/>code validates each one
    C->>H: suggestions (JSON)
    H->>S: digest: suggestion, evidence, risk
    S->>H: "open a PR for 1 and 3", or a question
    H->>G: pull request with the YAML change
    S->>G: review and merge
    Note over R,G: The change takes effect at the next deploy.<br/>Nothing is applied automatically.
```

## Who may do what

| Component | Trading212 | Writes to | LLM | Schedule |
|---|---|---|---|---|
| `t2-d2` | portfolio, place buys | its own logs and reports | none | one timer per trading session, weekdays |
| `t2-k2` | portfolio, place and cancel its own sells | its own reports | none | weekly |
| `t2-bb8` | instrument metadata, at most one harvest per 20 hours | its data folder (local only, not in git) | none | weekly |
| `t2-3po` | GET only | its reports and suggestions | Gemini Flash (free tier) or Ollama | weekly |
| `t2-hq` | none | pull requests, Telegram, its own state | Gemini Flash or Ollama | always on |

## Where things live

```mermaid
flowchart LR
    subgraph dev["$T2_SRC (development)"]
        r1["t2-hq"]
        r2["t2-d2"]
        r3["t2-k2"]
        r4["t2-bb8"]
        r5["t2-3po"]
    end
    gh[("GitHub<br/>t2-* repos")]
    subgraph apps["$T2_APPS (deployed, tag-pinned)"]
        a1["t2-hq"]
        a2["t2-d2"]
        a3["t2-k2"]
        a4["t2-bb8"]
        a5["t2-3po"]
    end
    dev -->|"push, tag"| gh
    gh -->|"t2-deploy deploy"| apps
    env["each app's .env<br/>written by the operator"] -.-> apps
```

Deployed apps sit side by side, so `t2-3po` reads `../t2-d2/config/` and `../t2-k2/config/`: the
configuration that is actually running, not a development branch.
