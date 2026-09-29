"""Tell the operator's assistant daemon (Jane, in the reference setup) something
the operator should know.

Optional and best-effort: off unless `jane_socket` is set, and a daemon that is
down, slow or refusing costs a warning in the log, never the pass it reports on.
Without one, operator DMs go to Slack exactly as before.

Any receiver works: `POST /notify` over the unix socket, header `X-Jane-Token`,
JSON `{title, url, body, urgency: low|normal|high, kind, source}`, any 2xx = taken.

A notice, not a turn: Jane routes it by where the operator is (desktop, phone)
without waking her model, and lists it in her next turn so she knows it landed.
`relay` is how operator DMs reach her instead of Slack; it answers False when
she did not take it, so the caller can fall back.
"""

from __future__ import annotations

import logging
import re

import httpx

from robbie.config import Config, Secrets

logger = logging.getLogger(__name__)

TIMEOUT_S = 30
MAX_TITLE = 200
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_SLACK_URL = re.compile(r"<(https?://[^>|]+)>")


async def notify(
    cfg: Config,
    secrets: Secrets,
    title: str,
    *,
    url: str = "",
    body: str = "",
    urgency: str = "normal",
    kind: str = "robbie",
) -> bool:
    if not cfg.jane_socket or secrets.jane_token is None:
        return False
    try:
        transport = httpx.AsyncHTTPTransport(uds=cfg.jane_socket)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as client:
            resp = await client.post(
                "http://jane/notify",
                json={"title": title[:MAX_TITLE], "url": url, "body": body,
                      "urgency": urgency, "kind": kind, "source": "robbie"},
                headers={"X-Jane-Token": secrets.jane_token.get_secret_value()},
            )
            resp.raise_for_status()
    except Exception as ex:  # noqa: BLE001 — a notice must never cost the pass
        logger.warning("could not tell jane: %r", ex)
        return False
    return True


async def relay(cfg: Config, secrets: Secrets, text: str) -> bool:
    """A Slack-shaped operator DM, as a notice."""
    title, url, body = from_slack(text)
    return await notify(cfg, secrets, title, url=url, body=body, kind="operator")


def from_slack(text: str) -> tuple[str, str, str]:
    """Slack mrkdwn → (first line, first link, the rest), in plain text."""
    first_url = next(
        (m.group(1) for m in re.finditer(r"<(https?://[^|>]+)", text)), ""
    )
    plain = _SLACK_URL.sub(r"\1", _SLACK_LINK.sub(r"\2", text)).replace("*", "")
    head, _, rest = plain.strip().partition("\n")
    if len(head) > MAX_TITLE:
        head, rest = head[: MAX_TITLE - 1] + "…", plain.strip()
    return head.strip(), first_url, rest.strip()
