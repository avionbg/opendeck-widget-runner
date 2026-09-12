"""Small SDK shared by runner.py and every widget module."""
import asyncio
import base64
import io
import json
import logging

log = logging.getLogger("widgetrunner")


class Deck:
    """Thin wrapper over the OpenDeck websocket (Stream Deck protocol)."""

    def __init__(self, ws):
        self.ws = ws

    async def send(self, event, context=None, payload=None, **extra):
        msg = {"event": event, **extra}
        if context is not None:
            msg["context"] = context
        if payload is not None:
            msg["payload"] = payload
        await self.ws.send(json.dumps(msg))

    async def set_image(self, context, image, target=0, state=None):
        """image: PIL.Image, or a ready data-URI string."""
        if not isinstance(image, str):
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            image = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        payload = {"image": image, "target": target}
        if state is not None:
            payload["state"] = state
        await self.send("setImage", context, payload)

    async def set_title(self, context, title, target=0):
        await self.send("setTitle", context, {"title": title, "target": target})

    async def set_settings(self, context, settings):
        await self.send("setSettings", context, settings)

    async def show_alert(self, context):
        await self.send("showAlert", context)

    async def show_ok(self, context):
        await self.send("showOk", context)

    async def log(self, message):
        await self.send("logMessage", payload={"message": message})


class Widget:
    """Base class. Subclass in widgets/<name>.py and set `action` to the action UUID."""

    action = None  # e.g. "com.goran.widgetrunner.clock"

    def __init__(self, deck: Deck, context: str, payload: dict):
        self.deck = deck
        self.context = context
        self.settings = payload.get("settings", {}) or {}
        self.controller = payload.get("controller", "Keypad")
        self.coordinates = payload.get("coordinates", {})
        self.title_params = {}  # filled by titleParametersDidChange
        self.device = None      # device id, filled by the runner from willAppear
        self._tasks = []

    # --- helpers -------------------------------------------------------
    def every(self, seconds, fn, *, align=False):
        """Run coroutine `fn()` periodically. align=True fires on whole-period boundaries."""

        async def loop():
            while True:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("%s tick failed", type(self).__name__)
                if align:
                    now = asyncio.get_running_loop().time()
                    await asyncio.sleep(seconds - (now % seconds))
                else:
                    await asyncio.sleep(seconds)

        task = asyncio.create_task(loop())
        self._tasks.append(task)
        return task

    async def set_image(self, image, **kw):
        await self.deck.set_image(self.context, image, **kw)

    async def set_title(self, title, **kw):
        await self.deck.set_title(self.context, title, **kw)

    # --- lifecycle hooks (override what you need) ----------------------
    async def on_appear(self):
        pass

    async def on_disappear(self):
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def on_key_down(self, payload):
        pass

    async def on_key_up(self, payload):
        pass

    async def on_dial_rotate(self, payload):
        pass

    async def on_dial_down(self, payload):
        pass

    async def on_dial_up(self, payload):
        pass

    async def on_touch_tap(self, payload):
        pass

    async def on_settings(self, settings):
        self.settings = settings or {}

    async def on_pi_message(self, payload):
        """Message from the property inspector (sendToPlugin)."""
        pass

    async def on_title_parameters(self, payload):
        """User changed title text/font/size/alignment in the OpenDeck UI."""
        self.title_params = payload.get("titleParameters", {}) or {}
