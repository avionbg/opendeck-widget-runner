"""Generic defaults for machine-specific paths and settings.

These are safe placeholders that work for a fresh install. To adapt the plugin to your machine
without touching the code, copy ``userconfig.example.py`` to ``userconfig.py`` and edit it; the
values there override the ones below. ``userconfig.py`` is git-ignored, so your personal paths never
end up in the repository. Everything here can also be overridden per button in its property inspector.
"""

# Radio widget
MPV_PATH = "mpv"          # mpv executable; "mpv" uses the one on PATH. Point to mpv.exe if portable.
M3U_PATH = ""             # a local .m3u playlist for the "Playlist" source (set it in the panel too)
M3U_GROUP = ""            # only this group-title from the .m3u ("" = all groups)

# Open on TV widget
TV_IP = ""                # your Android TV's IP for adb (e.g. "192.168.1.50"); set it in the panel too

# Track Info widget — MusicBrainz asks for a contact in the User-Agent (a URL or e-mail)
MUSICBRAINZ_CONTACT = "https://github.com/opendeck-widget-runner"

try:
    from userconfig import *  # noqa: F401,F403  local, git-ignored personal overrides
except ImportError:
    pass
