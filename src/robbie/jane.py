"""Tell Jane, the operator's assistant daemon, what the bug queue just did.

Optional and best-effort: off unless `jane_socket` is set, and a Jane that is
down, slow or refusing costs a warning in the log, never the pass it reports on.

A turn in her `main` conversation, the same one her widget and phone share, so
she relays it with whatever else she knows rather than forwarding it verbatim.
The stream is read to the end: her turn runs while it is open, and hanging up
early can take the turn down with it.
"""

from __future__ import annotations

import logging

import httpx

from robbie.config import Config, Secrets

logger = logging.getLogger(__name__)

TIMEOUT_S = 300


async def tell(cfg: Config, secrets: Secrets, text: str) -> None:
    if not cfg.jane_socket or secrets.jane_token is None:
        return
    try:
        transport = httpx.AsyncHTTPTransport(uds=cfg.jane_socket)
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as client:
            async with client.stream(
                "POST", "http://jane/chat",
                json={"text": f"[robbie, the bug-fix bot] {text}", "source": "robbie"},
                headers={"X-Jane-Token": secrets.jane_token.get_secret_value()},
            ) as resp:
                resp.raise_for_status()
                async for _ in resp.aiter_lines():
                    pass
    except Exception as ex:  # noqa: BLE001 — a notice must never cost the pass
        logger.warning("could not tell jane: %r", ex)
