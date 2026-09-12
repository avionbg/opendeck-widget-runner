"""OpenAI Codex usage widget: primary (5h) and secondary (7d) rate-limit windows.

Reads the OAuth token the Codex CLI keeps in ~/.codex/auth.json (re-read on every poll,
never logged) and calls the same usage endpoint the Codex CLI uses.
"""
import asyncio
import json
import os

from widgets._draw import get_json, open_first
from widgets.usage import UsageBase, humanize_until

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH = os.path.expanduser("~/.codex/auth.json")
OPEN_ON_PRESS = ["https://chatgpt.com/"]


def window_label(seconds):
    if not seconds:
        return "?"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}D"
    return f"{max(1, seconds // 3600)}H"


def fetch_codex_rows():
    with open(AUTH, encoding="utf-8") as f:
        tokens = json.load(f)["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    data = get_json(USAGE_URL, headers=headers)
    rl = data.get("rate_limit") or {}
    rows = []
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if w:
            rows.append((window_label(w.get("limit_window_seconds")), w.get("used_percent"),
                         humanize_until(w.get("reset_at"))))
    if not rows:
        raise KeyError("rate_limit")
    return rows


class Codex(UsageBase):
    action = "com.goran.widgetrunner.codex"
    brand = "Codex"

    def fetch_rows(self):
        return fetch_codex_rows()

    async def on_key_down(self, payload):
        """Open chatgpt.com in the default browser, then do the usual throttled refresh."""
        if await asyncio.to_thread(open_first, OPEN_ON_PRESS):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)
        await super().on_key_down(payload)
