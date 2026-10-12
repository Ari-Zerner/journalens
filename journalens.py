#!/usr/bin/env python3
"""JournaLens: advice from your journal, written by a headless Claude Code.

    journalens.py init                  # create the workspace (extract does this too)
    journalens.py extract <export>...   # Day One .zip/.json, or plain .md/.txt
    journalens.py plan                  # re-tier for today and show what to read
    journalens.py report [--topics T]   # have Claude Code write today's report

Nothing private lives in this repo. The journal and the reports are kept in a
separate workspace (see workspace_dir below), which is
also where Claude Code runs, under analyst/CLAUDE.md rather than this repo's:

    CLAUDE.md                instructions for sessions run here, copied from analyst/
    .claude/settings.json    what a conversation here may not do, likewise
    reports/2026-10-12.md    finished reports
    traces/2026-10-12.log    what the analyst did on that run (no journal text)
    feedback/2026-10-12.md   the author's answers and notes on that report
    journal/
      entries.jsonl          normalized entries; source of truth for `plan`
      recent.md              the last ~2 weeks, read in full
      weekly/2026-W38.md     older than that, up to ~90 days, one file per ISO week
      monthly/2026-05.md     everything older, one file per calendar month
      undated/<file>         plain-text sources, which carry no dates to tier by
      context.md             standing facts, maintained by the analyst
      summaries/{weekly,monthly}/<same name>.md   written by the analyst

Tier boundaries snap to whole weeks and whole months, so a weekly or monthly file
only changes when the journal itself does. That is what lets a summary be reused:
it is current when it is newer than the file it summarizes.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent


def workspace_dir():
    """$JOURNALENS_HOME, else the path in ~/.config/journalens/home, else a default.

    The config file is for machines where services, shells and Claude Code sessions
    all need to agree on the workspace without sharing an environment.
    """
    configured = Path.home() / ".config/journalens/home"
    chosen = os.environ.get("JOURNALENS_HOME") or (
        configured.read_text().strip() if configured.exists() else ""
    )
    return Path(chosen or Path.home() / ".local/share/journalens").expanduser().resolve()


HOME = workspace_dir()
JOURNAL = HOME / "journal"
REPORTS = HOME / "reports"
ENTRIES = JOURNAL / "entries.jsonl"

RECENT_DAYS = 14
WEEKLY_DAYS = 90

CLAUDE = os.environ.get("JOURNALENS_CLAUDE", "claude")
MODEL = os.environ.get("JOURNALENS_MODEL", "opus")
REPORT_TIMEOUT = 100 * 60  # a first run over years of entries has dozens of summaries to write
# What the analyst may do. It gets file tools and subagents only: no shell, no web,
# no MCP connectors, none of the user's own settings or allowlist. dontAsk then
# denies whatever ALLOWED_TOOLS does not list, so it can read its workspace, write
# summaries, context and reports, and nothing else.
SANDBOX = ["--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk"]
SANDBOX += ["--tools", "Read,Write,Edit,Glob,Grep,Agent"]
ALLOWED_TOOLS = [
    f"Read(/{HOME}/**)",
    f"Edit(/{HOME}/journal/summaries/**)",
    f"Edit(/{HOME}/journal/context.md)",
    f"Edit(/{HOME}/reports/**)",
    "Glob",
    "Grep",
    "Agent",
]

# Day One inlines attachments as markdown images and backslash-escapes punctuation.
MEDIA = re.compile(r"!\[[^\]]*\]\(dayone-moment:[^)]*\)\n?")
ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")


def normalize(raw, journal):
    """Day One entry -> our entry dict, or None if it has no date or no text."""
    text = ESCAPED.sub(r"\1", MEDIA.sub("", raw.get("text") or "")).strip()
    try:
        when = datetime.fromisoformat(raw["creationDate"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None
    if not text:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        # Local time, so a late-night entry stays on the day it was written.
        when = when.astimezone(ZoneInfo(raw["timeZone"]))
    except (KeyError, TypeError, ValueError, OSError):
        pass
    loc = raw.get("location") or {}
    location = loc.get("address") or ", ".join(
        filter(None, [loc.get("placeName"), loc.get("localityName")])
    )
    return {
        # Not strftime: it does not zero-pad years before 1000, and a mistyped
        # entry date (year 25 for 2025) is enough to meet one.
        "when": when.isoformat(sep=" ", timespec="minutes")[:16],
        "journal": journal,
        "location": location or None,
        "tags": raw.get("tags") or [],
        "text": text,
    }


def day_one_entries(raw, journal):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("not a Day One export (no entries array)")
    return [e for e in (normalize(r, journal) for r in data["entries"]) if e]


def read_export(path):
    """Return (entries, undated) where undated is {filename: text}."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        entries = []
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json")]
            if not names:
                raise ValueError("no JSON files in the archive")
            for name in names:
                try:
                    entries += day_one_entries(z.read(name), Path(name).stem)
                except ValueError as e:
                    print(f"skipping {name}: {e}", file=sys.stderr)
        return entries, {}
    if suffix == ".json":
        return day_one_entries(path.read_bytes(), path.stem), {}
    return [], {path.name: path.read_text(encoding="utf-8", errors="replace")}


def set_up_workspace():
    """Create the workspace and refresh the files that come from analyst/."""
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("journal", "reports", "traces", "feedback", ".claude"):
        (HOME / name).mkdir(exist_ok=True)
    instructions = (REPO / "analyst" / "CLAUDE.md").read_text()
    (HOME / "CLAUDE.md").write_text(instructions.replace("{journalens}", str(REPO / "journalens.py")))
    shutil.copyfile(REPO / "analyst" / "settings.json", HOME / ".claude" / "settings.json")


def extract(paths):
    entries, undated = [], {}
    for path in paths:
        try:
            found, text = read_export(path)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            sys.exit(f"{path}: {e}")
        entries += found
        undated.update(text)
    if not entries and not undated:
        sys.exit("No journal entries found.")
    if len({e["journal"] for e in entries}) < 2:
        for e in entries:
            e["journal"] = None
    entries.sort(key=lambda e: e["when"])

    set_up_workspace()
    ENTRIES.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
    sync(JOURNAL / "undated", undated, "*")


def render(title, entries):
    parts = [f"# {title}\n"]
    for e in entries:
        day = date.fromisoformat(e["when"][:10])
        meta = " · ".join(
            filter(
                None,
                [
                    e["location"],
                    e["journal"] and f"journal: {e['journal']}",
                    e["tags"] and "tags: " + ", ".join(e["tags"]),
                ],
            )
        )
        head = f"## {day} {day:%a} {e['when'][11:]}\n"
        parts.append(head + (f"*{meta}*\n" if meta else "") + f"\n{e['text']}\n")
    return "\n".join(parts)


def sync(directory, files, pattern="*.md"):
    """Make directory hold exactly `files`, touching only what changed."""
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob(pattern):
        if old.name not in files:
            old.unlink()
    for name, content in files.items():
        path = directory / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")


def tier(entries, today):
    """Split entries into recent / {week: entries} / {month: entries}."""
    recent_start = today - timedelta(days=RECENT_DAYS)
    recent_start -= timedelta(days=recent_start.weekday())  # back to Monday
    monthly_end = (today - timedelta(days=WEEKLY_DAYS)).replace(day=1)

    recent, weekly, monthly = [], {}, {}
    for e in entries:
        day = date.fromisoformat(e["when"][:10])
        if day >= recent_start:
            recent.append(e)
        elif day >= monthly_end:
            year, week, _ = day.isocalendar()
            weekly.setdefault(f"{year}-W{week:02d}", []).append(e)
        else:
            monthly.setdefault(e["when"][:7], []).append(e)
    return recent, weekly, monthly


def describe(path):
    text = path.read_text(encoding="utf-8")
    size = f"{len(text.split())} words"
    if path.suffix == ".md" and path.parent.name != "undated":
        size = f"{text.count(chr(10) + '## ')} entries, {size}"
    return f"{path.relative_to(HOME)}  ({size})"


def plan(today):
    """Write the tier files for `today` and return the reading plan as text."""
    if not ENTRIES.exists():
        sys.exit("No journal extracted yet. Run: journalens.py extract <export>")
    # One entry per "\n"-terminated line. Not splitlines(): that also breaks on
    # U+2028 and other separators that entry text can contain.
    with open(ENTRIES, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f]
    recent, weekly, monthly = tier(entries, today)

    recent_path = JOURNAL / "recent.md"
    if recent:
        sync(JOURNAL, {"recent.md": render("Recent journal entries", recent)}, "recent.md")
    else:
        recent_path.unlink(missing_ok=True)
    for kind, groups in (("weekly", weekly), ("monthly", monthly)):
        sync(
            JOURNAL / kind,
            {f"{key}.md": render(f"Journal entries, {key}", es) for key, es in groups.items()},
        )

    out = []
    if entries:
        out.append(
            f"Journal: {len(entries)} entries, {entries[0]['when'][:10]} to "
            f"{entries[-1]['when'][:10]} (as of {today})"
        )
    out.append("\nRead in full:")
    undated = sorted((JOURNAL / "undated").glob("*"))
    for path in ([recent_path] if recent else []) + undated:
        out.append(f"  {describe(path)}")
    if not recent:
        out.append(f"  (no dated entries since {today - timedelta(days=RECENT_DAYS)})")

    todo, current = [], []
    for kind in ("monthly", "weekly"):
        for source in sorted((JOURNAL / kind).glob("*.md")):
            summary = JOURNAL / "summaries" / kind / source.name
            if summary.exists() and summary.stat().st_mtime >= source.stat().st_mtime:
                current.append(summary)
            else:
                todo.append((source, summary))
    out.append("\nSummaries to write (source -> summary):")
    for source, summary in todo:
        out.append(f"  {describe(source)} -> {summary.relative_to(HOME)}")
    out.append("\nSummaries up to date, oldest first:")
    for summary in current:
        out.append(f"  {summary.relative_to(HOME)}")
    return "\n".join(out)


def trace_lines(event, agents):
    """Trace lines for one stream-json event: who did what to which file.

    Only tool names, file paths, sizes, token counts and timings are taken from the
    event. Prompts, search patterns, file contents and the model's own text can all
    carry journal content and are never copied. `agents` maps a subagent's
    tool-use id to its label and is filled in as subagents are spawned.
    """
    kind, subtype = event.get("type"), event.get("subtype")
    who = agents.get(event.get("parent_tool_use_id"), "main")
    lines = []
    if kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input", {})
            if name == "Agent":
                label = agents[block.get("id")] = f"agent{len(agents) + 1}"
                brief = str(args.get("prompt", ""))
                files = sorted(set(re.findall(r"journal/[\w/-]+\.md", brief)))
                detail = (
                    f"-> {label} model={args.get('model', 'inherit')} "
                    f"brief={len(brief)} chars, names {len(files)} files: {' '.join(files) or '-'}"
                )
            else:
                path = str(args.get("file_path") or args.get("path") or "")
                detail = path.removeprefix(f"{HOME}/")
                if "content" in args:
                    detail += f" ({len(str(args['content']))} chars)"
            lines.append(f"{who} {name} {detail}".rstrip())
    elif kind == "system" and subtype == "permission_denied":
        lines.append(f"DENIED {event.get('tool_name')}")
    elif kind == "system" and subtype == "task_notification":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        counts = " ".join(f"{k}={v}" for k, v in usage.items() if isinstance(v, (int, float)))
        label = agents.get(event.get("tool_use_id"), "agent?")
        lines.append(f"{label} finished: {event.get('status')} {counts}".rstrip())
    elif kind == "result":
        stats = event.get("subagent_stats") or {}
        lines.append(
            f"result: {subtype}, {event.get('num_turns')} turns, "
            f"{event.get('duration_ms', 0) / 1000:.0f}s, ${event.get('total_cost_usd', 0):.2f}, "
            f"subagents {stats.get('completed', 0)}/{stats.get('spawned', 0)} completed, "
            f"{len(event.get('permission_denials') or [])} denials"
        )
        for model, use in (event.get("modelUsage") or {}).items():
            lines.append(
                f"  {model}: in={use.get('inputTokens')} out={use.get('outputTokens')} "
                f"cache_read={use.get('cacheReadInputTokens')} cache_write={use.get('cacheCreationInputTokens')} "
                f"${use.get('costUSD', 0):.2f}"
            )
    return lines


def report(today, topics):
    """Run the analyst: a headless Claude Code in the workspace. Prints the report's path."""
    reading_plan = plan(today)
    set_up_workspace()
    target, n = REPORTS / f"{today}.md", 2
    while target.exists():
        target, n = REPORTS / f"{today}-{n}.md", n + 1

    # Feedback the author has left since a report last read it. A run that succeeds
    # records what it was shown, so each note is handed over once.
    seen_path = HOME / "feedback" / ".seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    feedback = {p.name: p.stat().st_mtime for p in sorted((HOME / "feedback").glob("*.md"))}
    unread = [f"  feedback/{name}" for name, mtime in feedback.items() if mtime > seen.get(name, 0)]

    prompt = (
        (REPO / "analyst" / "report.md").read_text()
        + f"\n# This run\n\nToday is {today:%A, %B} {today.day}, {today.year}. Write today's report and save it "
        f"to {target.relative_to(HOME)}.\n\n"
        f"Custom topics: {topics or 'none'}\n\n"
        f"Author's feedback to read:\n{chr(10).join(unread) or '  (none)'}\n\n"
        f"Reading plan:\n\n{reading_plan}\n"
    )
    command = [CLAUDE, "-p", prompt, "--model", MODEL, *SANDBOX, "--allowedTools", *ALLOWED_TOOLS]
    command += ["--output-format", "stream-json", "--verbose"]
    proc = subprocess.Popen(command, cwd=HOME, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    watchdog = threading.Timer(REPORT_TIMEOUT, proc.kill)
    watchdog.start()

    # The event stream carries everything the analyst reads and writes, so it is
    # never stored or passed on; only the trace lines derived from it are.
    started, agents = time.monotonic(), {}
    with open(HOME / "traces" / f"{target.stem}.log", "w") as trace:

        def note(text):
            line = f"{time.monotonic() - started:7.1f}s {text}"
            print(line, file=trace, flush=True)
            print(line, file=sys.stderr, flush=True)

        note(f"start model={MODEL} report={target.relative_to(HOME)}")
        for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            for text in trace_lines(event, agents):
                note(text)
        code = proc.wait()
        watchdog.cancel()
        note(f"exit {code}, report {'written' if target.exists() else 'MISSING'}")
    if not target.exists():
        sys.exit(f"Claude Code exited {code} without writing {target}.")
    seen_path.write_text(json.dumps(feedback))
    print(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    for name in ("extract", "plan", "report"):
        p = sub.add_parser(name)
        if name == "extract":
            p.add_argument("exports", nargs="+", type=Path)
        if name == "report":
            p.add_argument("--topics", default="", help="topics or questions to address")
        p.add_argument(
            "--today",
            type=date.fromisoformat,
            default=date.today(),
            help="tier as of this date (YYYY-MM-DD) instead of today",
        )
    args = parser.parse_args()
    if args.command == "init":
        set_up_workspace()
        print(HOME)
        return
    if args.command == "extract":
        extract(args.exports)
    if args.command == "report":
        report(args.today, " ".join(args.topics.split()))
    else:
        print(plan(args.today))


if __name__ == "__main__":
    main()
