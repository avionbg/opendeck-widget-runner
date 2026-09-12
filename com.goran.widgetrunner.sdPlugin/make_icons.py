"""Generate icons/*.png. Run: python make_icons.py"""
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from widgets import clock, date, docker, fx, mic, monitor, pomodoro, usage, weather, zoom, profiles, audiolevel, spectrum, nowplaying, lyrics, seek, sleeptimer, beat, appvolume, crossplay, mediakeys, trackinfo, tv, radio  # noqa: E402

out = HERE / "icons"; out.mkdir(exist_ok=True)
now = datetime(2026, 9, 11, 10, 10, 30)
clock.render(now).save(out / "clock.png")
date.render(now).save(out / "date.png")
usage.render([("5H", 47, "1h"), ("7D", 70, "6h")], "Claude", show_name=False).save(out / "usage.png")
usage.render([("5H", 0, "5h"), ("7D", 29, "3d 9h")], "Codex", show_name=False).save(out / "codex.png")
weather.render("Light rain", 17.6, "Belgrade", "rain").save(out / "weather.png")
monitor.render([("CPU", 23, ""), ("RAM", 61, ""), ("GPU", 8, "41°C")]).save(out / "monitor.png")
pomodoro.render(25 * 60, 25 * 60, "focus", "idle").save(out / "pomodoro.png")
docker.render(2, 1).save(out / "docker.png")
mic.render(False).save(out / "mic.png")
fx.render("EUR", "RSD", 117.35, now).save(out / "fx.png")
zoom.render(2.0, True).save(out / "zoom.png")
profiles.render("opendeck", 2, 3).save(out / "profiles.png")
import math as _m
import numpy as _np
spectrum.render(_np.array([0.9,0.8,0.95,0.7,0.6,0.75,0.5,0.55,0.45,0.6,0.4,0.35,0.5,0.3,0.25,0.35,0.2,0.25,0.15,0.2,0.1,0.15,0.08,0.05]), _np.array([0.95,0.85,1,0.8,0.7,0.8,0.6,0.6,0.5,0.65,0.5,0.4,0.55,0.4,0.3,0.4,0.3,0.3,0.2,0.25,0.15,0.2,0.1,0.1])).save(out / "spectrum.png")
audiolevel.render([0.7, 0.5], [0.85, 0.65]).save(out / "audiolevel.png")
lyrics.render("Robert's got a quick hand", "He'll look around the room", "ok").save(out / "lyrics.png")
seek.render(83, 220, True).save(out / "seek.png")
tv.render("device", "Mazinga_TV").save(out / "tv.png")
radio.render("Naxi Radio", "Zdravko Colic - Ti si mi u krvi", True, "Domaca muzika").save(out / "radio.png")
trackinfo.render({"status": "ok", "album": "Funeral", "year": "2004", "genre": "indie rock"}).save(out / "trackinfo.png")
crossplay.render("spotify", "Arcade Fire - Afterlife").save(out / "crossplay.png")
mediakeys.render("toggle", True).save(out / "playpause.png")
mediakeys.render("next").save(out / "next.png")
mediakeys.render("prev").save(out / "prev.png")
sleeptimer.render(25 * 60 + 12, 30 * 60).save(out / "sleeptimer.png")
beat.render(0.7, 124, True).save(out / "beat.png")
appvolume.render("Chrome", 65, False).save(out / "appvolume.png")
nowplaying.render({"title": "Trembling Hands", "artist": "The Temper Trap", "status": "PLAYING", "position": 90, "duration": 269, "art": None}).save(out / "nowplaying.png")

p = Image.new("RGB", (144, 144), (40, 44, 60)); d = ImageDraw.Draw(p)
for (x, y), c in zip([(24, 24), (78, 24), (24, 78), (78, 78)],
                     [(230, 70, 70), (70, 180, 120), (80, 140, 230), (240, 190, 60)]):
    d.rounded_rectangle((x, y, x + 42, y + 42), radius=8, fill=c)
p.save(out / "plugin.png")
print("icons written:", sorted(f.name for f in out.iterdir()))
