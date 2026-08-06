"""A read-only panel over the state robbie already writes. Off unless asked for.

stdlib `http.server` on purpose: one page, no JavaScript, no framework, nothing new
in the image. It reads the same SQLite and the same meters the daemon reads, so it
runs beside the daemon or on its own without coordinating with it.

There is no auth, so it binds to localhost unless told otherwise and the compose
service publishes it on 127.0.0.1 only. Everything it shows — diffs quoted in
transcripts, PR titles, spend — is private to the team.
"""

from __future__ import annotations

import html
import logging
import shutil
from collections.abc import Collection
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robbie import budget
from robbie.config import Config, Secrets
from robbie.db import SCHEMA_VERSION, Db

logger = logging.getLogger(__name__)

REFRESH_S = 30
RECENT = 25

CSS = """
:root { color-scheme: light dark; --line: #0000001f; --dim: #55606e; }
@media (prefers-color-scheme: dark) {
  :root { --line: #ffffff2b; --dim: #a3adbb; }
}
* { box-sizing: border-box; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
       margin: 0 auto; padding: 1.5rem; max-width: 1100px; }
h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
     margin: 2rem 0 .5rem; color: var(--dim); font-weight: 600; }
.sub { color: var(--dim); margin: 0 0 1rem; }
.dim { color: var(--dim); font-size: .8em; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .3rem .6rem .3rem 0;
         border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--dim); font-weight: 600; }
td.wide { white-space: normal; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.bar { display: inline-block; height: .7em; background: currentColor; opacity: .5;
       vertical-align: middle; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
        gap: .75rem; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: .6rem .8rem; }
.card b { display: block; font-size: 1.35rem; font-weight: 600; }
.card span { color: var(--dim); font-size: .78rem; }
a { color: inherit; text-decoration: underline;
    text-decoration-color: var(--dim); text-underline-offset: 2px; }
a:hover { text-decoration-color: currentColor; }
.ok { color: #2a9d4a; } .warn { color: #c77700; } .bad { color: #c0392b; }
pre { white-space: pre-wrap; word-break: break-word; border: 1px solid var(--line);
      padding: 1rem; border-radius: 6px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def _pr_link(repo: str, pr: int) -> str:
    """github.com is the only host robbie speaks to, so the URL is derivable."""
    return (
        f'<a href="https://github.com/{_esc(repo)}/pull/{int(pr)}" '
        f'target="_blank" rel="noopener">{_esc(f"{repo}#{pr}")}</a>'
    )


def _card(value: object, label: str, tone: str = "") -> str:
    cls = f' class="{tone}"' if tone else ""
    return f'<div class="card"><b{cls}>{_esc(value)}</b><span>{_esc(label)}</span></div>'


def _table(
    headers: list[str],
    rows: list[list[str]],
    numeric: Collection[int] = frozenset(),
    wide: Collection[int] = frozenset(),
) -> str:
    if not rows:
        return '<p class="sub">nothing recorded yet</p>'

    def cls(i: int) -> str:
        return " ".join(n for n, on in (("num", i in numeric), ("wide", i in wide)) if on)

    head = "".join(f'<th class="{cls(i)}">{_esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{cls(i)}">{c}</td>' for i, c in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _ago(ms: int | None) -> str:
    if not ms:
        return "—"
    seconds = int(datetime.now(UTC).timestamp() - ms / 1000)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def render(cfg: Config, secrets: Secrets | None, db: Db) -> str:
    parts = [
        f"<h1>robbie</h1><p class='sub'>{_esc(cfg.backend)} backend · "
        f"{len(cfg.repos)} repo(s) · {cfg.max_concurrent_reviews} concurrent · "
        f"tick {cfg.poll_interval_s}s · refreshed "
        f"{datetime.now(UTC):%H:%M:%S} UTC</p>",
        _ready(cfg, db),
        _system(cfg, db),
        _meters(cfg, secrets, db),
        _arms(cfg, db),
        _queue(db),
        _reviews(db),
        _transcripts(cfg),
    ]
    return (
        f"<!-- robbie dashboard --><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='{REFRESH_S}'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>robbie</title><style>{CSS}</style>" + "".join(parts)
    )


CI_TONE = {"green": ("ok", "ready for a human"), "red": ("bad", "build went red"),
           "waiting": ("warn", "waiting on the build")}


READY_DAYS = 30


def _ready(cfg: Config, db: Db) -> str:
    """Approvals and what the build robbie asked for made of them.

    The window is a backstop, not the way rows leave: a PR drops off when someone
    labels it taken. Clocking it any tighter hid the ones nobody had got to yet,
    which are the only ones this panel is for.
    """
    rows = []
    for r in db.approved_and_green(budget.midnight_ms() - READY_DAYS * 86_400_000):
        tone, label = CI_TONE.get(r["ci_state"] or "", ("", r["ci_state"] or "—"))
        model = r["model"] or "account"
        third = ' <span class="dim">3rd-party</span>' if model in cfg.endpoint_models else ""
        rows.append([
            _pr_link(r["repo"], r["pr"]),
            f'<span class="{tone}">{_esc(label)}</span>',
            f"{_esc(model)}{third}",
            _esc((r["head_sha"] or "")[:8]),
            _esc(_ago(r["ci_seen_at"] or r["created_at"])),
        ])
    return (
        "<h2>approved by robbie</h2>"
        "<p class='sub'>an `ok` asks CI to run; this is what came back</p>"
        + _table(["pr", "state", "approved by", "commit", "last seen"], rows, numeric={4})
    )


def _system(cfg: Config, db: Db) -> str:
    running, published, total = db.counts()
    spent = db.spend_since(budget.midnight_ms(), cfg.endpoint_models)
    usage = shutil.disk_usage(cfg.state_dir)
    return "<h2>system</h2><div class='grid'>" + "".join([
        _card(running, "reviewing right now", "warn" if running else ""),
        _card(published, f"published of {total} rows"),
        _card(f"${spent:.2f}", "billed to the account today"),
        _card(f"{usage.free // 2**30} GB", "free on the state disk"),
        _card(f"v{SCHEMA_VERSION}", "schema"),
        _card(cfg.review_effort, "review effort"),
    ]) + "</div>"


def _meters(cfg: Config, secrets: Secrets | None, db: Db) -> str:
    if secrets is None:
        return "<h2>usage</h2><p class='sub'>no secrets in this process; meters unread</p>"
    rows = []
    for label, kwargs in (
        ("account", {}),
        ("endpoint", {"via_endpoint": True}),
    ):
        if label == "endpoint" and not secrets.review_base_url:
            continue
        verdict = budget.check(cfg, secrets, db, **kwargs)  # cached ~60s
        tone = "ok" if verdict.allowed else "bad"
        rows.append([
            _esc(label),
            f'<span class="{tone}">{"admitting" if verdict.allowed else "holding"}</span>',
            _esc(verdict.detail),
        ])
    return "<h2>usage</h2>" + _table(["meter", "state", "reading"], rows, wide={2})


def _arms(cfg: Config, db: Db) -> str:
    if not cfg.review_models:
        return (
            "<h2>models</h2><p class='sub'>every review runs on the account's own "
            "model; no arms configured</p>"
        )
    total_weight = sum(m.weight for m in cfg.review_models)
    seen = db.by_model()
    rows = []
    for arm in cfg.review_models:
        share = 100 * arm.weight / total_weight
        row = seen.get(arm.model)
        rows.append([
            _esc(arm.model),
            _esc(arm.via),
            f'{share:.0f}% <span class="bar" style="width:{share / 2:.0f}px"></span>',
            _esc(row["runs"] if row else 0),
            _esc(row["f"] if row else 0),
            _esc(f"{row['secs']}s" if row and row["secs"] else "—"),
        ])
    return "<h2>models</h2>" + _table(
        ["model", "via", "share of reviews", "runs", "findings", "mean"], rows, {2, 3, 4, 5}
    )


def _queue(db: Db) -> str:
    rows = [
        [
            _pr_link(r["repo"], r["pr"]),
            _esc(r["state"]),
            _esc(r["hold_reason"]),
            _esc(_ago(r["created_at"])),
        ]
        for r in db.unfinished(RECENT)
    ]
    return "<h2>waiting, held or failed</h2>" + _table(
        ["pr", "state", "reason", "when"], rows, numeric={3}, wide={2}
    )


def _reviews(db: Db) -> str:
    rows = [
        [
            _pr_link(r["repo"], r["pr"]),
            _esc((r["head_sha"] or "")[:8]),
            _esc(r["verdict"]),
            _esc(r["model"] or "account"),
            _esc(r["findings"]),
            _esc(r["summary_findings"]),
            _esc(r["inline"]),
            _esc(f"{int(r['duration_s'])}s" if r["duration_s"] else "—"),
            _esc(r["tokens_out"]),
            _esc(_ago(r["finished_at"] or r["created_at"])),
        ]
        for r in db.recent_published(RECENT)
    ]
    return (
        "<h2>reviews</h2><p class='sub'>cost is omitted on purpose: the CLI prices a "
        "third-party model off its own table, which is not that provider's bill</p>"
        + _table(
            ["pr", "commit", "verdict", "model", "inline", "summary", "anchored",
             "time", "out tok", "when"],
            rows, {4, 5, 6, 7, 8, 9},
        )
    )


def _transcripts(cfg: Config) -> str:
    try:
        files = sorted(
            cfg.transcript_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:RECENT]
    except OSError:
        return "<h2>transcripts</h2><p class='sub'>transcript directory unreadable</p>"
    rows = [
        [
            f'<a href="/transcript?name={_esc(f.name)}">{_esc(f.name)}</a>',
            _esc(f"{f.stat().st_size // 1024} KB"),
            _esc(_ago(int(f.stat().st_mtime * 1000))),
        ]
        for f in files
    ]
    return "<h2>transcripts</h2>" + _table(["file", "size", "written"], rows, {1, 2})


def _transcript_body(cfg: Config, name: str) -> str | None:
    """One transcript, or None when the name points anywhere but the directory."""
    root = cfg.transcript_dir.resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_file() or target.suffix not in (".md", ".err"):
        return None
    return target.read_text(encoding="utf-8", errors="replace")


class _Handler(BaseHTTPRequestHandler):
    server_version = "robbie"

    def __init__(
        self, cfg: Config, secrets: Secrets | None, *args: Any, **kwargs: Any
    ) -> None:
        self.cfg, self.secrets = cfg, secrets
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("dashboard %s", fmt % args)

    def _send(self, body: str, status: int = 200, kind: str = "text/html") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        url = urlparse(self.path)
        # a fresh connection per request: the daemon writes with WAL, so a reader
        # never blocks it, and a page load never holds a handle open
        try:
            db = Db(self.cfg.db_path, read_only=True)
        except Exception as ex:  # noqa: BLE001 — a panel that cannot read says so
            logger.warning("dashboard could not open %s: %s", self.cfg.db_path, ex)
            self._send(f"cannot read robbie's state: {ex}", 503, "text/plain")
            return
        try:
            if url.path == "/":
                self._send(render(self.cfg, self.secrets, db))
            elif url.path == "/transcript":
                name = (parse_qs(url.query).get("name") or [""])[0]
                body = _transcript_body(self.cfg, name)
                if body is None:
                    self._send("no such transcript", 404, "text/plain")
                else:
                    self._send(
                        f"<meta charset='utf-8'><title>{_esc(name)}</title>"
                        f"<style>{CSS}</style><h1>{_esc(name)}</h1>"
                        f"<p class='sub'><a href='/'>← back</a></p><pre>{_esc(body)}</pre>"
                    )
            else:
                self._send("not found", 404, "text/plain")
        except Exception as ex:  # noqa: BLE001 — one bad page must not kill the server
            logger.exception("dashboard failed to render %s", url.path)
            self._send(f"dashboard error: {ex}", 500, "text/plain")
        finally:
            db.close()


def serve(cfg: Config, secrets: Secrets | None, *, host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), partial(_Handler, cfg, secrets))
    logger.info("dashboard on http://%s:%d (no auth; keep it local)", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
