# T2-HQ — headquarters for the T2 droids

The hub repo for a small system of Trading212 automation droids. It holds the system-level
plan and architecture now, and will hold the orchestrator later: a PydanticAI agent, reached
through Telegram, that reports on the droids and passes on strategy advice from `t2-3po`.

## The system

| Repo | Role | Trading212 access | Runs |
|---|---|---|---|
| `t2-d2` | buys dips (DCA) across UK, US and EU stocks | places buy orders | systemd timers, one per trading session |
| `t2-k2` | harvests gains from overbought holdings | places sell orders | weekly timer |
| `t2-bb8` | harvests the Trading212 instrument list | metadata only | weekly timer |
| `t2-3po` | advisory council: reviews theses, strategy and configuration | read-only | weekly timer |
| **t2-hq** (this repo) | plan, architecture, and later the orchestrator and Telegram interface | none | long-running service (planned) |

All five repos live under the same GitHub owner. Clone them side by side:

```bash
mkdir -p "$T2_SRC" && cd "$T2_SRC"
for repo in t2-hq t2-d2 t2-k2 t2-bb8 t2-3po; do git clone "git@github.com:<owner>/$repo.git"; done
```

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): diagrams of how the pieces fit together.
- [PLAN.md](PLAN.md): the design, the work to get there in order, and the decisions made so far.
- [CONTRACT.md](CONTRACT.md): what every droid does at the edges: run reports, exit codes, locks.

## Tools

```bash
export T2_APPS=~/apps/t2          # or set it in ~/.config/t2/deploy.conf

bin/t2-deploy release t2-k2 v0.2.0   # tag a clean, tested main and push the tag
bin/t2-deploy deploy t2-k2           # deploy the newest tag and install its systemd user units
bin/t2-deploy status                 # versions, last runs and timers
bin/t2-deploy undeploy t2-k2         # remove the units; keeps .env and var/

bin/t2-contract check                # every droid's t2contract.py matches contract/
```

Run `bin/t2-deploy` with no arguments for the full usage.

## How the pieces relate

- **Each droid stands alone.** It runs from its own systemd timer with its own `.env` and
  virtual environment, and keeps working if every other part is down.
- **`t2-d2` and `t2-k2` trade on their own.** They follow the rules in their YAML configuration.
  No LLM and no Telegram approval sits in the order path.
- **`t2-3po` advises.** It reads the other droids' configuration and run reports and suggests
  changes, such as moving a stock from `mild` to `moderate` or adding a holding. The operator
  decides.
- **`t2-hq` connects them to the operator.** It runs droids as subprocesses, reads their reports,
  and talks to the operator through Telegram. It never places an order.

## Layout on disk

Two directories, chosen per installation:

```
$T2_SRC/     development: one git checkout per repo, side by side   (for example ~/src/t2)
$T2_APPS/    deployments: one folder per droid, side by side         (for example ~/apps/t2)
```

Each deployed droid has its own `.env`, written by the operator from that repo's `.env.example`.
No script or agent in this system creates, reads or edits a `.env` file.

## Status

Steps 1 and 2 of [PLAN.md](PLAN.md) are done: the repos are linked, every droid follows the
contract, and `bin/t2-deploy` deploys them. Next is step 3, `t2-3po advise`.
