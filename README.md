# JournaLens

Thoughtful insights from your journal, written by [Claude Code](https://claude.com/claude-code).

A script turns your journal export into tiered files, then starts a headless Claude Code
in a private workspace to read them and write an advice report. There is no API key and
nothing to install beyond Python 3 and a signed-in Claude Code.

## Usage

```
python3 journalens.py extract MyJournal.zip
python3 journalens.py report --topics "should I take the project lead role?"
```

`extract` accepts Day One exports (`.zip` or `.json`), which are split by date, and plain
text (`.md`, `.txt`), which is read as one undated block. Each export is a full snapshot
that replaces the last. `report` prints the path of the finished report.

## Talking with your journal

The workspace is also a place to open Claude Code and ask questions: `python3
journalens.py init` prints its path. Sessions there get instructions for conversing
about the journal (`analyst/CLAUDE.md`) and have web and connector tools removed.

## How it works

Entries are sorted into tiers by age: the last two weeks or so in full, then one file per
week back to about 90 days, then one file per month. The analyst reads the recent entries
directly and writes a summary of each older file, which it reuses on later runs until
that file changes. Each report also draws on the previous reports and on a context file
of standing facts that the analyst keeps up to date from your journal.

The analyst's instructions are `analyst/CLAUDE.md`. It runs with file tools only,
confined to the workspace: no shell, no web access, no connectors.

## Privacy

Your journal, the summaries, and the reports live in `$JOURNALENS_HOME` (default
`~/.local/share/journalens`), not in this repo. They go to Claude through your own Claude
Code login and nowhere else.

## Upload site (optional)

`web/` is a small site for doing the above from a phone: sign in with Google, upload the
export, read the report. On a report you can answer its context questions and leave a
note on any section; the next report reads them. The whole report, with your answers and
notes in place, can be copied or shared into your journal app. Only one Google address is let in. The web process runs as an
unprivileged user that cannot read the journal or Claude's credentials; it hands requests
to a root-side runner through a spool directory.

```
sudo web/install.sh journal.example.com you@example.com
```

That installs Caddy (for HTTPS), the services, and opens ports 80 and 443. Then point the
hostname's DNS at the machine, create a Google OAuth client with the redirect URI
`https://<hostname>/auth/callback`, and put its ID and secret in
`/etc/journalens/web.env`.

## Earlier version

JournaLens began as a Next.js web app that called the Claude API with your own key. That
version is preserved on the [`archive/web-app`](../../tree/archive/web-app) branch.
