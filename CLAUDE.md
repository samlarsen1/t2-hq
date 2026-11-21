# T2-HQ

Hub repo for the T2 droid system: the plan ([PLAN.md](PLAN.md)), the architecture
([ARCHITECTURE.md](ARCHITECTURE.md)), the droid contract ([CONTRACT.md](CONTRACT.md)), the deploy
tool (`bin/t2-deploy`) and the orchestrator: uv project (Python 3.12), `src/t2hq`, command `t2-hq`.

```bash
uv run pytest && uv run ruff check . && uv run mypy      # before every commit
```

## Sibling repos

Checked out side by side in one development directory, and deployed side by side in one apps
directory:

- `../t2-d2`: dip buyer (places buys)
- `../t2-k2`: gain harvester (places sells)
- `../t2-bb8`: instrument harvester (metadata only)
- `../t2-3po`: advisory council (read-only)
- `../t2-r2`: trade ledger (read-only Trading212 key)
- `../t2-core`: shared library (the price store); a library, not a droid

## Rules

- **The orchestrator is read-only.** Every agent tool is in `agent.py` and only reads, or runs a
  dry run through `Droids.dry_run`, which builds the command in code and refuses
  `FORBIDDEN_ARGS`. Never add a tool, command or button that trades, edits a droid's files, or
  passes text from the model into a command line.
- **Pull requests are code-written and operator-started.** `prs.py` applies a stored suggestion
  through `configedit.py`, whose every edit is verified against the parsed YAML. Only the
  Open PR button or `t2-hq open-pr` starts it: never give the agent a tool that pushes or opens
  pull requests, and never let model output reach a file or a git command.
- **Never log or raise the Telegram token.** It is in every Bot API URL, so `telegram.py` errors
  carry no URL or request, and the `httpx` loggers stay at WARNING.
- **The orchestrator never places orders** and holds no Trading212 key. Trading stays in
  `t2-d2` and `t2-k2`, driven by their own timers and YAML configuration.
- **Advice is never applied automatically.** `t2-3po` suggestions reach the operator through
  Telegram; a configuration change lands only as a pull request that the operator merges.
- **Never create, read, edit, copy or link `.env` files.** Only write `.env.example` with placeholders.
- **Keep the documents generic.** No personal names, machine names, home-directory paths or
  secrets-file locations; use `$T2_SRC` and `$T2_APPS` for the two directories.
- **Droids are called as subprocesses**, never imported. Read their JSON run reports.
- **The contract lives here.** Change `contract/t2contract.py` (with its tests and
  `CONTRACT.md`), then `bin/t2-contract sync` and commit the copy in every droid. Keep it
  standard-library only and backwards compatible, or bump `CONTRACT_VERSION` and the schema.
- **`bin/t2-deploy` must stay safe:** never touch `.env`, never install user units next to a
  droid's system units, never deploy over local changes. Test it against scratch remotes with
  `T2_UNIT_DIR` and `T2_SYSTEMCTL`, not against the real units.
- **Keep the documents current.** A change to how the droids interact updates `ARCHITECTURE.md`
  and `PLAN.md` in the same commit. A change to the droid contract is made here first.
- Git: commit only on `feature/<name>`, `bug/<name>`, `task/<name>` or `hotfix/<name>` branches;
  never directly on `main`.
