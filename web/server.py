#!/usr/bin/env python3
"""JournaLens upload site: sign in with Google, upload an export, read reports.

Runs as an unprivileged user behind Caddy and never touches the journal or Claude
Code. All it can do is drop an upload and a request into the spool directory; a
root-owned systemd path unit picks the request up and runs web/run_report.py, which
publishes the status and finished reports back for this process to serve.

Configuration comes from the environment (/etc/journalens/web.env):
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET, ALLOWED_EMAIL, PUBLIC_URL.
"""

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from base64 import urlsafe_b64decode
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
SECRET = os.environ["SESSION_SECRET"].encode()
ALLOWED_EMAIL = os.environ["ALLOWED_EMAIL"].lower()
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "8787"))

STATE = Path(os.environ.get("JOURNALENS_STATE", "/var/lib/journalens"))
SPOOL = STATE / "spool"
REPORTS = STATE / "reports"
STATUS = STATE / "status.json"
STATIC = Path(__file__).resolve().parent

MAX_UPLOAD = 2 * 1024**3
MAX_TOPICS = 2000
SESSION_SECONDS = 30 * 24 * 3600
STALE_SECONDS = 125 * 60  # the report unit is stopped by systemd before this
REPORT_NAME = re.compile(r"\d{4}-\d{2}-\d{2}(-\d+)?\.md")
# The author's answers and notes on each report, as the site last saved them. This
# process keeps them here to refill the form; the root side copies them into the
# workspace when notes.changed appears.
NOTES = SPOOL / "notes"
MAX_FEEDBACK = 200_000

CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def sign(payload):
    mac = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def unsign(token, kind):
    """Return the fields of a signed, unexpired `kind.expiry.fields...` token."""
    payload, _, _ = token.rpartition(".")
    if not hmac.compare_digest(sign(payload), token):
        return None
    parts = payload.split(".")
    if parts[0] != kind or not parts[1].isdigit() or int(parts[1]) < time.time():
        return None
    return parts[2:]


def google_email(code):
    """Exchange an authorization code for the verified email it belongs to."""
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": PUBLIC_URL + "/auth/callback",
            "grant_type": "authorization_code",
        }
    ).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, timeout=15) as r:
        token = json.load(r)["id_token"]
    # The ID token came straight from Google over TLS, which is what authenticates
    # it (OpenID Connect Core 3.1.3.7), so the claims are read without checking
    # the signature.
    segment = token.split(".")[1]
    claims = json.loads(urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    if (
        claims.get("aud") != CLIENT_ID
        or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com")
        or claims.get("exp", 0) < time.time()
        or claims.get("email_verified") is not True
    ):
        return None
    return str(claims.get("email", "")).lower()


def status():
    if (SPOOL / "request.json").exists():
        return {"state": "queued"}
    try:
        current = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}
    if current.get("state") == "running" and time.time() - current.get("started", 0) > STALE_SECONDS:
        return {"state": "error", "error": "The last run never finished."}
    return current


def busy():
    return status()["state"] in ("queued", "running")


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", text)
    return re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)\"]+)\)",
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        text,
    )


def render_markdown(text):
    """Markdown as reports use it -> HTML. Everything is escaped; no raw HTML."""
    out, para, lists, fence = [], [], [], None

    def close_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_lists(indent=-1):
        while lists and lists[-1][0] > indent:
            out.append(f"</li></{lists.pop()[1]}>")

    for line in text.splitlines():
        if fence is not None:
            if line.strip().startswith("```"):
                out.append("<pre>" + html.escape("\n".join(fence)) + "</pre>")
                fence = None
            else:
                fence.append(line)
            continue
        stripped = line.strip()
        item = re.match(r"(\s*)([-*+]|\d+[.)])\s+(.*)", line)
        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if stripped.startswith("```"):
            close_para(), close_lists()
            fence = []
        elif not stripped:
            close_para()
        elif heading:
            close_para(), close_lists()
            level = len(heading[1])
            out.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif re.fullmatch(r"([-*_])(\s*\1){2,}", stripped):
            close_para(), close_lists()
            out.append("<hr>")
        elif item:
            close_para()
            indent, tag = len(item[1]), "ul" if item[2] in "-*+" else "ol"
            close_lists(indent)
            if lists and lists[-1][0] == indent:
                out.append("</li><li>")
            else:
                lists.append((indent, tag))
                out.append(f"<{tag}><li>")
            out.append(inline(item[3]))
        elif lists and line.startswith(" ") and not para:
            out.append(" " + inline(stripped))
        else:
            close_lists()
            para.append(inline(stripped.lstrip("> ") if stripped.startswith(">") else stripped))
    if fence is not None:
        out.append("<pre>" + html.escape("\n".join(fence)) + "</pre>")
    close_para(), close_lists()
    return "\n".join(out)


def split_sections(text):
    """A report as [(level-2 heading or None, body)], in order."""
    sections, heading, body = [], None, []
    for line in text.splitlines():
        match = re.match(r"##\s+(.*)", line)
        if match:
            sections.append((heading, "\n".join(body)))
            heading, body = match[1].strip(), []
        else:
            body.append(line)
    return sections + [(heading, "\n".join(body))]


def split_questions(body):
    """A context section as (everything but its questions, [the questions]).

    Questions are the items of a list. Reports written before that was the format
    put each question on a line of its own, so failing a list, any line that ends
    in a question mark counts.
    """
    rest, questions, in_item = [], [], False
    for line in body.splitlines():
        item = re.match(r"(?:\d+[.)]|[-*+])\s+(.*)", line)
        if item:
            questions.append(item[1].strip())
            in_item = True
        elif in_item and line.startswith(" ") and line.strip():
            questions[-1] += " " + line.strip()
        else:
            in_item = False
            rest.append(line)
    if not questions:
        questions = [line.strip() for line in rest if line.rstrip().endswith("?")]
        rest = [line for line in rest if not line.rstrip().endswith("?")]
    return "\n".join(rest), questions


def clean_feedback(data):
    """Whatever the browser sent, cut down to the shape and sizes we store."""

    def items(key, field):
        found = data.get(key) if isinstance(data.get(key), list) else []
        return [
            {field: str(i.get(field, ""))[:500], "text": str(i.get("text", ""))[:5000]}
            for i in found[:80]
            if isinstance(i, dict) and str(i.get("text", "")).strip()
        ]

    return {
        "answers": items("answers", "question"),
        "notes": items("notes", "section"),
        "general": str(data.get("general", ""))[:5000],
    }


def report_page(name):
    """The report with an answer box under each context question and a note box per section."""
    try:
        saved = clean_feedback(json.loads((NOTES / f"{name[:-3]}.json").read_text()))
    except (OSError, ValueError, AttributeError):
        saved = clean_feedback({})
    answers = {a["question"]: a["text"] for a in saved["answers"]}
    notes = {n["section"]: n["text"] for n in saved["notes"]}

    def box(attribute, value, text, rows):
        value = html.escape(value, quote=True)
        return f'<textarea {attribute}="{value}" rows="{rows}">{html.escape(text)}</textarea>'

    # Each section also carries its Markdown source, from which report.js rebuilds
    # the report, answers and notes included, to copy or share into a journal app.
    out, title = [], ""
    for heading, body in split_sections((REPORTS / name).read_text(encoding="utf-8")):
        if heading is None:
            title = body.strip()
            out.append(render_markdown(body))
            continue
        questions = []
        if heading.lower().startswith("context for journalens"):
            body, questions = split_questions(body)
        out.append(
            f'<section data-heading="{html.escape(heading, quote=True)}" '
            f'data-md="{html.escape(body.strip(), quote=True)}">'
            f"<h2>{inline(heading)}</h2>{render_markdown(body)}"
        )
        for question in questions:
            out.append(
                f'<div class="question"><p>{inline(question)}</p>'
                + box("data-question", question, answers.get(question, ""), 2)
                + "</div>"
            )
        note = notes.get(heading, "")
        out.append(
            f'<details class="note"{" open" if note else ""}><summary>Note on this section</summary>'
            + box("data-section", heading, note, 3)
            + "</details></section>"
        )
    out.append(
        "<section><h2>Anything else</h2>"
        + box("data-general", "", saved["general"], 3)
        + '<div class="actions"><button id="save">Save answers and notes</button>'
        '<button id="copy" class="quiet">Copy for my journal</button>'
        '<button id="share" class="quiet" hidden>Share to my journal</button></div>'
        '<p id="saved" role="status">Saved answers and notes are read when the next report is written. '
        "Copy and Share take the whole report with your answers and notes in place.</p>"
        "</section>"
    )
    return (
        f'<p><a href="/">&larr; All reports</a></p>'
        f'<article data-report="{name}" data-title="{html.escape(title, quote=True)}">'
        + "\n".join(out)
        + '</article><script src="/report.js"></script>'
    )


def page(title, body):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="stylesheet" href="/style.css"></head>'
        f"<body><main>{body}</main></body></html>"
    )


LOGIN = """<h1>JournaLens</h1>
<p>Private. Sign in to continue.</p>
<p><a class="button" href="/auth/login">Sign in with Google</a></p>"""

HOME = """<header><h1>JournaLens</h1>
<form method="post" action="/auth/logout"><button class="quiet">Sign out</button></form></header>
<section>
<label for="file">Journal export (Day One .zip)</label>
<input type="file" id="file" accept=".zip,application/zip">
<label for="topics">Topics or questions for this report (optional)</label>
<textarea id="topics" rows="3" maxlength="2000"></textarea>
<div class="actions">
<button id="upload">Upload and write report</button>
<button id="rerun" class="quiet">Write report from the current journal</button>
</div>
<progress id="progress" max="1" value="0" hidden></progress>
<p id="status" role="status"></p>
</section>
<section><h2>Reports</h2>%s</section>
<script src="/app.js"></script>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "journalens"
    sys_version = ""
    timeout = 120

    def log_message(self, fmt, *args):
        # The default line includes the query string, which carries OAuth codes.
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, data):
        self.send(code, json.dumps(data), "application/json")

    def redirect(self, location, cookies=()):
        self.send(303, headers=[("Location", location)] + [("Set-Cookie", c) for c in cookies])

    def cookie(self, name):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return jar[name].value if name in jar else ""

    @staticmethod
    def set_cookie(name, value, max_age):
        return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"

    def signed_in(self):
        return unsign(self.cookie("__Host-session"), "s") is not None

    def same_origin(self):
        return self.headers.get("Origin") == PUBLIC_URL

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path

        if path in ("/style.css", "/app.js", "/report.js"):
            ctype = "text/css" if path.endswith(".css") else "text/javascript"
            return self.send(200, (STATIC / path[1:]).read_bytes(), ctype)

        if path == "/auth/login":
            nonce = secrets.token_urlsafe(24)
            state = sign(f"o.{int(time.time()) + 600}.{nonce}")
            params = urllib.parse.urlencode(
                {
                    "client_id": CLIENT_ID,
                    "redirect_uri": PUBLIC_URL + "/auth/callback",
                    "response_type": "code",
                    "scope": "openid email",
                    "state": nonce,
                    "login_hint": ALLOWED_EMAIL,
                }
            )
            return self.redirect(
                "https://accounts.google.com/o/oauth2/v2/auth?" + params,
                [self.set_cookie("__Host-oauth", state, 600)],
            )

        if path == "/auth/callback":
            expected = unsign(self.cookie("__Host-oauth"), "o")
            given = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            clear = self.set_cookie("__Host-oauth", "", 0)
            if not expected or not code or not hmac.compare_digest(expected[0], given):
                return self.send(400, page("Sign-in failed", "<p>Sign-in failed. <a href='/'>Try again</a>.</p>"))
            try:
                email = google_email(code)
            except (OSError, ValueError, KeyError, IndexError):
                email = None
            if email != ALLOWED_EMAIL:
                print(f"sign-in refused for {email!r}", flush=True)
                return self.send(
                    403, page("Not allowed", "<p>That account is not allowed here.</p>"), headers=[("Set-Cookie", clear)]
                )
            session = sign(f"s.{int(time.time()) + SESSION_SECONDS}")
            return self.redirect("/", [clear, self.set_cookie("__Host-session", session, SESSION_SECONDS)])

        if path == "/":
            if not self.signed_in():
                return self.send(200, page("JournaLens", LOGIN))
            names = sorted((p.name for p in REPORTS.glob("*.md") if REPORT_NAME.fullmatch(p.name)), reverse=True)
            items = "".join(f'<li><a href="/reports/{n}">{n[:-3]}</a></li>' for n in names)
            reports = f"<ul>{items}</ul>" if items else "<p>None yet.</p>"
            return self.send(200, page("JournaLens", HOME % reports))

        if not self.signed_in():
            return self.send(401, page("Sign in", LOGIN))

        if path == "/status":
            return self.send_json(200, status())

        name = path.removeprefix("/reports/")
        if path.startswith("/reports/") and REPORT_NAME.fullmatch(name) and (REPORTS / name).is_file():
            return self.send(200, page(name[:-3], report_page(name)))

        self.send(404, page("Not found", "<p>Not found.</p>"))

    def do_POST(self):
        if not self.same_origin():
            return self.send(403)
        if self.path == "/auth/logout":
            return self.redirect("/", [self.set_cookie("__Host-session", "", 0)])
        if not self.signed_in():
            return self.send(401)
        feedback = re.fullmatch(r"/reports/([^/]+)/feedback", self.path)
        if feedback and REPORT_NAME.fullmatch(feedback[1]) and (REPORTS / feedback[1]).is_file():
            limit = MAX_FEEDBACK
        elif self.path == "/run":
            limit = 4 * MAX_TOPICS + 100
        else:
            return self.send(404)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= limit:
            return self.send(413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            data.get  # anything but an object is a bad request
        except (ValueError, AttributeError):
            return self.send(400)

        if feedback:
            NOTES.mkdir(exist_ok=True)
            target = NOTES / f"{feedback[1][:-3]}.json"
            target.with_suffix(".tmp").write_text(json.dumps(clean_feedback(data)))
            os.replace(target.with_suffix(".tmp"), target)
            (SPOOL / "notes.changed").touch()
            return self.send_json(200, {"ok": True})

        if busy():
            return self.send_json(409, {"error": "A report is already being written."})
        request = SPOOL / "request.json"
        topics = str(data.get("topics", ""))[:MAX_TOPICS]
        request.with_suffix(".tmp").write_text(json.dumps({"topics": topics}))
        os.replace(request.with_suffix(".tmp"), request)
        self.send_json(202, status())

    def do_PUT(self):
        if not self.same_origin():
            return self.send(403)
        if not self.signed_in():
            return self.send(401)
        if self.path != "/upload":
            return self.send(404)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self.send_json(413, {"error": "Missing or too large."})
        if busy():
            return self.send_json(409, {"error": "A report is already being written."})
        part = SPOOL / "upload.zip.part"
        remaining = length
        with open(part, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        with open(part, "rb") as f:
            is_zip = f.read(4) == b"PK\x03\x04"
        if remaining or not is_zip:
            part.unlink()
            return self.send_json(400, {"error": "That is not a complete .zip file."})
        os.replace(part, SPOOL / "upload.zip")
        self.send_json(201, {"ok": True})


if __name__ == "__main__":
    if not all((CLIENT_ID, CLIENT_SECRET, ALLOWED_EMAIL, PUBLIC_URL.startswith("https://"), len(SECRET) >= 32)):
        print("Not configured: fill in /etc/journalens/web.env")
        raise SystemExit(78)  # EX_CONFIG; the unit does not retry this
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
