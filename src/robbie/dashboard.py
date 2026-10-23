"""A read-only panel over the state robbie already writes. Off unless asked for.

stdlib `http.server` on purpose: a few pages, no JavaScript, no framework, nothing
new in the image. Everything it shows comes out of the same SQLite the daemon
writes — including the usage meters, which it reads rather than polls, so a
browser left open cannot spend the rate limit the gate depends on. The fixes and
the assigned issues come from the daemon's export file, for the same reason.

`/` is the summary: one card per page, and what is waiting on the operator.

There is no auth, so it binds to localhost unless told otherwise and the compose
service publishes it on 127.0.0.1 only. Everything it shows — diffs quoted in
transcripts, PR titles, spend — is private to the team.
"""

from __future__ import annotations

import html
import json
import logging
import shutil
import sqlite3
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robbie import branding, budget, export
from robbie.config import Config
from robbie.db import SCHEMA_VERSION, Db

logger = logging.getLogger(__name__)

REFRESH_S = 30
RECENT = 25
READY_DAYS = 30

CSS = """
:root { color-scheme: light dark; --fg: #1d1f23; --bg: #fcfcfc; --line: #0000001a;
        --dim: #6a7280; --ok: #1f8a43; --warn: #a96500; --bad: #c0392b; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e4e6ea; --bg: #131417; --line: #ffffff1c; --dim: #98a1ad;
          --ok: #4cc27a; --warn: #e0a23a; --bad: #ef6b5b; }
}
* { box-sizing: border-box; }
body { font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg);
       background: var(--bg); margin: 0 auto; padding: 1.25rem 1rem 3rem; max-width: 1100px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: .4rem 1.75rem;
         padding-bottom: .9rem; border-bottom: 1px solid var(--line); }
h1 { font-size: 1rem; margin: 0; }
nav { display: flex; flex-wrap: wrap; gap: .25rem 1.1rem; }
nav a { color: var(--dim); text-decoration: none; }
nav a:hover, nav a.on { color: var(--fg); }
nav a.on { font-weight: 600; }
header .meta { margin-left: auto; color: var(--dim); font-size: .8rem; }
h2 { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
     color: var(--dim); font-weight: 600; margin: 2rem 0 .4rem; }
.sub { color: var(--dim); font-size: .85rem; margin: 0 0 .75rem; }
.dim { color: var(--dim); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4rem .9rem .4rem 0; white-space: nowrap;
         border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--dim); font-weight: 500; font-size: .8rem; }
td.wide { white-space: normal; min-width: 16rem; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
        gap: .75rem; margin-top: 1.5rem; }
.card { display: block; border: 1px solid var(--line); border-radius: 8px;
        padding: .75rem .9rem; color: inherit; text-decoration: none; }
a.card:hover { border-color: var(--dim); }
.card b { display: block; font-size: 1.5rem; font-weight: 600; line-height: 1.25;
          font-variant-numeric: tabular-nums; }
.card span { display: block; font-size: .85rem; }
.card small { display: block; color: var(--dim); font-size: .75rem; margin-top: .1rem; }
a { color: inherit; text-decoration: underline; text-decoration-color: var(--line);
    text-underline-offset: 3px; }
a:hover { text-decoration-color: currentColor; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.tag.ok::before, .tag.warn::before, .tag.bad::before { content: "● "; font-size: .7em; }
.bar { display: inline-block; height: .6em; background: currentColor; opacity: .4;
       vertical-align: middle; border-radius: 2px; }
pre { white-space: pre-wrap; word-break: break-word; border: 1px solid var(--line);
      padding: 1rem; border-radius: 8px; font-size: .85rem; }
"""

ICON = (
    "<link rel='icon' href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
    " viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%2317181c'/%3E"
    "%3Ctext x='12' y='17.5' font-size='13' text-anchor='middle'%3E%F0%9F%A7%A0%3C/text%3E"
    "%3C/svg%3E\">"
)


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def _pr_link(repo: str, pr: int, kind: str = "pull") -> str:
    """github.com is the only host robbie speaks to, so the URL is derivable.
    ponytail: the repo is only on hover; a panel over several repos that look
    alike would want it in the text."""
    return (
        f'<a href="https://github.com/{_esc(repo)}/{kind}/{int(pr)}" title="{_esc(repo)}" '
        f'target="_blank" rel="noopener">#{int(pr)}</a>'
    )


TONE = {"green": "ok", "approved": "ok", "red": "bad", "needs work": "bad",
        "pending": "warn", "waiting": "warn", "draft": "warn"}


def _tag(word: str | None, text: str | None = None) -> str:
    return f'<span class="tag {TONE.get(word or "", "")}">{_esc(text or word)}</span>'


def _card(value: object, label: str, sub: str = "", tone: str = "", href: str = "") -> str:
    body = (
        f'<b class="{tone}">{_esc(value)}</b><span>{_esc(label)}</span>'
        + (f"<small>{_esc(sub)}</small>" if sub else "")
    )
    if href:
        return f'<a class="card" href="{href}">{body}</a>'
    return f'<div class="card">{body}</div>'


def _table(
    headers: list[str],
    rows: list[list[str]],
    numeric: Collection[int] = frozenset(),
    wide: Collection[int] = frozenset(),
    empty: str = "nothing recorded yet",
) -> str:
    if not rows:
        return f'<p class="sub">{_esc(empty)}</p>'

    def cls(i: int) -> str:
        return " ".join(n for n, on in (("num", i in numeric), ("wide", i in wide)) if on)

    head = "".join(f'<th class="{cls(i)}">{_esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{cls(i)}">{c}</td>' for i, c in enumerate(row)) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _ago(ms: int | None) -> str:
    if not ms:
        return "—"
    seconds = int(datetime.now(UTC).timestamp() - ms / 1000)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _sha(sha: str | None) -> str:
    return f'<span class="mono">{_esc((sha or "")[:8])}</span>'


# ----- what the pages read ------------------------------------------------

NO_EXPORT = "<p class='sub'>set <code>export_interval_s</code> to fill this</p>"


def _worklist(cfg: Config) -> dict[str, Any] | None:
    """The daemon's last export, or None when it has never written one."""
    try:
        path = export.export_dir(cfg) / "worklist.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _as_of(work: dict[str, Any]) -> str:
    return f"<p class='sub'>as of the export {_ago(work.get('written_at', 0) * 1000)}</p>"


def _titles(work: dict[str, Any] | None) -> dict[tuple[str, int], str]:
    """PR titles the export already read, so the tables can name what they link."""
    return {
        (r["repo"], r["number"]): r.get("title") or ""
        for key in ("awaiting_you", "needs_work", "fixes", "mine")
        for r in (work or {}).get(key, [])
    }


def _approvals(db: Db) -> list[sqlite3.Row] | None:
    """Approvals in the window. The window is a backstop, not the way rows leave:
    a PR drops off when someone labels it taken."""
    try:
        return db.approved_and_green(budget.midnight_ms() - READY_DAYS * 86_400_000)
    except sqlite3.OperationalError:
        # a read-only connection cannot run the CREATE, so a panel that starts
        # before the daemon has booted once sees a table that is not there yet
        return None


def _gates(cfg: Config, db: Db) -> list[tuple[str, budget.Verdict]]:
    """What the daemon's meter poll last stored. A page that refreshes itself must
    never ask a provider: the meters rate-limit, and the gate needs them more."""
    gates = [("account", budget.check(cfg, db))]
    if cfg.endpoint_models:
        gates.append(("endpoint", budget.check(cfg, db, via_endpoint=True)))
    return gates


# ----- pages --------------------------------------------------------------

CI_TONE = {"green": ("green", "ready for a human"), "red": ("red", "build went red"),
           "waiting": ("waiting", "waiting on the build")}


def _summary(cfg: Config, db: Db) -> str:
    work = _worklist(cfg)
    titles = _titles(work)
    approved = _approvals(db) or []
    green = [r for r in approved if r["ci_state"] == "green"]
    fixes = (work or {}).get("fixes", [])
    drafts = [f for f in fixes if f.get("status") == "draft"]
    issues = (work or {}).get("triage_assigned", [])
    bare = sum(1 for i in issues if not i.get("prs"))
    held = [r for r in db.unfinished(RECENT) if r["created_at"] >= budget.midnight_ms()]
    running = db.counts()[0]
    gates = _gates(cfg, db)
    stuck = next((v for _, v in gates if not v.allowed), None)
    spent = db.spend_since(budget.midnight_ms(), cfg.endpoint_models)
    unknown = "—" if work is None else None

    cards = "".join([
        _card(len(green), "ready for you", f"{len(approved)} approved in {READY_DAYS} days",
              "ok" if green else "", "/approved"),
        _card(unknown or len(fixes), "open fixes", f"{len(drafts)} draft(s) to look at",
              href="/fixes"),
        _card(unknown or len(issues), "assigned issues", f"{bare} without a pull request",
              href="/issues"),
        _card(len(held), "held or failed today", f"{running} reviewing now",
              "warn" if held else "", "/reviews"),
        _card("holding" if stuck else "admitting", "usage", (stuck or gates[0][1]).detail,
              "bad" if stuck else "ok", "/system"),
        _card(f"${spent:.2f}", "billed today", href="/system"),
    ])
    waiting = [
        [
            _pr_link(r["repo"], r["pr"]),
            _esc(titles.get((r["repo"], r["pr"]))),
            _tag("green", "approved, build green"),
            _esc(_ago(r["ci_seen_at"] or r["created_at"])),
        ]
        for r in green
    ] + [
        [
            _pr_link(f["repo"], f["number"]),
            _esc(f["title"]),
            _tag("draft", "fix draft" + (f" for #{f['issue']}" if f.get("issue") else "")),
            _esc(_ago(f.get("opened_at", 0) * 1000)),
        ]
        for f in drafts
    ]
    return (
        f"<div class='grid'>{cards}</div>"
        "<h2>waiting on you</h2>"
        + (NO_EXPORT if work is None else "")
        + _table(["pr", "title", "why", "since"], waiting, numeric={3}, wide={1},
                 empty="nothing is waiting on you")
    )


def _approved(cfg: Config, db: Db) -> str:
    head = f"<h2>approved by {_esc(branding.name())}</h2>"
    approved = _approvals(db)
    if approved is None:
        return head + "<p class='sub'>waiting for the daemon's first tick</p>"
    titles = _titles(_worklist(cfg))
    rows = []
    for r in approved:
        word, label = CI_TONE.get(r["ci_state"] or "", ("", r["ci_state"] or "—"))
        model = r["model"] or "account"
        third = ' <span class="dim">3rd-party</span>' if model in cfg.endpoint_models else ""
        rows.append([
            _pr_link(r["repo"], r["pr"]),
            _esc(titles.get((r["repo"], r["pr"]))),
            _tag(word, label),
            f"{_esc(model)}{third}",
            _sha(r["head_sha"]),
            _esc(_ago(r["ci_seen_at"] or r["created_at"])),
        ])
    return (
        head + "<p class='sub'>an `ok` asks CI to run; this is what came back</p>"
        + _table(["pr", "title", "build", "approved by", "commit", "last seen"], rows,
                 numeric={5}, wide={1})
    )


def _fixes(cfg: Config, db: Db) -> str:
    head = "<h2>open fixes</h2>"
    work = _worklist(cfg)
    if work is None:
        return head + NO_EXPORT
    rows = [
        [
            _pr_link(f["repo"], f["number"]),
            _esc(f["title"]),
            _pr_link(f["repo"], f["issue"], "issues") if f.get("issue") else "—",
            _tag(f.get("status")),
            _tag(f.get("ci")),
            _esc(_ago(f.get("opened_at", 0) * 1000)),
        ]
        for f in work.get("fixes", [])
    ]
    return head + _as_of(work) + _table(
        ["pr", "title", "issue", "state", "ci", "opened"], rows, numeric={5}, wide={1},
        empty="no fix is open",
    )


def _issues(cfg: Config, db: Db) -> str:
    head = "<h2>issues assigned to you</h2>"
    work = _worklist(cfg)
    if work is None:
        return head + NO_EXPORT
    # every row carries the queue's own labels, so they say nothing here
    quiet = {lbl for repo in cfg.repos for lbl in repo.issues.labels}
    rows = [
        [
            _pr_link(i["repo"], i["number"], "issues"),
            _esc(i["title"]),
            _esc(", ".join(x for x in i.get("labels", []) if x not in quiet) or "—"),
            " ".join(
                f'{_pr_link(i["repo"], p["number"])} {_tag(p["status"])}'
                for p in i.get("prs", [])
            ) or '<span class="dim">none</span>',
            _esc(_ago(i.get("updated_at", 0) * 1000)),
        ]
        for i in work.get("triage_assigned", [])
    ]
    return head + _as_of(work) + _table(
        ["issue", "title", "labels", "pull requests", "updated"], rows,
        numeric={4}, wide={1}, empty="nothing is assigned to you",
    )


def _reviews(cfg: Config, db: Db) -> str:
    queue = [
        [
            _pr_link(r["repo"], r["pr"]),
            _tag("red" if r["state"] == "failed" else "waiting", r["state"]),
            _esc(r["hold_reason"]),
            _esc(_ago(r["created_at"])),
        ]
        for r in db.unfinished(RECENT)
    ]
    done = [
        [
            _pr_link(r["repo"], r["pr"]),
            _sha(r["head_sha"]),
            _tag({"ok": "green", "needs-work": "red"}.get(r["verdict"]), r["verdict"]),
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
        "<h2>waiting, held or failed</h2>"
        + _table(["pr", "state", "reason", "when"], queue, numeric={3}, wide={2},
                 empty="the queue is clear")
        + "<h2>reviews</h2><p class='sub'>cost is omitted on purpose: the CLI prices a "
        "third-party model off its own table, which is not that provider's bill</p>"
        + _table(
            ["pr", "commit", "verdict", "model", "inline", "summary", "anchored",
             "time", "out tok", "when"],
            done, {4, 5, 6, 7, 8, 9},
        )
    )


def _system(cfg: Config, db: Db) -> str:
    running, published, total = db.counts()
    spent = db.spend_since(budget.midnight_ms(), cfg.endpoint_models)
    usage = shutil.disk_usage(cfg.state_dir)
    cards = "".join([
        _card(running, "reviewing right now", tone="warn" if running else ""),
        _card(published, f"published of {total} rows"),
        _card(f"${spent:.2f}", "billed to the account today"),
        _card(f"{usage.free // 2**30} GB", "free on the state disk"),
        _card(f"v{SCHEMA_VERSION}", "schema"),
        _card(cfg.review_effort, "review effort"),
    ])
    meters = [
        [
            _esc(label),
            _tag("green" if v.allowed else "red", "admitting" if v.allowed else "holding"),
            _esc(v.detail),
        ]
        for label, v in _gates(cfg, db)
    ]
    return (
        "<h2>system</h2>"
        f"<p class='sub'>{_esc(cfg.backend)} backend · {len(cfg.repos)} repo(s) · "
        f"{cfg.max_concurrent_reviews} concurrent · tick {cfg.poll_interval_s}s</p>"
        f"<div class='grid'>{cards}</div>"
        "<h2>usage</h2>" + _table(["meter", "state", "reading"], meters, wide={2})
        + _arms(cfg, db)
    )


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


def _transcripts(cfg: Config, db: Db) -> str:
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


PAGES: dict[str, tuple[str, Callable[[Config, Db], str]]] = {
    "/": ("summary", _summary),
    "/approved": ("approved", _approved),
    "/fixes": ("fixes", _fixes),
    "/issues": ("issues", _issues),
    "/reviews": ("reviews", _reviews),
    "/system": ("system", _system),
    "/transcripts": ("transcripts", _transcripts),
}


def _shell(current: str, body: str, title: str = "", refresh: bool = True) -> str:
    name = _esc(branding.name())
    nav = "".join(
        f'<a href="{path}"{" class=on" if path == current else ""}>{label}</a>'
        for path, (label, _) in PAGES.items()
    )
    return (
        "<!doctype html><!-- robbie dashboard --><meta charset='utf-8'>"
        + (f"<meta http-equiv='refresh' content='{REFRESH_S}'>" if refresh else "")
        + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{name}{f' · {_esc(title)}' if title else ''}</title>{ICON}"
        f"<style>{CSS}</style>"
        f"<header><h1>{name}</h1><nav>{nav}</nav>"
        f"<span class='meta'>refreshed {datetime.now(UTC):%H:%M:%S} UTC</span></header>"
        f"<main>{body}</main>"
    )


def render(cfg: Config, db: Db, path: str = "/") -> str | None:
    """One page of the panel, or None for a path that is not one."""
    if path not in PAGES:
        return None
    label, build = PAGES[path]
    return _shell(path, build(cfg, db), "" if path == "/" else label)


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
        self, cfg: Config, *args: Any, **kwargs: Any
    ) -> None:
        self.cfg = cfg
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
            if url.path == "/transcript":
                name = (parse_qs(url.query).get("name") or [""])[0]
                body = _transcript_body(self.cfg, name)
                if body is None:
                    self._send("no such transcript", 404, "text/plain")
                else:
                    self._send(_shell(
                        "/transcripts",
                        f"<h2>{_esc(name)}</h2><pre>{_esc(body)}</pre>",
                        name, refresh=False,
                    ))
            elif (page := render(self.cfg, db, url.path)) is not None:
                self._send(page)
            else:
                self._send("not found", 404, "text/plain")
        except Exception as ex:  # noqa: BLE001 — one bad page must not kill the server
            logger.exception("dashboard failed to render %s", url.path)
            self._send(f"dashboard error: {ex}", 500, "text/plain")
        finally:
            db.close()


def serve(cfg: Config, *, host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), partial(_Handler, cfg))
    logger.info("dashboard on http://%s:%d (no auth; keep it local)", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
