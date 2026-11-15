# T2-HQ — headquarters for the T2 droids

The hub repo for a small system of Trading212 automation droids. It holds the system-level
plan and architecture now, and will hold the orchestrator later: a PydanticAI agent, reached
through Telegram, that reports on the droids and passes on strategy advice from `t2-3po`.

## The system

| Repo | Role | Trading212 access | Runs |
|---|---|---|---|
| [t2-d2](https://github.com/samlarsen1/t2-d2) | buys dips (DCA) across UK, US and EU stocks | places buy orders | six systemd timers, weekdays |
| [t2-k2](https://github.com/samlarsen1/t2-k2) | harvests gains from overbought holdings | places sell orders | weekly (not yet deployed) |
| [t2-bb8](https://github.com/samlarsen1/t2-bb8) | harvests the Trading212 instrument list | metadata only | weekly (not yet deployed) |
| [t2-3po](https://github.com/samlarsen1/t2-3po) | advisory council: reviews theses, strategy and configuration | read-only | weekly (not yet deployed) |
| **t2-hq** (this repo) | plan, architecture, and later the orchestrator and Telegram interface | none | long-running service (not yet built) |

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): diagrams of how the pieces fit together.
- [PLAN.md](PLAN.md): the work to get there, in order, with the decisions made so far.

## How the pieces relate

- **Each droid stands alone.** It runs from its own systemd timer with its own `.env` and
  virtual environment, and keeps working if every other part is down.
- **`t2-d2` and `t2-k2` trade on their own.** They follow the rules in their YAML configuration.
  No LLM and no Telegram approval sits in the order path.
- **`t2-3po` advises.** It reads the other droids' configuration and run reports and suggests
  changes, such as moving a stock from `mild` to `moderate` or adding a holding. Sam decides.
- **`t2-hq` connects them to Sam.** It runs droids as subprocesses, reads their reports, and
  talks to Sam through Telegram. It never places an order.

## Layout on disk

```
~/dev/git/samlarsen1/t2/     development: one git repo per droid, plus this one
~/dev/apps/                  deployments: one folder per droid, each with its own .env
```

`.env` files are managed by hand. No script or agent in this system creates, reads or edits one.

## Status

Planning. This repo has no code yet; see the order of work in [PLAN.md](PLAN.md).
