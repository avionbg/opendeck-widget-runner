"""Widget Runner: one process, many OpenDeck widgets.

Launched by OpenDeck as:
  runner.py -port N -pluginUUID X -registerEvent registerPlugin -info {json}
"""
import asyncio
import importlib
import json
import logging
import os
import pkgutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

handler = RotatingFileHandler(HERE / "runner.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
logging.basicConfig(level=logging.DEBUG if (os.environ.get("WIDGETRUNNER_DEBUG") or (HERE / "debug").exists()) else logging.INFO, handlers=[handler],
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("widgetrunner")

import websockets  # noqa: E402
from widgetlib import Deck, Widget  # noqa: E402


def parse_args(argv):
    out = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("-") and i + 1 < len(argv):
            out[argv[i].lstrip("-")] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def load_widgets():
    """Import every module in widgets/ and collect Widget subclasses keyed by action UUID."""
    registry = {}
    import widgets as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        try:
            module = importlib.import_module(f"widgets.{mod.name}")
        except Exception:
            log.exception("failed to import widget module %s", mod.name)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Widget) and obj is not Widget and obj.action:
                registry[obj.action] = obj
                log.info("registered widget %s -> %s.%s", obj.action, mod.name, obj.__name__)
    return registry


EVENT_METHODS = {
    "keyDown": "on_key_down",
    "keyUp": "on_key_up",
    "dialRotate": "on_dial_rotate",
    "dialDown": "on_dial_down",
    "dialUp": "on_dial_up",
    "touchTap": "on_touch_tap",
    "sendToPlugin": "on_pi_message",
    "titleParametersDidChange": "on_title_parameters",
}


async def main():
    args = parse_args(sys.argv[1:])
    log.info("starting, argv=%s", sys.argv[1:])
    port = args["port"]
    uuid = args["pluginUUID"]
    register_event = args.get("registerEvent", "registerPlugin")

    registry = load_widgets()
    instances: dict[str, Widget] = {}

    async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as ws:
        deck = Deck(ws)
        await deck.send(register_event, uuid=uuid)
        log.info("registered as %s on port %s", uuid, port)

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-json message: %r", raw[:200])
                continue
            event = msg.get("event")
            context = msg.get("context")
            action = msg.get("action")
            payload = msg.get("payload", {}) or {}

            try:
                if event in ("willAppear", "willDisappear", "titleParametersDidChange",
                             "propertyInspectorDidAppear", "propertyInspectorDidDisappear", "didReceiveSettings"):
                    log.info("%s %s %s", event, action, context)
                if event == "willAppear":
                    cls = registry.get(action)
                    if cls is None:
                        log.warning("no widget for action %s", action)
                        continue
                    old = instances.pop(context, None)
                    if old:
                        await old.on_disappear()
                    inst = instances[context] = cls(deck, context, payload)
                    inst.device = msg.get("device")
                    await inst.on_appear()
                elif event == "willDisappear":
                    inst = instances.pop(context, None)
                    if inst:
                        await inst.on_disappear()
                elif event == "didReceiveSettings":
                    inst = instances.get(context)
                    if inst:
                        await inst.on_settings(payload.get("settings", {}))
                elif event in EVENT_METHODS:
                    inst = instances.get(context)
                    if inst:
                        await getattr(inst, EVENT_METHODS[event])(payload)
                else:
                    log.debug("unhandled event %s", event)
            except Exception:
                log.exception("error handling %s for %s", event, context)

    log.info("websocket closed, exiting")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except websockets.ConnectionClosed as e:
        log.info("connection closed by OpenDeck (%s), exiting", e)
    except Exception:
        log.exception("fatal")
        raise
