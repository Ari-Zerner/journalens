#!/usr/bin/env python3
"""Copy the author's answers and notes from the upload site into the workspace.

Runs as root, started by journalens-feedback.path when the web process touches
notes.changed. The site keeps one JSON file per report in the spool; each becomes
feedback/<report>.md in the workspace, which the next report run is told to read.

The spool is untrusted. A question or section is kept only if it really appears in
the report it claims to be about, and nothing here is executed or interpreted: the
text is written out as the author's words for the analyst to read.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journalens  # noqa: E402

SPOOL = Path(os.environ.get("JOURNALENS_STATE", "/var/lib/journalens")) / "spool"
NAME = re.compile(r"\d{4}-\d{2}-\d{2}(-\d+)?")


def render(stem, data, report):
    """Markdown for one report's feedback, or "" if there is none worth keeping."""

    def items(key, field):
        found = data.get(key) if isinstance(data.get(key), list) else []
        for item in found[:80]:
            if isinstance(item, dict):
                label, text = str(item.get(field, ""))[:500].strip(), str(item.get("text", ""))[:5000].strip()
                if label and text and label in report:
                    yield label, text

    parts = []
    answers = [f"**Q:** {q}\n\n**A:** {a}" for q, a in items("answers", "question")]
    if answers:
        parts.append("## Answers to the context questions\n\n" + "\n\n".join(answers))
    parts += [f"## Note on \"{section}\"\n\n{note}" for section, note in items("notes", "section")]
    general = str(data.get("general", ""))[:5000].strip()
    if general:
        parts.append(f"## Anything else\n\n{general}")
    if not parts:
        return ""
    return f"# The author's answers and notes on the report of {stem}\n\n" + "\n\n".join(parts) + "\n"


def main():
    (SPOOL / "notes.changed").unlink(missing_ok=True)
    feedback = journalens.HOME / "feedback"
    for source in sorted((SPOOL / "notes").glob("*.json")):
        report = journalens.REPORTS / f"{source.stem}.md"
        if not NAME.fullmatch(source.stem) or source.is_symlink() or not report.is_file():
            continue
        if source.stat().st_size > 400_000:
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            text = render(source.stem, data if isinstance(data, dict) else {}, report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        target = feedback / f"{source.stem}.md"
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if text == current:
            continue  # untouched files keep their mtime, which is how a run knows what is new
        feedback.mkdir(exist_ok=True)
        if text:
            target.write_text(text, encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        print(f"feedback for {source.stem}: {len(text)} chars")


if __name__ == "__main__":
    main()
