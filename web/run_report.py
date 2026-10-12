#!/usr/bin/env python3
"""Handle one request from the upload site. Runs as root, started by systemd.

journalens-report.path fires when the web process (an unprivileged user) drops
request.json into the spool. This feeds the uploaded export, if there is one, to
`journalens.py extract`, runs `journalens.py report`, and copies the result to
where the web process can serve it.

Everything in the spool is untrusted: it is written by the internet-facing
process. The upload is only ever parsed as a zip of JSON, and the topics only
ever reach the analyst as report topics, inside the sandbox journalens.py sets up.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

JOURNALENS = [sys.executable, str(Path(__file__).resolve().parent.parent / "journalens.py")]
STATE = Path(os.environ.get("JOURNALENS_STATE", "/var/lib/journalens"))
SPOOL = STATE / "spool"
WEB_GROUP = "journalens-web"


def publish(path, text):
    """Put a file where the web process can read it (and nobody else)."""
    target = STATE / path
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    shutil.chown(tmp, group=WEB_GROUP)
    tmp.chmod(0o640)
    os.replace(tmp, target)


def set_status(**fields):
    publish("status.json", json.dumps(fields))


def regular_file(path):
    return path.is_file() and not path.is_symlink()


def main():
    request = SPOOL / "request.json"
    upload = SPOOL / "upload.zip"
    started = time.time()
    topics = ""
    try:
        if regular_file(request) and request.stat().st_size < 100_000:
            topics = str(json.loads(request.read_text()).get("topics", ""))
    except (ValueError, AttributeError):
        pass
    finally:
        # Mark it running before the request disappears, so the site never sees a gap.
        set_status(state="running", step="starting", started=started)
        request.unlink(missing_ok=True)
    topics = " ".join(topics.split())[:2000]
    extracted = False

    try:
        if upload.exists():
            set_status(state="running", step="reading the export", started=started)
            if not regular_file(upload):
                raise RuntimeError("The upload is not a regular file.")
            done = subprocess.run(
                JOURNALENS + ["extract", str(upload)], capture_output=True, text=True, timeout=600
            )
            if done.returncode:
                # The script's own message; it names the problem, not journal text.
                raise RuntimeError(done.stderr.strip().splitlines()[-1].split(": ", 1)[-1])
            extracted = True

        set_status(state="running", step="writing the report", started=started)
        done = subprocess.run(
            JOURNALENS + ["report", f"--topics={topics}"], stdout=subprocess.PIPE, text=True
        )
        if done.returncode:
            raise RuntimeError("The report could not be written: no journal yet, or Claude Code failed.")
        report = Path(done.stdout.strip().splitlines()[-1])
        for path in report.parent.glob("*.md"):
            publish(f"reports/{path.name}", path.read_text(encoding="utf-8"))
        set_status(state="done", report=report.name)
    except subprocess.TimeoutExpired:
        set_status(state="error", error="Timed out.")
    except Exception as e:  # whatever went wrong, the site needs to hear about it
        print(f"run failed: {e!r}", file=sys.stderr)
        set_status(state="error", error=str(e) if isinstance(e, RuntimeError) else "Internal error; see the journal.")
    finally:
        # A failed run keeps its upload (root only, one at a time) so it can be retried
        # by hand without another large upload; a successful one has no further use for it.
        failed = STATE / "failed-upload.zip"
        failed.unlink(missing_ok=True)
        if upload.exists() and regular_file(upload) and not extracted:
            shutil.move(upload, failed)
            failed.chmod(0o600)
        upload.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
