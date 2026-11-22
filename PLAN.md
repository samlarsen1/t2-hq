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
| `t2-3po` | advisory council | read-only (GET only) | `evaluate`, `review` and `advise` work; runs on Gemini's free tier or Ollama |
| `t2-hq` | hub and orchestrator | none | the contract (`CONTRACT.md`), `bin/t2-deploy`, `bin/t2-contract`, and the read-only orchestrator (`t2-hq run`, 0.1.0) |

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
| `t2-3po` | daily 07:00 Europe/London, `review --batch 5`; Sunday 08:00, `advise` | yes | five stocks a day, least recently reviewed first, to stay inside the free tier's daily quota; after `t2-bb8` on Sundays |
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

## Part 4: `t2-3po` as strategy and configuration advisor *(done in 0.3.0)*

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

### Commands

- `t2-3po advise [--only d2|k2] [--no-portfolio] [--dry-run]` writes
  `var/suggestions/<date>_advice.json` and `reports/<date>_advice.md`. It runs weekly from
  `t2-3po-advise.timer`, Sunday 08:00, after the review.
- `t2-3po suggestions [--all]` lists suggestions; `t2-3po suggestions accept|reject <id> --note`
  records the operator's answer in `var/suggestions/decisions.json`. Ids are stable, so the same
  change suggested again keeps its id. `t2-hq` will call the same commands (Part 5).

### As built

- **Two agents, not the full council.** A Strategy Analyst drafts up to eight suggestions and a
  Risk Reviewer challenges each one. That's two model calls per run instead of four, which suits
  Gemini's free tier; the council stays for `evaluate`.
- **New holdings aren't picked from a code-built shortlist.** The analyst names a symbol and a
  market; code then requires exactly one tradable `t2-bb8` instrument in that market, a year of
  Yahoo prices, and that the stock isn't already listed. A ticker outside `t2-bb8`'s list is
  still impossible.
- **"Dip days by mode"** is the main evidence for mode changes: how often each mode's daily
  (and weekly) price rule would have fired in the last year. It ignores the RSI, trend and cash
  checks, so real buys are rarer, and the prompt says so.
- **Adjustable settings are an allow-list** in `t2-3po`'s `configs.py`: dip, position, cash and
  weight limits per t2-d2 mode, and t2-k2's overbought and harvest thresholds. A change keeps its
  sign and moves at most 50% per step. Order types, schedules and global limits are out of scope.
- **Privacy:** the model sees position weights, never account values.
- A suggestion rejected in the last 90 days, or already accepted, is dropped; an open one is
  marked as a repeat. At most five survive, highest confidence first.

## Part 5: the orchestrator (`t2-hq`) *(read-only part done in 0.1.0)*

### As built (step 4)

- **Service:** `t2-hq run`, a systemd user service (`Restart=on-failure`) deployed by
  `bin/t2-deploy`, which now also enables and restarts long-running services. It reads the
  droids from the sibling app directories (`APPS_DIR`, default `..`).
- **Telegram:** plain Bot API calls over `httpx` with long polling; no bot library. Commands
  `/status`, `/advice`, `/reset`, `/help`; anything else goes to the agent. Each update is handled
  in its own task, so a slow dry run doesn't block polling.
- **Agent tools:** `status`, `last_report`, `recent_runs`, `show_config`, `dry_run`,
  `suggestions`. `harvest_instruments`, `review_*` and `advise` aren't tools: those runs are long
  and scheduled weekly, and the agent can read their reports. `ask_user` isn't a tool either: the
  agent asks in its reply, and code-driven questions (the advice digest) use buttons.
- **Dry runs** set `T2_RUNS_DIR` to a temporary directory, so they don't count as real activity
  in t2-3po's advice, and switch the droid's Telegram off.
- **Watcher:** every minute, sends a new advice run as a digest with Accept/Reject buttons (which
  call `t2-3po suggestions accept|reject`) and alerts on any `failed` or `config_error` run
  report. On the first start it marks what's already there as seen.
- **Conversation history:** the last six question-and-answer pairs per chat, without tool calls.
- **Not yet:** missed-session detection (the `status` tool shows each timer's last and next run,
  so gaps are visible on request).

### As built (step 5, `t2-hq` 0.2.0)

- **Button, not tool.** Each suggestion in the digest has 📝 Open PR, ✅ Accept and ❌ Reject.
  Pushing to GitHub is an outward action, so only the button (or `t2-hq open-pr <id>`) starts
  it; the agent has no `open_config_pr` tool.
- **Code writes the change** (`configedit.py`): small line edits that keep comments and layout,
  then a check that the parsed file differs from the original by exactly the intended change,
  and that the current value is still what the suggestion saw. Removing a market's last stock
  writes `tickers: []`.
- **Its own clones** under `var/repos/`, reset to the latest `main` each time. The branch is
  `task/3po-<id>`, which the droid repos' branch-name rule accepts; a second press finds the
  existing pull request.
- **Checks run in CI**, on the pull request, rather than locally before pushing: the droid's
  own workflow runs its tests and linters, and nothing changes until the operator merges and
  redeploys.
- After opening the pull request, the suggestion is recorded as accepted in t2-3po, with the
  pull request's URL as the note.

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

Droids depend on it by git tag. It touches all four repos and changes no behaviour, but Parts 8
and 9 rely on one price store and one set of Yahoo symbol mappings, so it now comes before them.
Add to the list above:

- **One price store** for every droid: the same closes (adjusted or not, decided once and
  documented), the same Trading212 → Yahoo symbol mapping, and a local cache that the backtester
  in Part 9 can read years of history from.

## Part 8: trade ledger *(planned)*

Run reports say what a droid *decided* and which orders it *placed*. They don't say whether a
limit order filled, at what price, or what the position was worth later. The ledger is a single
local record of orders, fills and positions over time, which Part 9 measures against.

### Design

```
droids ──► var/runs/*.json ──┐
                             ├──► t2-hq ledger ingest ──► SQLite (var/ledger.db) ──► optional sinks
Trading212 history (GET) ────┘                              source of truth            Google Sheets
                                                                                        CSV export
```

- **The droids don't change.** They keep writing run reports and never depend on the ledger, so
  trading continues if the ledger or any sink is down (Principle 3).
- **SQLite is the source of truth**, one file under `var/`. It needs no server, is in the Python
  standard library, survives a reboot, and is easy to back up (`sqlite3 .backup`). WAL mode lets
  readers work while ingest writes. Postgres is not needed while everything runs on one host; the
  schema should be plain SQL so it can move to Postgres later if that changes.
- **Ingest is idempotent.** Every row has a natural key (run id, Trading212 order id, fill id),
  and ingest upserts by that key, so re-running it, or replaying all run reports, never
  duplicates anything.
- **Fills come from Trading212, not from run reports.** A read-only key (as `t2-3po` uses)
  reads the order and transaction history, so the ledger records what actually happened,
  including limit orders that never filled and orders the operator placed in the app.
- **Ingest runs on a timer** after the trading sessions, and on demand from the orchestrator. It
  takes the shared Trading212 lock for its history calls.

### Tables (first draft)

| Table | Holds |
|---|---|
| `runs` | one row per run report: droid, session, start, end, status, exit code |
| `decisions` | one row per ticker evaluated: rule fired or skip reason, the figures behind it |
| `orders` | every order placed: Trading212 id, `source` (`t2-d2`, `t2-k2`, `manual`), run id, side, type, limit, quantity, status |
| `lot_performance` | per fill: source, returns at each horizon, shadow benchmark return, excess return |
| `fills` | executed quantity, price, FX rate, fees, time |
| `positions_daily` | end-of-day quantity, average price, value and weight per ticker |
| `suggestions` | `t2-3po` suggestions and the operator's decision, with the pull request URL |
| `sync_state` | the last row each sink has received |

### Sinks: Google Sheets as one option, not a dependency

- **A sink is one-way and append-only.** It reads rows after its high-water mark in
  `sync_state`, writes them, then advances the mark. A failed sync retries the same rows next
  time; the ledger is never changed by a sink.
- **Google Sheets sink** (optional): enabled only when its settings are present in `t2-hq`'s
  `.env` (a service account key path and the spreadsheet id). One tab per table, batched appends
  to stay inside the Sheets API's per-minute write quota. The sheet is a view to read and chart;
  edits made in it are never read back.
- **CSV export** (always available): `t2-hq ledger export` writes each table to CSV, for any
  spreadsheet or for a backup.
- **Privacy:** the sheet is private to the operator, so the Sheets sink sends full rows,
  account values included. (This differs from the LLM calls, where only weights are sent.)

### Who placed each order

Every order in the ledger has a `source`: `t2-d2`, `t2-k2` or `manual`.

- **Trading212 marks where an order came from.** Pending orders carry `initiatedFrom` (`t2-k2`
  already uses it to cancel only API-placed sells) and historical orders carry `executor`. API
  orders are the droids'; anything placed from the app or the website is `manual`.
- **The droid comes from its run report.** `t2-k2`'s run report already records each order's
  Trading212 id. `t2-d2`'s `trades` don't yet: add the order id to each trade (a backwards
  compatible change to its run report), so ingest matches every API order to the droid and the
  session that placed it, and to the rule and figures behind it.
- **Fallbacks:** an API order with no matching run report (placed before the id was recorded)
  is attributed by side (buys to `t2-d2`, sells to `t2-k2`) and marked `inferred`.
- **Lots, not positions.** The droids and the operator often hold the same stock, so a position
  can't be split between them. Each fill is its own lot with its own source, which is what Part
  9 measures.

### Measuring operator trades against droid trades

Per lot, from the ledger and the price store:

- **Return to date** and at fixed horizons (5, 20, 60, 250 trading days), in GBP, FX and fees
  included.
- **Against a shadow benchmark:** the same money put into a broad index fund at the same time.
  The difference is what the decision added, and it makes trades of different sizes and dates
  comparable.
- **Sells** are measured the other way: the return of the shares sold over the following
  horizons. A negative figure means selling avoided a loss.
- **Grouped by source** (and for the droids, by mode and session) to chart cumulative excess
  return, hit rate and average excess return per trade for `manual`, `t2-d2` and `t2-k2`.
- **Money-weighted return (XIRR) per source**, from each source's cash flows, plus the current
  value of its remaining lots. For sold shares, lots are matched first in, first out within the
  same source.

The Sheets sink writes a `lot_performance` tab, refreshed daily, ready to chart.

### Commands

- `t2-hq ledger ingest [--since DATE]`, `t2-hq ledger sync [--sink sheets]`,
  `t2-hq ledger export DIR`.
- Agent tool `trades(ticker, since)`: read-only queries over the ledger, so questions such as
  "what has d2 bought in RIO this year and at what average?" are answered from the ledger rather
  than by reading run reports through the model.

## Part 9: market analysis improvements *(planned)*

Today the evidence behind `t2-3po`'s suggestions counts how often a rule *would fire* ("dip days
by mode"). It doesn't measure whether firing made money. These features add that measurement
first, then better signals. Each is computed in code and reaches `t2-3po` as evidence, keeping
"code computes, the council judges". New rules land as options in `strategy.yaml`, off by
default, through the usual reviewed pull request.

1. **Backtesting with the droids' own rules.** `t2d2-algo` and `t2-k2`'s `strategy.py` are pure,
   so they can be replayed over years of cached prices (Part 7's price store): d2 and k2
   together, with Trading212's FX fee on non-GBP trades. Compare each mode, and the system as a
   whole, against regular investment in a broad index fund from the watchlist. This answers
   "is `mild` or `moderate` better for this stock?" with returns, not trigger counts.
2. **Forward returns on every real trade.** From the ledger (Part 8), record each buy's and
   sell's return 5, 20 and 60 trading days later, by droid, mode and ticker. Score `t2-3po`
   suggestions the same way: did accepted suggestions do better than rejected ones? This is the
   quickest feedback loop and needs only data the system already produces.
3. **Volatility-scaled thresholds and sizing.** A fixed -3% daily dip is noise for a volatile
   small cap and a real move for a utility. Add an option to express dips in standard deviations
   of the stock's recent daily returns (or in ATR), and to size buys inversely to volatility, so
   each position adds similar risk rather than similar pounds.
4. **Dividends and earnings.** Check that every price module uses closes adjusted for dividends,
   so an ex-dividend drop isn't read as a dip (this matters most for high-yield holdings). Add
   earnings dates: skip buys in the session before results, and treat a gap after results
   separately from an ordinary dip.
5. **Exposure limits by theme.** Today's limits are per position. Holdings cluster by pillar of
   the macro thesis (for example hydrogen and renewables, electrification, semiconductors), so a
   sector sell-off can spend every trade in a run on the same bet. Tag each stock with its
   pillar in `stocks.yaml`, add a maximum weight per pillar, and have `t2-3po` report pillar
   weights and the correlation between holdings.
6. **Market regime filter.** Scale the cash `t2-d2` deploys by the state of the broad market,
   for example less when a broad index is below its 200-day SMA or volatility is high. This sits
   above the per-stock rising-SMA gate.
7. **Value-trap checks for `deep_value`.** "Near the 52-week low" also catches businesses that
   are getting worse. Add code-computed quality figures (revenue trend, debt, cash burn) as
   evidence, and optionally as a gate.
8. **`t2-k2` as rebalancing.** In an ISA there is no capital gains tax, so trimming is a
   rebalancing policy, not tax harvesting. Measure in the backtest whether trimming and later
   buying back on a dip beats holding after FX fees, and how long trimmed cash sits idle before
   `t2-d2` deploys it.

## Documentation fixes

- `t2-d2`'s README mode table doesn't match `config/strategy.yaml` (cash deployed per run and
  position increases for `mild`, `moderate` and `aggressive`), and the "Schedules" section has a
  leftover fragment of the old cron table. Generate the table from the YAML, or link to it.
- Every droid README's repo table still lists `t2-hq` as "(planned)".
- Add `.headroom/` to `t2-3po`'s `.gitignore`.

## Order of work

1. **Link the repos.** Create `t2-hq`; cross-link every README and `CLAUDE.md`. *(done)*
2. **Contract and deploy.** JSON reports, exit codes and locks in each droid; `bin/t2-deploy`;
   systemd units; align on Python 3.12. *(done; see "Rolling out step 2" below)*
3. **`t2-3po advise`.** Config readers, suggestion models, validation, the new command. Usable
   from the command line before the orchestrator exists. *(done: `t2-3po` 0.3.0)*
4. **Orchestrator, read-only.** `t2-hq` with the LLM toggle, Telegram questions and answers,
   `status`, `last_report`, `dry_run`, and the advice digest. *(done: `t2-hq` 0.1.0)*
5. **Suggestion to pull request.** *(done: `t2-hq` 0.2.0)*
6. **Documentation fixes.** Small; any time.
7. **Shared library and price store** (Part 7).
8. **Trade ledger** (Part 8): first, `t2-d2` records the Trading212 order id of each trade in
   its run report; then SQLite ingest from run reports and Trading212 history, CSV export, and
   the optional Google Sheets sink.
9. **Forward returns** (Part 9, item 2): the first measurement, from the ledger.
10. **Backtester** (Part 9, item 1), with its results passed to `t2-3po advise` as evidence.
11. **Better signals** (Part 9, items 3–8), each as a `strategy.yaml` option, off by default,
    justified by the backtest before it's turned on.

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

- **An accepted suggestion can become a pull request** at the press of a button; accepting
  without one is still possible.
