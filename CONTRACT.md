# The T2 droid contract

Every droid behaves the same way at the edges, so systemd timers, the operator, the orchestrator
and `t2-3po` can run any droid and read the outcome without knowing its internals.

The reference implementation is [`contract/t2contract.py`](contract/t2contract.py): standard
library only, vendored unchanged into each droid. Contract version: **1**.

| Droid | Vendored copy |
|---|---|
| `t2-d2` | `services/scheduler/src/scheduler/t2contract.py` |
| `t2-k2` | `src/t2k2/t2contract.py` |
| `t2-bb8` | `src/t2bb8/t2contract.py` |
| `t2-3po` | `src/t2_3po/t2contract.py` |
| `t2-r2` | `src/t2r2/t2contract.py` |

To change it, edit the reference here, run its tests, then copy it into every droid and check:

```bash
uv run --no-project --python 3.12 --with pytest pytest contract
bin/t2-contract sync      # copy into each droid checkout in $T2_SRC
bin/t2-contract check     # exit 1 if any copy differs
```

Then commit the copy in each droid. Droids never edit their copy.

## Exit codes

| Code | Status | Meaning |
|---|---|---|
| `0` | `ok` | The run did its job. |
| `1` | `failed` | The run failed or partly failed: an API error, a failed order, a crash. |
| `2` | `config_error` | Configuration or usage error: a missing key, a bad config file, an unknown argument. Retrying won't help. |
| `3` | `skipped` | Nothing to do, or not allowed now: a blackout date, a recent harvest, a lock held. Not a failure. |

systemd units set `SuccessExitStatus=3`, so a skipped run doesn't show as a failed unit.

## Run report

Every run writes one JSON file, including runs that fail, skip or crash:

- **Location:** `var/runs/<UTC start, YYYYMMDDTHHMMSSZ>-<command>.json` under the working
  directory. `T2_RUNS_DIR` changes the directory; a droid's `--report PATH` sets the exact file.
- **Shape:** [`contract/run-report.schema.json`](contract/run-report.schema.json).

```json
{
  "schema": "t2.run-report/1",
  "droid": "t2-k2",
  "version": "0.2.0",
  "command": "harvest",
  "argv": [],
  "started_at": "2026-11-17T22:00:01.102Z",
  "finished_at": "2026-11-17T22:00:47.881Z",
  "duration_seconds": 46.779,
  "dry_run": true,
  "status": "ok",
  "exit_code": 0,
  "summary": "14 holdings checked, 2 sell orders simulated",
  "result": { "...": "droid-specific; see the droid's README" },
  "errors": []
}
```

`dry_run` is `null` for droids that never trade (`t2-bb8`, `t2-3po`, `t2-r2`). `errors` never holds
secrets: crashes are recorded by exception type only.

## Locks

Locks are `flock` files in `/run/user/<uid>/t2` (or `$T2_LOCK_DIR`). The directory is derived
from the user ID, not from `XDG_RUNTIME_DIR`, so system services running as the same user share
it. A lock is released when its process exits, however it exits.

- **Run lock, per droid** (`run_lock(droid)`): one run of a droid at a time. A second run exits
  `3` immediately.
- **Trading212 account lock, shared** (`t212_lock()`): rate limits apply per account, so droids
  take turns using it. A run waits up to `T2_T212_LOCK_TIMEOUT` seconds (default 600), then exits
  `3`. Droids hold it only while they use the account: `t2-d2` for a session, `t2-k2` for a
  harvest, `t2-bb8` for its two requests, `t2-3po` while fetching the portfolio, `t2-r2` while reading order history.

## Deployment

Each droid ships systemd unit templates in `deploy/systemd/`, with `@APP_DIR@` for the deployed
app directory. A droid whose units depend on its configuration ships `deploy/systemd/render.py`
instead, which `bin/t2-deploy` runs with the app's Python as `render.py OUT_DIR APP_DIR`.

Unit names start with the droid's name without hyphens (`t2d2`, `t2k2`, `t2bb8`), except
`t2-3po`. `bin/t2-deploy` uses that prefix to find, replace and remove a droid's units.
