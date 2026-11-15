# T2 droids: independent schedules plus an agentic orchestrator

The design for running the T2 droids on their own schedules while an orchestrator ties them
together, and the order of work to get there. Diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md).

Written for any installation. Two directories are chosen per host:

- `$T2_SRC`: development checkouts of every repo, side by side (for example `~/src/t2`).
- `$T2_APPS`: deployed apps, side by side (for example `~/apps/t2`).

"The operator" is whoever runs the installation: they own the Trading212 account, the Telegram
chat and every `.env` file.

## Goal

- Each droid runs on its own local schedule, with no dependency on the others or on an LLM.
- `t2-d2` and `t2-k2` trade by rule. No approval step sits in the order path.
- `t2-3po` is the system's advisor: it reviews the strategy and configuration of the other
  droids and suggests changes, for example "move this stock from `mild` to `moderate`" or "add
  this holding to `stocks.yaml`".
- A higher-level agent (`t2-hq`) ties it together: it answers questions, runs dry runs, and
  delivers `t2-3po`'s advice.
- Stack: PydanticAI for orchestration; an LLM toggle between local Ollama and the Google Gemini
  free API (Flash); Telegram as the interface, able to ask a question and receive the answer; the
  droids as the execution layer.
- Development happens in `$T2_SRC`; deployed apps live in `$T2_APPS`.
- Each app has its own `.env`, written by the operator. No script or agent creates, reads or
  edits one.

## Current state of the repos

| Repo | Role | Trading212 access | State |
|---|---|---|---|
| `t2-d2` | dip buyer, one run per trading session | places buys | working; scheduled with systemd timers (see its `install.md`) |
| `t2-k2` | gain harvester | places sells (`--execute`) | working; weekly timer described in its README |
| `t2-bb8` | instrument harvester | metadata only | working; conservative with the rate limits |
| `t2-3po` | advisory council | read-only (GET only) | `evaluate` and `review` work; runs on Gemini's free tier or Ollama |
| `t2-hq` | hub and orchestrator | none | the contract (`CONTRACT.md`), `bin/t2-deploy` and `bin/t2-contract`; no orchestrator yet |

Things that matter for the design:

- **Code is duplicated.** There are four Trading212 clients (`t2d2-client`, `t2k2/client.py`,
  `t2bb8/client.py`, `t2_3po/t212.py`) and three Yahoo data modules. Telegram is send-only and
  lives inside `t2-d2`'s scheduler.
- **Every droid follows the contract** in [CONTRACT.md](CONTRACT.md) (version 0.2.0 of each droid):
  a JSON run report per run, exit codes 0/1/2/3, a run lock, and the shared Trading212 lock.
- **Only code and configuration go to GitHub.** Harvested data, price caches, reports and
  proposals are gitignored in every repo. Deployed apps write them under `var/`.
- **`t2-bb8` is conservative with the API key:** at most one harvest per 20 hours, retries never
  inside an endpoint's rate-limit window, three attempts at most.
- **`t2-3po` reads `../t2-d2/config/stocks.yaml` by relative path.** That works in both
  `$T2_SRC` and `$T2_APPS` as long as the repos sit side by side.
- All droids need Python 3.12.

## Principles

1. **Droids are standalone command-line tools.** A timer, a person, or the orchestrator can run
   one, and it behaves the same each time.
2. **The orchestrator calls droids as subprocesses and reads their reports.** It does not import
   them. Each droid keeps its own virtual environment, `.env` and failure isolation.
3. **Scheduled runs never depend on the LLM.** If the orchestrator, Gemini or Ollama is down,
   the timers still run.
4. **Trading follows rules in YAML, and nothing else.** The droids place orders on their own;
   no LLM output reaches an order. The only way an LLM influences trading is a suggestion that
   the operator accepts and that then lands as a reviewed change to a YAML file.
5. **Least privilege for keys.** Each droid gets only the Trading212 permission it needs. The
   orchestrator holds no Trading212 key.

## Target layout

```
$T2_SRC/                          development (git checkouts)
  t2-hq/                          hub: plan, architecture, orchestrator
  t2-bb8/  t2-d2/  t2-k2/  t2-3po/
  t2-core/                        later: shared library

$T2_APPS/                         deployments (tagged releases, never edited by hand)
  t2-bb8/  t2-d2/  t2-k2/  t2-3po/  t2-hq/
    .env                          written by the operator from .env.example
    .venv/                        built by `uv sync --frozen --no-dev`
    var/                          runs/, data/, cache/ — untouched by deploys
```

Every repo's README and `CLAUDE.md` link to `t2-hq`, and `t2-hq` lists each of them.

## Part 1: droid contract *(done)*

Every droid meets the same contract, so timers, the orchestrator and `t2-3po` treat them alike.
The rules are in [CONTRACT.md](CONTRACT.md); in short:

- **Dry run by default** for the droids that trade (`t2-d2`, `t2-k2`).
- **A JSON run report per run** in `var/runs/`, in the shape of
  [`contract/run-report.schema.json`](contract/run-report.schema.json), including runs that fail,
  skip or crash.
- **Exit codes:** 0 ok, 1 failed, 2 configuration or usage error, 3 skipped.
- **One run at a time per droid**, and **a shared Trading212 account lock** so droids on
  independent timers don't trip the per-account rate limits. `t2-d2`'s Tuesday 22:00 session and
  `t2-k2`'s weekly harvest, for example, now take turns.
- **One implementation.** [`contract/t2contract.py`](contract/t2contract.py) is the reference,
  vendored unchanged into each droid; `bin/t2-contract check` catches drift. It moves to `t2-core`
  in Part 7.

## Part 2: deployment *(done)*

A deployed app is a git checkout of a release tag in `$T2_APPS`, with its own virtual
environment. One tool in this repo, [`bin/t2-deploy`](bin/t2-deploy), deploys every droid, rather
than a copy of the same script in each repo:

- `t2-deploy check <droid>`: the droid's tests.
- `t2-deploy release <droid> vX.Y.Z`: only from a clean `main` that matches `origin/main`, with
  the project version equal to the tag and the tests passing; tags and pushes the tag.
- `t2-deploy deploy <droid> [tag]` (default: the newest tag):
  1. refuses if the droid already has system units, so it can never be scheduled twice;
  2. checks out the tag (detached) in `$T2_APPS/<droid>`, refusing if the checkout has local
     changes; an existing folder without git (holding only `.env`) is fine;
  3. `uv sync --frozen --no-dev`;
  4. creates `var/`;
  5. stops if `.env` is missing, before installing anything;
  6. renders the droid's units from `deploy/systemd/`, removes units the new version no longer has,
     installs them as systemd **user** units, and enables the timers;
  7. prints the deployed version and the next runs, and warns if lingering is off.
- `t2-deploy undeploy <droid>`: disables and removes the units; leaves `.env` and `var/` alone.
- `t2-deploy status`: deployed versions, each droid's last run, and all `t2*` timers.

The deploy never creates, reads or edits `.env`.

## Part 3: scheduling *(done)*

systemd user timers on the host that runs the apps. Each repo ships its units under
`deploy/systemd/`.

| Droid | Schedule | Persistent | Notes |
|---|---|---|---|
| `t2-d2` | one timer per enabled session in `config/schedules.yaml` | no | rendered by `deploy/systemd/render.py`; `DRY_RUN` in `.env` decides live or dry |
| `t2-k2` | Tuesday 22:00 Europe/London | no | **dry run** unless the operator adds `--execute` in a drop-in (see its README) |
| `t2-bb8` | Sunday 06:00 Europe/London | yes | writes to `var/data` |
| `t2-3po` | Sunday 07:00 Europe/London, `review` | yes | after `t2-bb8`; paced free-tier model calls |
| `t2-hq` | long-running service | n/a | `Restart=on-failure` (Part 5) |

- **User units, not system units.** They install without `sudo` and run while the user is logged
  out once lingering is on (`loginctl enable-linger`). `t2-d2`'s `install.md` still documents
  system units for a standalone install; `bin/t2-deploy` refuses to add user units next to them.
- **Missed runs.** Timers only fire while the host is running and awake; a laptop that sleeps
  will miss sessions. An always-on host such as a Raspberry Pi avoids that. Either way, the
  `status` check in `t2-hq` reports missed sessions, so a gap is visible in Telegram instead of
  silent. `t2-bb8` and `t2-3po` use `Persistent=true` so they run at the next wake-up. `t2-d2`'s
  sessions and `t2-k2`'s harvest don't: firing late would trade on stale prices.
- Timers use each market's time zone.

## Part 4: `t2-3po` as strategy and configuration advisor

`t2-3po` evaluates a thesis (`evaluate`) and profiles each stock in `t2-d2`'s list against a macro
thesis (`review`). The new work adds a third job: review how the other droids are *configured*
and suggest changes.

### What it reads

| Source | Used for |
|---|---|
| `t2-d2/config/stocks.yaml` | which stocks are traded and each one's mode (`deep_value`, `mild`, `moderate`, `aggressive`) |
| `t2-d2/config/strategy.yaml` | what each mode does: dip thresholds, position and cash limits |
| `t2-d2/config/schedules.yaml` | when sessions run |
| `t2-k2/config/strategy.yaml` | overbought signals and harvest sizes |
| `t2-bb8` instrument data | which instruments exist on Trading212, with their exact tickers |
| run reports from `t2-d2` and `t2-k2` | what the rules did: buys, sells, skips and the reasons |
| Trading212 portfolio (GET only) | current weights and cash |
| Yahoo Finance | prices, volatility, fundamentals |
| `theses/macro/core.md` | what the portfolio is for |

When deployed, these are the sibling folders in `$T2_APPS`, so it reviews the configuration that
is running. Paths are settings with those defaults.

### What it suggests

Each suggestion is a typed record, not free text:

| Kind | Example |
|---|---|
| change a stock's mode | `RIO`: `mild` → `moderate` |
| add a holding | new entry for `stocks.yaml` with `symbol`, `t212_ticker`, `name`, market and mode |
| remove or pause a holding | move a stock to the watchlist |
| change a threshold | `mild.min_daily_dip_pct`: -3.0 → -2.5; `t2-k2` `overbought.rsi_min`: 70 → 75 |
| no change | stated explicitly, with the reason, so silence isn't ambiguous |

Every suggestion carries: the file and key, current and proposed value, the evidence (figures
with their source), the expected effect, the main risk, and a confidence level.

### How it stays trustworthy

- **Code computes, the council judges.** Figures such as "this stock dipped 3% on 14 days this
  year and `mild` bought it twice" come from code over prices and run reports. The LLM argues
  from those figures; it doesn't produce them.
- **Code validates every suggestion** before it is shown: the mode exists in `strategy.yaml`;
  the `t212_ticker` exists in `t2-bb8`'s data and trades in the stated market and currency; the
  holding isn't already listed; the change keeps the portfolio inside the concentration limits;
  a threshold stays inside a sane range. A suggestion that fails is dropped and logged.
- **A candidate list comes from code.** For new holdings, code first narrows `t2-bb8`'s
  instruments (ISA-eligible, liquid, in a market `t2-d2` trades) before the council sees them.
  The council can't suggest a ticker that isn't in that list.
- **A cap per review** (for example five suggestions), ranked, so the digest stays readable.
- **Memory of past advice.** Suggestions and the operator's answers are stored, so the council
  doesn't repeat a rejected idea every week and can later be judged against what happened.
- `t2-3po` writes suggestions and reports. It never edits another repo's files.

### New command

`t2-3po advise [--only d2|k2] [--dry-run]` writes `var/suggestions/<date>.json` and a markdown
report. It reuses the existing council (Bull, Bear, Quantitative Analyst, Protocol Officer).

## Part 5: the orchestrator (`t2-hq`)

One long-running service with three parts.

### LLM toggle

- Reuse `t2-3po`'s `build_model(settings)`, which already has `MODEL_PROVIDER=google|ollama`,
  free-tier pacing (`PacedModel`) and retry with backoff. No paid provider anywhere in the system.
- Use a fallback chain so a rate-limited Gemini call falls back to Ollama.
- **Size Ollama to the host.** Without a GPU and with around 8 GB of RAM, only a model of about
  4B parameters fits; it is slow and unreliable at calling tools. On such a host, use Gemini Flash
  for the agent that calls tools and keep Ollama for offline fallback and simple summaries.
- **Privacy:** Google's free tier may use prompts to improve its products. Decide how much
  portfolio detail is sent, and consider sending weights and tickers but not account values.
- **Limits:** free-tier rate limits are low and change. The agent should make few calls per
  question and cache droid reports instead of re-reading them through the model.
- Model names go in `.env`, not in code.

### Telegram interface

- The orchestrator owns the bot's incoming messages and uses long polling, so no inbound port is
  needed. Only one process may poll a bot; `t2-d2` keeps sending its session summaries with
  `sendMessage`, which doesn't conflict.
- It accepts messages only from the configured chat ID and ignores everything else.
- Two directions:
  - **The operator asks:** free text goes to the agent ("what did d2 do today?", "why hasn't RR
    been bought?", "dry-run k2").
  - **The agent asks:** a question with buttons or choices, an ID, a timeout, and a default of
    "do nothing". Used for advice: "3PO suggests moving RIO to `moderate`. Open a PR, skip, or
    tell me more?"
- Pending questions are stored in SQLite under `var/`, so they survive a restart.

### Agent and tools

PydanticAI agent with typed tools. Each tool runs a droid subprocess or reads a report.

| Tool | What it does |
|---|---|
| `status` | timers, last run per droid, failures, missed sessions |
| `last_report(droid)` | parsed latest run report |
| `dry_run(droid, args)` | runs the droid in dry-run mode |
| `show_config(droid)` | current stocks, modes and thresholds |
| `harvest_instruments` | runs `t2-bb8` |
| `advise` / `review_stocks` / `review_thesis` | runs `t2-3po` (note the model calls) |
| `suggestions(status)` | open, accepted and rejected suggestions |
| `open_config_pr(suggestion_id)` | opens a pull request with that YAML change |
| `ask_user(question, options)` | sends a Telegram question and waits |

There is **no tool that places an order**, and none that runs a droid with `--execute`.

How a suggestion becomes a change:

1. `t2-3po advise` writes validated suggestions.
2. `t2-hq` sends a digest to Telegram: each suggestion with its evidence and risk.
3. The operator answers per suggestion: open a PR, skip, or ask a follow-up question.
4. For "open a PR", code (not the LLM) applies the stored suggestion to the YAML on a
   `task/3po-<id>` branch of the repo in `$T2_SRC`, runs that repo's config validation and
   tests, pushes, and opens a pull request. The LLM never writes the file content.
5. The operator reviews and merges. The change is live after the next `just deploy`.

Fixed multi-step jobs, such as "harvest, then advise, then send the digest", are plain code or a
graph, with the LLM used only for summarising and for the conversation.

**Untrusted text.** `t2-3po` reads documents and Yahoo data. Small free models are easy to steer
with instructions hidden in such text, so that content never reaches the tool-calling agent
directly; it sees structured report and suggestion fields only.

### The orchestrator's settings

Its `.env` holds the Telegram bot token and chat ID, the Gemini and Ollama settings, and a GitHub
token limited to opening pull requests on the `t2-*` repos. It holds no Trading212 key.

## Part 6: keys and `.env` files

The operator writes these; this is the recommended setup.

- One `.env` per app, each written from that repo's `.env.example`. Don't share one file
  between apps: a droid should only be able to load the keys it needs.
- Separate Trading212 keys by permission: metadata only for `t2-bb8`; read-only for `t2-3po`;
  portfolio, orders read and orders execute for `t2-d2` and `t2-k2`.
- Every repo keeps an `.env.example` listing all variables with placeholders.

## Part 7: shared library (later)

Once the contract is stable, extract `t2-core`:

- one Trading212 client (rate limits, retries, the shared lock, no retry on order placement);
- Yahoo data and the price cache;
- Telegram send helpers;
- run-report and suggestion models;
- the lock-file helper.

Droids depend on it by git tag. Do this last: it touches all four repos and changes no behaviour.

## Order of work

1. **Link the repos.** Create `t2-hq`; cross-link every README and `CLAUDE.md`. *(done)*
2. **Contract and deploy.** JSON reports, exit codes and locks in each droid; `bin/t2-deploy`;
   systemd units; align on Python 3.12. *(done; see "Rolling out step 2" below)*
3. **`t2-3po advise`.** Config readers, suggestion models, validation, the new command. Usable
   from the command line before the orchestrator exists.
4. **Orchestrator, read-only.** `t2-hq` with the LLM toggle, Telegram questions and answers,
   `status`, `last_report`, `dry_run`, and the advice digest.
5. **Suggestion to pull request.**
6. **Shared library.**

### Rolling out step 2

Each droid's 0.2.0 must be merged, released and deployed. Per host:

1. Merge each droid's contract branch, then `bin/t2-deploy release <droid> v0.2.0` for each.
2. Make sure each `$T2_APPS/<droid>/.env` exists (the operator writes it), and that
   `GOOGLE_API_KEY` or `MODEL_PROVIDER=ollama` is in `t2-3po`'s.
3. `bin/t2-deploy deploy t2-bb8`, then `t2-k2` (dry run), then `t2-3po`.
4. `t2-d2` last, and only after removing its system units if it has them (its README,
   Deployment), ideally outside market hours: `bin/t2-deploy deploy t2-d2`.
5. `bin/t2-deploy status` to see the timers, then check each droid's first run report.

## Design decisions

- **Schedules run on one host with systemd user timers**, installed by `bin/t2-deploy`. The
  orchestrator runs on the same host.
- **One deploy tool and one contract module in `t2-hq`**, not a copy of each in every droid.
- **No Telegram approval is needed to place orders.** The droids trade on their own rules.
- **`t2-3po` reviews strategy and configuration** across the other repos and advises changes.
- **The hub repo is `t2-hq`.**
- **`t2-bb8` harvests conservatively**, so the shared Trading212 key isn't rate limited.
- **Only code and configuration go to GitHub.** Harvested data, caches, reports and proposals
  stay local.
- **Free models only.** `t2-3po` and the orchestrator run on Gemini's free tier or Ollama.

Still open:

1. When the operator accepts a suggestion in Telegram, should `t2-hq` open a pull request
   (Part 5), or only report and leave the edit to the operator? The plan assumes a pull request.
