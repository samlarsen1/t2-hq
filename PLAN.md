# T2 droids: plan for independent schedules plus an agentic orchestrator

First written 2026-11-15 from a review of `t2-bb8`, `t2-d2`, `t2-k2` and `t2-3po` and of what is
deployed in `~/dev/apps`. Revised the same day with Sam's decisions (see the end).
Diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Goal

- Each droid keeps running on its own local schedule, with no dependency on the others or on an LLM.
- `t2-d2` and `t2-k2` keep trading by rule. No approval step is added to the order path.
- `t2-3po` becomes the system's advisor: it reviews the strategy and configuration of the other
  droids and suggests changes, for example "move this stock from `mild` to `moderate`" or "add
  this holding to `stocks.yaml`".
- A higher-level agent (`t2-hq`) ties it together: it answers questions, runs dry runs, and
  delivers `t2-3po`'s advice.
- Stack: PydanticAI for orchestration; an LLM toggle between local Ollama and the Google Gemini
  free API (Flash); Telegram as the interface, able to ask a question and receive the answer; the
  existing droids, improved, as the execution layer.
- All development happens in `~/dev/git/samlarsen1/t2/*`. All deployed apps live in `~/dev/apps`.
- Each system has its own `.env`, managed by Sam. No script or agent creates, reads or edits one.

## Where things stand today

| Repo | Role | Trading212 access | State |
|---|---|---|---|
| `t2-bb8` | instrument harvester | metadata only | `apps/t2-bb8` is empty: not deployed |
| `t2-d2` | dip buyer, 6 sessions | places buys | **deployed and scheduled**; the dev repo is on `feature/sma200-uptrend-gate`, 2 commits ahead of the deployed `main` |
| `t2-k2` | gain harvester | places sells (`--execute`) | `apps/t2-k2` holds only an `.env` link: not deployed |
| `t2-3po` | advisory council | read-only (GET only) | not deployed; on `feature/initial-build`; PydanticAI with a Gemini (free tier) / Ollama toggle (`feature/free-models`) |
| `t2-hq` | hub and orchestrator | none | this repo; documents only |

Things that matter for the design:

- **`t2-d2` is scheduled on this Chromebook.** Six system-level timers (`t2d2-<session>.timer`
  in `/etc/systemd/system`) start `t2d2@<session>.service`, which runs as `sam` from
  `~/dev/apps/t2-d2`. They last fired on 2026-11-14 and send the daily Telegram messages. Nothing
  else is scheduled; there is no cron.
- **This machine is a Chromebook Linux container:** 16 cores, 6.3 GB RAM, no GPU. Timers only
  fire while the Chromebook is awake, and they are set not to catch up on missed runs. Ollama
  isn't installed.
- **Both deployed `.env` files are links to one shared live secrets file.** Every droid can
  therefore load every key, including the ones that place orders.
- **Code is duplicated.** There are four Trading212 clients (`t2d2-client`, `t2k2/client.py`,
  `t2bb8/client.py`, `t2_3po/t212.py`) and three Yahoo data modules. Telegram is send-only and
  lives inside `t2-d2`'s scheduler.
- **Only `t2-k2` writes a machine-readable report** (`--report`). `t2-d2` and `t2-bb8` only log.
- **`t2-bb8` commits its harvested data to git**, and keeps doing so. A deployed checkout must
  therefore write to `var/data` (`--data-dir`) so the checkout stays clean.
- **`t2-bb8` is conservative with the shared key** (`feature/conservative-harvest`): at most one
  harvest per 20 hours, retries never inside an endpoint's window, three attempts at most.
- **`t2-3po` reads `../t2-d2/config/stocks.yaml` by relative path.** That works in both the dev
  and the apps layout as long as the two sit side by side.
- Python versions differ: `t2-3po` allows 3.11, the others need 3.12.

## Principles

1. **Droids are standalone command-line tools.** A timer, a person, or the orchestrator can run
   one, and it behaves the same each time.
2. **The orchestrator calls droids as subprocesses and reads their reports.** It does not import
   them. Each droid keeps its own virtual environment, `.env` and failure isolation.
3. **Scheduled runs never depend on the LLM.** If the orchestrator, Gemini or Ollama is down,
   the timers still run.
4. **Trading follows rules in YAML, and nothing else.** The droids place orders on their own;
   no LLM output reaches an order. The only way an LLM influences trading is a suggestion that
   Sam accepts and that then lands as a reviewed change to a YAML file.
5. **Least privilege for keys.** Each droid gets only the Trading212 permission it needs. The
   orchestrator holds no Trading212 key.

## Target layout

```
~/dev/git/samlarsen1/t2/          development (git repos)
  t2-hq/                          hub: plan, architecture, orchestrator
  t2-bb8/  t2-d2/  t2-k2/  t2-3po/
  t2-core/                        later: shared library

~/dev/apps/                       deployments (tagged releases, never edited by hand)
  t2-bb8/  t2-d2/  t2-k2/  t2-3po/  t2-hq/
    .env                          managed by Sam
    .venv/                        built by `uv sync --frozen --no-dev`
    var/                          runs/, data/, cache/ — untouched by deploys
```

Every repo's README and `CLAUDE.md` link to `t2-hq`, and `t2-hq` links to each of them.

## Part 1: droid contract

Every droid meets the same contract, so timers, the orchestrator and `t2-3po` treat them alike.

- **Dry run by default.** Already true for `t2-d2` and `t2-k2`; `t2-bb8` and `t2-3po` don't trade.
- **JSON run report.** Each run writes `var/runs/<UTC timestamp>-<command>.json` with: droid,
  version, command, start and end time, dry-run flag, exit status, and a result section (decisions,
  orders, skips with reasons, errors). A `--report PATH` flag overrides the location.
  - `t2-k2`: has `--report`; add the default location and the common header fields.
  - `t2-d2`: add a report built from `RunResult`; add a `t2d2` console script so it runs the same
    way as the others.
  - `t2-bb8`: `manifest.json` is close; add the run report and move output to `var/data` when
    deployed (`--data-dir`). The dev repo keeps committing data. Its exit code 3 currently means an
    API error and a recent-harvest skip exits 0, so align its codes with this contract.
  - `t2-3po`: reports and proposals already go to folders; add the run report and a JSON form of
    each proposal and suggestion.
- **Stable, documented exit codes:** 0 success, 1 run failed, 2 bad config or usage, 3 skipped
  (blackout day, lock held).
- **One run at a time per droid**, with a lock file. A second start exits with code 3.
- **A shared Trading212 lock.** Rate limits are per account, so droids on independent timers can
  collide; `t2-d2`'s log already shows 429 responses. All Trading212 calls take a file lock at a
  shared path (for example `~/dev/apps/.t212.lock`) and honour the documented per-endpoint spacing.
- **Shared report models.** The pydantic models for run reports live in one place so the
  orchestrator and `t2-3po` parse exactly what the droids write. They start in `t2-hq` and move
  to `t2-core`.

## Part 2: deployment

Keep the pattern `t2-d2` already uses (a git checkout in `~/dev/apps`), but script it and pin it
to tags.

Each repo gets a `justfile` with:

- `just check`: the repo's existing tests and linters.
- `just release vX.Y.Z`: only from a clean `main`; tags and pushes.
- `just deploy [tag]`:
  1. clone into `~/dev/apps/<name>` if absent, otherwise `git fetch --tags`;
  2. check out the tag (detached), refusing if the app checkout has local changes;
  3. `uv sync --frozen --no-dev`;
  4. create `var/` if missing;
  5. install or refresh the systemd units and reload;
  6. print the deployed version and the next timer runs.
- `just undeploy`: disable the units. It leaves `.env` and `var/` alone.

The deploy script never creates, reads or edits `.env`. If `.env` is missing it says so and stops
before enabling timers.

## Part 3: scheduling

Schedules run on the Chromebook, with systemd timers, the way `t2-d2` runs today.

| Droid | Schedule | Notes |
|---|---|---|
| `t2-d2` | six sessions from `config/schedules.yaml` | **already installed**; keep as is, later generate the units from `just deploy` |
| `t2-k2` | weekly, Tuesday 22:00 Europe/London | per its README; dry run unless the unit passes `--execute` |
| `t2-bb8` | weekly, outside market hours | refreshes the instrument list |
| `t2-3po` | weekly strategy review, after `t2-bb8` | rate-limited model calls; keep it infrequent |
| `t2-hq` | long-running service | `Restart=on-failure` |

- **System units or user units.** `t2-d2`'s units are system-level and were installed with `sudo`.
  New droids can use the same pattern for consistency, or user units (lingering is already on),
  which `just deploy` could install without `sudo`. Recommendation: user units for new droids,
  and leave `t2-d2`'s alone until its next change.
- **Missed runs.** Timers only fire while the Chromebook is awake. That is accepted for now. A
  `status` check in `t2-hq` should say when a session was missed, so a gap is visible in Telegram
  instead of silent. Weekly jobs (`t2-k2`, `t2-bb8`, `t2-3po`) should use `Persistent=true` so
  they run at the next wake-up; the intraday `t2-d2` sessions should stay non-persistent.
- Timers use each market's time zone, as now.

## Part 4: `t2-3po` as strategy and configuration advisor

Today `t2-3po` evaluates a thesis (`evaluate`) and profiles each stock in `t2-d2`'s list against
a macro thesis (`review`). The new work adds a third job: review how the other droids are
*configured* and suggest changes.

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

In the deployed layout these are the sibling folders under `~/dev/apps`, so it reviews the
configuration that is running. Paths are settings with those defaults.

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
- **Memory of past advice.** Suggestions and Sam's answers are stored, so the council doesn't
  repeat a rejected idea every week and can later be judged against what happened.
- `t2-3po` writes suggestions and reports. It never edits another repo's files.

### New command

`t2-3po advise [--only d2|k2] [--dry-run]` writes `var/suggestions/<date>.json` and a markdown
report. It reuses the existing council (Bull, Bear, Quantitative Analyst, Protocol Officer).

## Part 5: the orchestrator (`t2-hq`)

One long-running service with three parts.

### LLM toggle

- Reuse `t2-3po`'s `build_model(settings)`, which already has `MODEL_PROVIDER=google|ollama`, free-tier
  pacing (`PacedModel`) and retry with backoff. No paid provider anywhere in the system.
- Use a fallback chain so a rate-limited Gemini call falls back to Ollama.
- **What to expect from Ollama on this machine:** with 6.3 GB RAM and no GPU, only a model of
  about 4B parameters fits. It will be slow and unreliable at calling tools. Use Gemini Flash for
  the agent that calls tools; keep Ollama for offline fallback and simple summaries.
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
  - **Sam asks:** free text goes to the agent ("what did d2 do today?", "why hasn't RR been
    bought?", "dry-run k2").
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
3. Sam answers per suggestion: open a PR, skip, or ask a follow-up question.
4. For "open a PR", code (not the LLM) applies the stored suggestion to the YAML on a
   `task/3po-<id>` branch of the dev repo, runs that repo's config validation and tests, pushes,
   and opens a pull request. The LLM never writes the file content.
5. Sam reviews and merges. The change is live after the next `just deploy`.

Fixed multi-step jobs, such as "harvest, then advise, then send the digest", are plain code or a
graph, with the LLM used only for summarising and for the conversation.

**Untrusted text.** `t2-3po` reads documents and Yahoo data. Small free models are easy to steer
with instructions hidden in such text, so that content never reaches the tool-calling agent
directly; it sees structured report and suggestion fields only.

### Secrets for the orchestrator

Its `.env` holds the Telegram token and chat ID, the Gemini and Ollama settings, and a GitHub
token limited to opening pull requests on the `t2-*` repos. It holds no Trading212 key.

## Part 6: secrets layout

Sam manages these; this is a recommendation only.

- One `.env` per app instead of one shared live file.
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
2. **Contract and deploy.** JSON reports and exit codes in each droid; `justfile` deploy scripts;
   systemd units; merge the open feature branches; deploy `t2-k2`, `t2-bb8` and `t2-3po`; align on
   Python 3.12.
3. **`t2-3po advise`.** Config readers, suggestion models, validation, the new command. Usable
   from the command line before the orchestrator exists.
4. **Orchestrator, read-only.** `t2-hq` with the LLM toggle, Telegram questions and answers,
   `status`, `last_report`, `dry_run`, and the advice digest.
5. **Suggestion to pull request.**
6. **Shared library.**

## Decisions

Made by Sam on 2026-11-15:

- **Schedules run on the Chromebook** for now.
- **No Telegram approval is needed to place orders.** The droids trade on their own rules.
- **`t2-3po` reviews strategy and configuration** across the other repos and advises changes.
- **The hub repo is `t2-hq`.**
- **`t2-bb8` keeps harvesting into git**, conservatively, so the shared key isn't rate limited.
- **`t2-3po` doesn't use Claude.** It runs on Gemini's free tier or Ollama, like the orchestrator.

Still open:

1. When Sam accepts a suggestion in Telegram, should `t2-hq` open a pull request (Part 5), or
   only report and leave the edit to Sam? The plan assumes a pull request.
2. Should `t2-bb8` keep committing harvested data to git in the dev repo?
3. Is `t2-hq` public like the other repos? It was created private.
