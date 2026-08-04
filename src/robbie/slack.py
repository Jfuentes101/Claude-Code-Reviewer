"""Slack delivery: operator DMs, author DMs, channel notes.

No Socket Mode, no bot conversation surface — robbie only ever posts. That keeps
the whole Slack dependency at one HTTP endpoint.

An author only gets a DM when their GitHub login is mapped in the users file. No
mapping means no DM, and the operator is told once per login: GitHub logins are
not Slack users, and guessing one would DM a stranger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

POST_URL = "https://slack.com/api/chat.postMessage"


@dataclass
class Slack:
    token: str
    owner_id: str
    users_file: Path
    dry_run: bool = False

    async def post(self, channel: str, text: str) -> bool:
        if self.dry_run:
            logger.info("DRY slack → %s:\n%s", channel, text)
            return True
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    POST_URL,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"channel": channel, "text": text, "unfurl_links": False},
                )
            body = resp.json()
        except Exception as ex:  # noqa: BLE001 — a failed notice must not kill a review
            logger.warning("slack post to %s failed: %s", channel, ex)
            return False
        if not body.get("ok"):
            logger.warning("slack post to %s rejected: %s", channel, body.get("error"))
            return False
        return True

    async def dm_owner(self, text: str) -> bool:
        return await self.post(self.owner_id, text)

    async def dm_author(self, login: str, text: str) -> str:
        """Returns 'sent', 'unmapped' or 'bot' so the caller can warn once."""
        if login.startswith("app/") or login.endswith("[bot]"):
            return "bot"
        member = self.lookup(login)
        if not member:
            return "unmapped"
        await self.post(member, text)
        return "sent"

    def lookup(self, login: str) -> str | None:
        """github login → slack member id. Case-insensitive; `#` comments out a row."""
        if not self.users_file.is_file():
            return None
        wanted = login.strip().lower()
        for line in self.users_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 2 and fields[0].lower() == wanted:
                return fields[1]
        return None


def verdict_phrase(verdict: str, *, blocking: int, should_fix: int) -> str:
    """The one-line "what happened", shared by the channel note and the author DM."""
    if verdict == "needs-work":
        what = "*changes requested*"
        if blocking:
            what += f" — {blocking} {_things(blocking)} to fix before this can merge"
        return what
    what = "*reviewed, nothing blocking*"
    if should_fix:
        what += f" — but {should_fix} {_things(should_fix)} should be fixed"
    return what


def channel_note(pr: int, title: str, url: str, author: str, phrase: str) -> str:
    return f"🔍 <{url}|*#{pr}*> {title[:70]} — {phrase}. Over to {author}, findings are on the PR."


def approved_note(pr: int, title: str, url: str, ci: str) -> str:
    """An `ok` leaves no review on the PR, so this line is the whole signal."""
    return f"✅ <{url}|*#{pr}*> {title[:70]} — nothing to fix, {ci}."


def author_note(pr: int, title: str, url: str, phrase: str) -> str:
    return (
        f"🔍 <{url}|*#{pr}*> {title[:70]} — my pre-review is done: {phrase}. "
        "The findings are on the PR. 🙏"
    )


def unmapped_note(login: str, users_file: Path) -> str:
    return (
        f"I reviewed a PR by *{login}* and posted the findings, but I have no Slack id "
        f"for them so they got no DM. Add a line to `{users_file}` (`{login}` + tab + "
        "their Slack member id) and I'll write to them from the next one on."
    )


def _things(n: int) -> str:
    return "thing" if n == 1 else "things"
