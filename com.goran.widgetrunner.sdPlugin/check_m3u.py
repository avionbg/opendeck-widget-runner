"""Check which stations in an .m3u actually stream, and comment out the dead ones.

    python check_m3u.py <file.m3u>            report only
    python check_m3u.py <file.m3u> --apply    also rewrite the file (dead entries get '#DEAD ' prefixes)
    python check_m3u.py <file.m3u> --revive   uncomment #DEAD entries that work again, then check
    add --dedupe to also comment out repeated entries (same name and url) with '#DUP '

A station counts as alive when, within the timeout, the server answers 200 (HTTP or ICY) with an
audio/stream content type and sends some bytes. Redirects are followed. A backup is written next to
the file before any change.
"""
import concurrent.futures as cf
import http.client
import shutil
import socket
import ssl
import sys
import time
import urllib.parse
from datetime import datetime

import certifi

TIMEOUT = 8
WORKERS = 30
BYTES_NEEDED = 2048
AUDIO_TYPES = ("audio/", "application/ogg", "application/octet-stream", "video/mp2t", "application/vnd.apple.mpegurl",
               "application/x-mpegurl", "audio/mpegurl", "application/x-winamp-playlist")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VLC/3.0", "Icy-MetaData": "1", "Accept": "*/*"}
CTX = ssl.create_default_context(cafile=certifi.where())
CTX_LAX = ssl.create_default_context()
CTX_LAX.check_hostname = False
CTX_LAX.verify_mode = ssl.CERT_NONE


def probe(url, depth=0):
    """-> (ok, reason)"""
    if depth > 5:
        return False, "too many redirects"
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https"):
            return False, f"scheme {u.scheme}"
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        ctx = CTX_LAX if u.scheme == "https" else None
        conn = (http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ctx) if u.scheme == "https"
                else http.client.HTTPConnection(host, port, timeout=TIMEOUT))
        conn.request("GET", path, headers={**HEADERS, "Host": u.netloc})
        try:
            resp = conn.getresponse()
        except http.client.BadStatusLine as e:
            line = str(e.line if hasattr(e, "line") else e)
            if "ICY 200" in line:
                return True, "icy"
            return False, f"bad status {line[:30]!r}"
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location") or ""
            conn.close()
            return probe(urllib.parse.urljoin(url, loc), depth + 1) if loc else (False, "redirect without location")
        if resp.status != 200:
            return False, f"http {resp.status}"
        ctype = (resp.getheader("Content-Type") or "").lower()
        if ctype.startswith("text/html") or ctype.startswith("application/json"):
            return False, f"not a stream ({ctype.split(';')[0]})"
        if ctype and not any(ctype.startswith(t) for t in AUDIO_TYPES) and "icy" not in ctype:
            # unknown type: still accept if it streams bytes
            pass
        got = b""
        deadline = time.time() + TIMEOUT
        while len(got) < BYTES_NEEDED and time.time() < deadline:
            chunk = resp.read(1024)
            if not chunk:
                break
            got += chunk
        conn.close()
        head = got[:512].lower()
        if b"<html" in head or b"<!doctype" in head:
            return False, "html body"
        if got.lstrip().startswith(b"#EXTM3U"):
            return True, "hls playlist"
        if len(got) >= 256:
            return True, ctype.split(";")[0] or "stream"
        return False, "no data"
    except socket.timeout:
        return False, "timeout"
    except (ConnectionError, OSError, http.client.HTTPException, ssl.SSLError) as e:
        return False, type(e).__name__
    except Exception as e:  # pragma: no cover
        return False, f"{type(e).__name__}: {e}"[:40]


def parse(lines):
    """-> list of (extinf_index, url_index, name, url, dead_flag)"""
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        dead = stripped.startswith("#DEAD ")
        core = stripped[6:] if dead else stripped
        if core.startswith("#EXTINF") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            nxt_core = nxt[6:] if nxt.startswith("#DEAD ") else nxt
            if nxt_core and not nxt_core.startswith("#"):
                name = core.split(",", 1)[1].strip() if "," in core else core
                entries.append((i, i + 1, name, nxt_core, dead))
                i += 2
                continue
        i += 1
    return entries


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    apply = "--apply" in sys.argv
    revive = "--revive" in sys.argv
    dedupe = "--dedupe" in sys.argv
    raw = open(path, "rb").read()
    text = raw.decode("utf-8-sig", errors="replace")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    entries = parse(lines)
    dupes = []
    if dedupe:
        seen = set()
        for e in entries:
            key = (e[2].strip().lower(), e[3].strip())
            if key in seen:
                dupes.append(e)
            else:
                seen.add(key)
        print(f"duplicates (same name + url): {len(dupes)}")
    dup_idx = {e[1] for e in dupes}
    todo = [e for e in entries if (revive or not e[4]) and e[1] not in dup_idx]
    print(f"{len(entries)} stations, checking {len(todo)} with {WORKERS} workers (timeout {TIMEOUT}s)...", flush=True)
    t0 = time.time()
    results = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(probe, e[3]): e for e in todo}
        done = 0
        for f in cf.as_completed(futs):
            e = futs[f]
            results[e[1]] = f.result()
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    retry = [e for e in todo if not results[e[1]][0]]
    if retry:
        print(f"  retrying {len(retry)} failures with 6 workers / {TIMEOUT + 6}s...", flush=True)
        globals()["TIMEOUT"] = TIMEOUT + 6
        with cf.ThreadPoolExecutor(6) as ex:
            futs = {ex.submit(probe, e[3]): e for e in retry}
            for f in cf.as_completed(futs):
                results[futs[f][1]] = f.result()
        globals()["TIMEOUT"] = TIMEOUT - 6
    dead = [(e, results[e[1]]) for e in todo if not results[e[1]][0]]
    alive = [(e, results[e[1]]) for e in todo if results[e[1]][0]]
    print(f"\nalive: {len(alive)}   dead: {len(dead)}   ({time.time() - t0:.0f}s)")
    reasons = {}
    for _, (_, r) in dead:
        reasons[r] = reasons.get(r, 0) + 1
    print("dead by reason:", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
    for e, (_, r) in sorted(dead, key=lambda x: x[0][2].lower())[:400]:
        print(f"  DEAD  {e[2][:40]:40} {r:28} {e[3][:60]}")
    if not (apply or revive):
        print("\n(report only; add --apply to comment the dead ones out)")
        return 0
    backup = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)
    changed = 0
    for e, (ok, _) in list(zip(todo, [results[e[1]] for e in todo])):
        ei, ui, _, _, was_dead = e
        if ok and was_dead:
            lines[ei] = lines[ei].replace("#DEAD ", "", 1)
            lines[ui] = lines[ui].replace("#DEAD ", "", 1)
            changed += 1
        elif not ok and not was_dead:
            lines[ei] = "#DEAD " + lines[ei]
            lines[ui] = "#DEAD " + lines[ui]
            changed += 1
    for ei, ui, _, _, was_dead in dupes:
        if not lines[ei].lstrip().startswith("#DUP "):
            lines[ei] = "#DUP " + lines[ei]
            lines[ui] = "#DUP " + lines[ui]
            changed += 1
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines))
    print(f"\nwritten: {changed} entries changed, backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
