# JournaLens

This repo is code only. The journal lives in a separate workspace (`$JOURNALENS_HOME`,
else the path in `~/.config/journalens/home`, else `~/.local/share/journalens`), and two
kinds of Claude Code session run *there*, not here:

- **Report runs:** headless and sandboxed, started by `journalens.py report`, following
  `analyst/report.md`.
- **Conversations with the author about their journal:** an ordinary interactive session
  in the workspace, following `analyst/CLAUDE.md` and limited by `analyst/settings.json`.

A session working here is a developer of that pipeline, not the analyst: to get a
report, run the script, don't write one yourself. The original Next.js app that called
the Claude API is on the `archive/web-app` branch.

| Path | What |
|---|---|
| `journalens.py` | `extract` an export into tiered files, `plan` the reading, `report` via the analyst. Standard-library Python only; keep it that way. Its docstring has the workspace layout. |
| `analyst/` | What sessions in the workspace are told and allowed: `CLAUDE.md` (the workspace's own, written for conversations), `report.md` (the report procedure, sent as the prompt of a report run), `settings.json` (deny rules for conversations: no web, no connectors). `init`, `extract` and `report` copy these into the workspace, so edit them here. |
| `web/` | Optional upload site: `server.py` (unprivileged, behind Caddy, Google sign-in for one address), `run_report.py` (root, started by a systemd path unit when the site queues a request), `install.sh`. |
| `test-data/` | A fictional journal, safe to commit and to test with. |

## The journal is private

- Never copy journal text, summaries, or reports into this repo, a commit message, or
  memory. Don't read the workspace unless the task needs it; test with `test-data/` and
  a scratch `JOURNALENS_HOME`.
- The analyst's sandbox is the `SANDBOX` and `ALLOWED_TOOLS` lists in `journalens.py`:
  file tools only, confined to the workspace, no shell, web, or MCP. If you change them,
  re-probe by asking a run with the same flags to read a file outside the workspace and
  to run a shell command; both must fail. Without `--tools`, harmless-looking shell
  commands are still allowed even in `dontAsk` mode.
- A conversation session is only guarded, not sandboxed: `analyst/settings.json` removes
  web and connector tools, but it still has a shell under the normal permission mode.
  Report runs are the sandboxed ones.
- Each run leaves a trace in the workspace's `traces/` and on stderr (so in
  `journalctl -u journalens-report` for site runs): tool names, file paths, sizes,
  tokens, cost. `trace_lines` in `journalens.py` must never copy prompts, search
  patterns, file contents, or model text, all of which can carry journal content.
- The web process must stay unable to read the workspace or Claude's credentials. It
  talks to the root side only through `/var/lib/journalens/spool` (requests in) and
  `status.json` and `reports/` (results out). Treat everything in the spool as
  untrusted input.

## Deploying the site

`web/install.sh <hostname> <email>` is idempotent; re-run it after changing anything in
`web/` (the server is copied to `/opt/journalens-web`) or after Claude Code moves (the
runner's unit pins its path). `run_report.py` and `journalens.py` run from this checkout,
so a broken working tree breaks the live site.
