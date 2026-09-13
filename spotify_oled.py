#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "pyserial",
# ]
# ///
"""Spotify to Arduino OLED bridge — centered synced lyrics + footer meta.

Pipeline (kept deliberately simple and reliable):
- Poll playerctl every PLAYER_POLL_INTERVAL for a full snapshot (including
  mpris:trackid so track changes are detected reliably).
- Interpolate playback position from the monotonic clock between polls so
  the OLED reveal stays smooth and accurate at render tick speed.
- Fetch lyrics from LRCLIB in a BACKGROUND thread: the serial loop never
  blocks on the network, so track changes update instantly on the OLED and
  there is no visible latency.
- Word-level reveal: Enhanced LRC word tags if present, else even spread.

Serial protocol (same as sketch_sep13a.ino):
$status;volume;title;artist;position;duration;progress;l1_rev;l1_full;l2_rev;l2_full\n
"""

from __future__ import annotations

import sys
import time
import argparse
import subprocess
import threading
import urllib.request
import urllib.parse
import json
import re
import serial
import serial.tools.list_ports

# ── constants ────────────────────────────────────────────────────────────
OLED_MAX_CHARS = 18        # single-line capacity (7x13 font on 128px)
OLED_MAX_CHARS_2LINE = 21  # per-line capacity for two-line mode (6x10 font)
PLAYER_POLL_INTERVAL = 0.5  # seconds between playerctl subprocess calls
INTERPOLATE_TICK = 0.05    # render tick (50ms ~= 20 FPS reveal)

# ── snapshot cache ───────────────────────────────────────────────────────
_snap_lock = threading.Lock()
_last_status: int = -1          # -1 unknown, 0 paused, 1 playing
_last_pos_us: int = 0           # Spotify-reported position at anchor
_last_len_us: int = 0
_last_anchor_mono: float = 0.0  # time.monotonic() when anchor was recorded
_last_title: str = ""
_last_artist: str = ""
_last_volume: int = 0
_last_raw_title: str = ""
_last_raw_artist: str = ""
_last_trackid: str = ""

# ── lyrics cache (threaded fetch) ────────────────────────────────────────
_lyrics_lock = threading.Lock()
_cache_trackid = ""
_cache_lines: list[dict] = []
_fetch_target = None        # (trackid, raw_title, raw_artist, len_sec)


def _snap_update(status=None, pos_us=None, len_us=None, title=None, artist=None,
                 volume=None, raw_title=None, raw_artist=None, trackid=None,
                 anchor=None):
    global _last_status, _last_pos_us, _last_len_us, _last_anchor_mono
    global _last_title, _last_artist, _last_volume
    global _last_raw_title, _last_raw_artist, _last_trackid
    with _snap_lock:
        if status is not None:
            _last_status = status
        if pos_us is not None:
            _last_pos_us = pos_us
        if len_us is not None:
            _last_len_us = len_us
        if title is not None:
            _last_title = sanitize_text(title, max_len=24)
        if artist is not None:
            _last_artist = sanitize_text(artist, max_len=24)
        if volume is not None:
            _last_volume = volume
        if raw_title is not None:
            _last_raw_title = raw_title
        if raw_artist is not None:
            _last_raw_artist = raw_artist
        if trackid is not None:
            _last_trackid = trackid
        if anchor is not None:
            _last_anchor_mono = anchor


def _snap_read():
    with _snap_lock:
        return (_last_status, _last_pos_us, _last_len_us, _last_anchor_mono,
                _last_title, _last_artist, _last_volume,
                _last_raw_title, _last_raw_artist, _last_trackid)


def interpolated_position() -> float:
    """Estimated current position (sec) via monotonic clock (playing only)."""
    status, pos_us, len_us, anchor, *_ = _snap_read()
    if status != 1:
        return pos_us / 1_000_000.0
    est = pos_us / 1_000_000.0 + (time.monotonic() - anchor)
    if len_us > 0:
        est = min(est, len_us / 1_000_000.0)
    return est


def find_arduino_port():
    """Auto-detect Arduino serial port."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        id_str = f"{p.description} {p.hwid} {p.manufacturer} {p.device}".lower()
        if any(k in id_str for k in ("arduino", "ttyacm", "ttyusb")):
            return p.device
    for p in ports:
        if any(k in p.device.lower() for k in ("acm", "usb")):
            return p.device
    return None


def sanitize_text(text: str, max_len: int = 26) -> str:
    """Sanitize text to printable ASCII and truncate."""
    if not text:
        return ""
    text = text.replace(";", " ").replace("\u266a", "~")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = "".join(c for c in text if 32 <= ord(c) <= 126).strip()
    return text[:max_len]


# ── LRC parsing (Enhanced <mm:ss.xx> + standard) ─────────────────────────

def parse_lrc_time(ts: str) -> float:
    """Parse LRC timestamp mm:ss.xx or mm:ss.xxx -> seconds."""
    m = re.match(r"(\d+):(\d+(?:\.\d+)?)", ts)
    if not m:
        return 0.0
    return int(m.group(1)) * 60.0 + float(m.group(2))


_ENHANCED_RE = re.compile(r"<(\d+:\d+(?:\.\d+)?)>\s*([^<]*)")
_STD_RE = re.compile(r"\[(\d+:\d+(?:\.\d+)?)\]\s*(.*)")


def split_char_timelines(words: list, t_start: float, t_end: float, n_l1: int):
    """Build one globally monotonic char timeline, then split it by display line.

    Words are the line's full time-sorted word list; the first n_l1 go to
    display line 1, the rest to line 2. This keeps char reveal strictly in
    singing order (line 2 never starts before line 1 finishes its last word).

    Returns (l1_chars, l2_chars).
    """
    l1_chars: list = []
    l2_chars: list = []
    for wi, (wtext, wt) in enumerate(words):
        if wi + 1 < len(words):
            nxt = words[wi + 1][1]
        else:
            nxt = t_end
        span = max(0.15, nxt - wt)
        ch = wtext + (" " if wi + 1 < len(words) else "")
        n = max(1, len(ch))
        target = l1_chars if wi < n_l1 else l2_chars
        for ci, c in enumerate(ch):
            target.append((c, wt + (ci / n) * span * 0.9))
    return l1_chars, l2_chars


def _parse_enhanced_line(text: str):
    """Parse Enhanced LRC word tags <mm:ss.xx>word.

    Returns (line_time_sec, [(word_text, word_time_sec), ...]) or None.
    """
    m = _STD_RE.match(text)
    if not m:
        return None
    line_t = parse_lrc_time(m.group(1))
    body = m.group(2)
    if not body:
        return None
    words = []
    for wm in _ENHANCED_RE.finditer(body):
        wt = parse_lrc_time(wm.group(1))
        wtxt = wm.group(2).strip()
        if wtxt:
            words.append((wtxt, wt))
    if not words:
        return None
    return (line_t, words)


def _parse_standard_line(text: str):
    """Parse standard LRC [mm:ss.xx]text -> (line_time_sec, full_text)."""
    m = _STD_RE.match(text)
    if not m:
        return None
    body = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", m.group(2)).strip()
    return (parse_lrc_time(m.group(1)), body) if body else None


def _split_line_to_display(text: str):
    """Split text into 1-2 display lines at a clean word boundary.

    Returns (l1_text, l2_text). Words are kept contiguous (no reordering),
    so a word-ordered lyric list can be split at len(l1_text.split()).
    """
    words = text.split()
    if not words:
        return "", ""
    if len(text) <= OLED_MAX_CHARS:
        return text, ""
    # Balanced char-count split at a word boundary (never mid-word).
    total = sum(len(w) + 1 for w in words) - 1
    mid = total / 2
    acc = 0
    cut = len(words)
    for i, w in enumerate(words):
        if i > 0 and acc >= mid:
            cut = i
            break
        acc += len(w) + 1
    # Guarantee both lines get content (multi-line mode)
    cut = max(1, min(cut, len(words) - 1))
    return " ".join(words[:cut]), " ".join(words[cut:])


def parse_lrc_precise(lrc_text: str) -> list[dict]:
    """Parse LRC text into per-line structures with word-level timing.

    Priority:
    1. Enhanced LRC (<mm:ss.xx>word) — real word timestamps from the file
    2. Standard LRC (line-level) — words distributed evenly across line gap
    """
    raw = [l.strip() for l in lrc_text.splitlines() if l.strip()]

    # Pass 1: parse every line as enhanced or standard. Mixed files are common
    # (some lines word-tagged, some not), so keep both kinds.
    parsed: list[dict] = []
    for line in raw:
        enh = _parse_enhanced_line(line)
        if enh is not None:
            line_t, words = enh
            parsed.append({
                "t_start": line_t,
                "words": words,
                "full_text": " ".join(w for w, _ in words),
                "enhanced": True,
            })
            continue
        std = _parse_standard_line(line)
        if std is not None:
            line_t, full = std
            parsed.append({
                "t_start": line_t,
                "words": [],
                "full_text": full,
                "enhanced": False,
                "_raw_words": full.split(),
            })

    parsed.sort(key=lambda x: x["t_start"])

    # Fill word timings for standard LRC (evenly distributed in the gap)
    for i, entry in enumerate(parsed):
        if entry.get("enhanced") or entry.get("_raw_words") is None:
            continue
        raw_words = entry.pop("_raw_words", [])
        if not raw_words:
            continue
        gap = 3.0
        if i + 1 < len(parsed):
            gap = max(1.0, parsed[i + 1]["t_start"] - entry["t_start"])
        sing_dur = max(0.5, gap * 0.85)
        n = len(raw_words)
        entry["words"] = [
            (w, entry["t_start"] + (j / n) * sing_dur) for j, w in enumerate(raw_words)
        ]

    # Line end = next line start (clamped), last line gets +5s
    for i, entry in enumerate(parsed):
        entry["t_end"] = parsed[i + 1]["t_start"] if i + 1 < len(parsed) \
            else entry["t_start"] + 5.0

    # Split into display lines and match words to l1/l2
    for entry in parsed:
        l1_text, l2_text = _split_line_to_display(entry["full_text"])
        entry["l1_text"] = l1_text
        entry["l2_text"] = l2_text

        words = entry["words"]
        if l2_text:
            n_l1 = len(l1_text.split())
            words_l1 = words[:n_l1]
            words_l2 = words[n_l1:]
        else:
            words_l1 = words
            words_l2 = []

        entry["words_l1"] = words_l1
        entry["words_l2"] = words_l2

        t_start = entry["t_start"]
        t_end = entry["t_end"]
        if words_l2:
            n_l1 = len(words_l1)
            entry["l1_chars"], entry["l2_chars"] = split_char_timelines(
                words, t_start, t_end, n_l1
            )
        else:
            entry["l1_chars"] = split_char_timelines(words, t_start, t_end, len(words))[0]
            entry["l2_chars"] = []

    return parsed


# ── LRCLIB fetch ─────────────────────────────────────────────────────────

def generate_synthetic_synced_lyrics(plain_text: str, total_duration: float) -> list:
    """Build timed lines from plain lyrics by distributing across duration."""
    lines = [l.strip() for l in plain_text.splitlines() if l.strip()]
    if not lines:
        return []
    dur = float(total_duration) if total_duration > 30 else 240.0
    intro = min(22.0, max(8.0, dur * 0.08))
    outro = min(20.0, max(10.0, dur * 0.06))
    singing_time = max(10.0, dur - intro - outro)

    weights = [max(8, len(l)) for l in lines]
    tot_weight = sum(weights) or 1.0

    timed = []
    cum = intro
    for line, w in zip(lines, weights):
        line_dur = (w / tot_weight) * singing_time
        timed.append(f"[{int(cum // 60):02d}:{cum % 60:05.2f}] {line}")
        cum += line_dur

    return parse_lrc_precise("\n".join(timed))


def fetch_lyrics(title: str, artist: str, duration_sec: float) -> list[dict]:
    """Fetch synced lyrics from LRCLIB (with search + plain-text fallback).

    The exact-match /api/get and the /api/search run CONCURRENTLY; whichever
    yields usable lyrics first wins. This keeps track-change loads fast even
    when the exact match misses (remaster suffixes etc).
    """
    headers = {"User-Agent": "SpotifyOLED/2.0"}
    results: list[dict] = []
    lock = threading.Lock()

    def _try_fetch(url: str):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _use(line_list):
        if line_list:
            with lock:
                results.append(line_list)

    def _run():
        params = {"artist_name": artist, "track_name": title}
        if duration_sec > 0:
            params["duration"] = str(int(duration_sec))
        data = _try_fetch("https://lrclib.net/api/get?" + urllib.parse.urlencode(params))
        if data:
            synced = data.get("syncedLyrics")
            if synced:
                _use(parse_lrc_precise(synced))
                return
            plain = data.get("plainLyrics")
            if plain:
                _use(generate_synthetic_synced_lyrics(
                    plain, duration_sec or data.get("duration") or 240.0))
                return

    def _search():
        clean_title = title.split(" - ")[0].split(" (")[0].strip()
        results_data = _try_fetch(
            "https://lrclib.net/api/search?"
            + urllib.parse.urlencode({"q": f"{clean_title} {artist}"})
        )
        if not (results_data and isinstance(results_data, list)):
            return
        for item in results_data:
            synced = item.get("syncedLyrics")
            if synced:
                _use(parse_lrc_precise(synced))
                return
            plain = item.get("plainLyrics")
            if plain:
                _use(generate_synthetic_synced_lyrics(
                    plain, duration_sec or item.get("duration") or 240.0))
                return

    threads = [threading.Thread(target=_run, daemon=True),
               threading.Thread(target=_search, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)

    return results[0] if results else []


# ── background lyrics loader ─────────────────────────────────────────────

def _request_lyrics(trackid: str, raw_title: str, raw_artist: str, len_sec: float):
    """Queue a lyrics fetch for the current track (idempotent, non-blocking)."""
    global _fetch_target
    if not trackid:
        return
    with _lyrics_lock:
        if _cache_trackid == trackid:
            return
        if _fetch_target is not None and _fetch_target[0] == trackid:
            return
        _fetch_target = (trackid, raw_title, raw_artist, len_sec)


def _lyrics_worker():
    """Background thread: fetch LRCLIB lyrics without blocking the serial loop."""
    global _fetch_target, _cache_trackid, _cache_lines
    while True:
        with _lyrics_lock:
            target = _fetch_target
        if target is None:
            time.sleep(0.1)
            continue

        trackid, raw_title, raw_artist, len_sec = target
        lines = fetch_lyrics(raw_title, raw_artist, len_sec)

        with _lyrics_lock:
            if _cache_trackid == trackid:
                continue  # already superseded
            _cache_trackid = trackid
            _cache_lines = lines
            if _fetch_target is not None and _fetch_target[0] == trackid:
                _fetch_target = None
        print(f"\n[lyrics] '{sanitize_text(raw_title, 30)}' - "
              f"'{sanitize_text(raw_artist, 30)}': {len(lines)} lines")


def start_lyrics_loader():
    threading.Thread(target=_lyrics_worker, daemon=True, name="lyrics-loader").start()


def get_cached_lyrics(trackid: str) -> list[dict]:
    """Return parsed lyrics only if they belong to the current track."""
    with _lyrics_lock:
        if _cache_trackid == trackid:
            return _cache_lines
        return []


# ── playerctl polling ────────────────────────────────────────────────────

def poll_player():
    """Poll playerctl for a full metadata snapshot. Returns True on success."""
    cmd = [
        "playerctl", "-p", "spotify", "metadata",
        "--format", "{{status}}|{{volume}}|{{title}}|{{artist}}|{{position}}|"
                     "{{mpris:length}}|{{mpris:trackid}}"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
        if res.returncode != 0 or not res.stdout.strip():
            _snap_update(status=-1)
            return False

        parts = res.stdout.strip().split("|")
        if len(parts) < 7:
            _snap_update(status=-1)
            return False

        status_str, vol_str, raw_title, raw_artist, pos_str, len_str, tstr = parts[:7]
        status = 1 if status_str.strip().lower() == "playing" else 0

        try:
            volume = int(float(vol_str.strip()) * 100)
        except (ValueError, TypeError):
            volume = 0
        try:
            pos_us = int(float(pos_str.strip()))
        except (ValueError, TypeError):
            pos_us = 0
        try:
            len_us = int(float(len_str.strip()))
        except (ValueError, TypeError):
            len_us = 0
        trackid = tstr.strip()

        _snap_update(status=status, pos_us=pos_us, len_us=len_us,
                     title=raw_title, artist=raw_artist,
                     volume=volume,
                     raw_title=raw_title.strip(), raw_artist=raw_artist.strip(),
                     trackid=trackid, anchor=time.monotonic())

        _request_lyrics(trackid, raw_title.strip(), raw_artist.strip(),
                        len_us / 1_000_000.0)
        return True
    except Exception:
        _snap_update(status=-1)
        return False


# ── lyric lookup ─────────────────────────────────────────────────────────

def chars_revealed(chars: list, pos_sec: float) -> str:
    """Concatenate all chars whose reveal time has passed (typewriter)."""
    out = []
    for c, ct in chars:
        if pos_sec >= ct:
            out.append(c)
    return "".join(out)


def get_active_lyrics(lines: list, pos_sec: float, default_title: str = "") -> tuple:
    """Return (l1_rev, l1_full, l2_rev, l2_full) at current position.

    Char-by-char reveal from word timestamps. During instrumental gaps the
    previous (fully revealed) line stays visible so lyrics never blink off.
    """
    if not lines:
        return default_title, default_title, "", ""

    if pos_sec < lines[0]["t_start"]:
        return "(Intro)", "(Intro)", "", ""

    if pos_sec >= lines[-1]["t_end"]:
        last = lines[-1]
        l1 = chars_revealed(last["l1_chars"], pos_sec) or last["l1_text"]
        l2 = chars_revealed(last["l2_chars"], pos_sec) or last["l2_text"]
        return l1, last["l1_text"], l2, last["l2_text"]

    line = None
    for l in lines:
        if l["t_start"] <= pos_sec:
            line = l
    if line is None:
        return "", "", "", ""

    l1_rev = chars_revealed(line["l1_chars"], pos_sec)
    l2_rev = chars_revealed(line["l2_chars"], pos_sec)
    return l1_rev, line["l1_text"], l2_rev, line["l2_text"]


# ── display data builder ─────────────────────────────────────────────────

def build_display_data(lead_offset: float = 0.0) -> dict | None:
    """Build a full display packet from the snapshot + interpolated time."""
    status, pos_us, len_us, anchor, title, artist, volume, raw_title, raw_artist, trackid = \
        _snap_read()

    if status == -1:
        return None

    base_pos = max(0.0, interpolated_position())
    lyric_pos = max(0.0, base_pos + lead_offset)

    lines = get_cached_lyrics(trackid)

    pos_sec = base_pos
    pos_int = int(pos_sec)
    len_norm = max(pos_sec, len_us / 1_000_000.0)
    len_int = int(len_norm)
    rem_int = max(0, len_int - pos_int)
    progress = int((pos_sec / len_norm) * 100) if len_norm > 0 else 0
    progress = max(0, min(100, progress))

    l1_rev, l1_full, l2_rev, l2_full = get_active_lyrics(
        lines, lyric_pos, default_title=""
    )

    return {
        "status": status,
        "volume": volume,
        "title": title or "Unknown Title",
        "artist": artist or "Unknown Artist",
        "pos_sec": pos_sec,
        "pos_int": pos_int,
        "position": f"{pos_int // 60:02d}:{pos_int % 60:02d}",
        "remaining": f"{rem_int // 60:02d}:{rem_int % 60:02d}",
        "duration": f"{len_int // 60:02d}:{len_int % 60:02d}",
        "progress": progress,
        "l1_rev": l1_rev,
        "l1_full": l1_full,
        "l2_rev": l2_rev,
        "l2_full": l2_full,
        "trackid": trackid,
    }


# ── serial protocol ──────────────────────────────────────────────────────

def format_packet(data: dict | None) -> str:
    """Serialize framed payload with '$' prefix."""
    if not data:
        return "$0;0;No Music;Spotify;00:00;00:00;0;;;;\n"
    return (
        f"${data['status']};{data['volume']};{data['title']};{data['artist']};"
        f"{data['position']};{data['duration']};{data['progress']};"
        f"{data['l1_rev']};{data['l1_full']};{data['l2_rev']};{data['l2_full']}\n"
    )


# ── main loop ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Spotify OLED bridge - centered synced karaoke lyrics."
    )
    parser.add_argument("-p", "--port", type=str, default=None, help="Serial port")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("-o", "--offset", type=float, default=0.0,
                        help="Vocal offset in seconds (positive = lyrics earlier)")
    parser.add_argument("--poll", type=float, default=PLAYER_POLL_INTERVAL,
                        help="Seconds between playerctl polls (default 0.5)")
    args = parser.parse_args()

    port = args.port or find_arduino_port()
    if not port:
        print("[!] Arduino port not found. Specify with --port.")
        sys.exit(1)

    start_lyrics_loader()
    poll_interval = args.poll

    print(f"[*] Connecting to {port} (Baud: {args.baud}, Offset: {args.offset}s)...")

    while True:
        try:
            with serial.Serial(port, args.baud, timeout=1) as ser:
                print(f"[+] Connected to {port}. Ready.")
                time.sleep(2)

                last_state = None
                last_pos_int = -1
                last_status = -1
                last_trackid = ""
                last_send_time = 0.0
                last_poll_mono = -1.0

                while True:
                    now = time.time()
                    now_mono = time.monotonic()

                    # Poll playerctl; interpolate between polls
                    if now_mono - last_poll_mono >= poll_interval or last_poll_mono < 0:
                        last_poll_mono = now_mono
                        poll_player()

                    data = build_display_data(lead_offset=args.offset)

                    should_send = False
                    if data:
                        current_state = (data["l1_rev"], data["l1_full"],
                                         data["l2_rev"], data["l2_full"])
                        is_new_track = data["trackid"] != last_trackid
                        # Instant send on word reveal / line transition / track change
                        if current_state != last_state or is_new_track:
                            should_send = True
                        elif data["status"] != last_status:
                            should_send = True
                        elif data["pos_int"] != last_pos_int and (now - last_send_time >= 0.8):
                            should_send = True
                        elif now - last_send_time >= 2.0:
                            should_send = True
                    else:
                        if last_status != 0 or (now - last_send_time >= 2.0):
                            should_send = True

                    if should_send:
                        ser.write(format_packet(data).encode("utf-8"))

                        if data:
                            last_state = (data["l1_rev"], data["l1_full"],
                                          data["l2_rev"], data["l2_full"])
                            last_pos_int = data["pos_int"]
                            last_status = data["status"]
                            last_trackid = data["trackid"]
                        else:
                            last_state = None
                            last_pos_int = -1
                            last_status = 0
                            last_trackid = ""

                        last_send_time = now

                        status_tag = "PLAYING" if data and data["status"] == 1 else "PAUSED"
                        title = data["title"] if data else "No Music"
                        pos = data["position"] if data else "00:00"
                        dur = data["duration"] if data else "00:00"
                        l1 = data["l1_rev"] if data else ""
                        l2 = data["l2_rev"] if data else ""
                        combined = f"{l1} / {l2}" if l2 else l1
                        print(f"\r[{status_tag}] {title} | {combined:<22} [{pos}/{dur}]  ",
                              end="", flush=True)

                    time.sleep(INTERPOLATE_TICK)

        except serial.SerialException as e:
            print(f"\n[!] Serial error: {e}. Retrying in 3s...")
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[*] Exiting.")
            break


if __name__ == "__main__":
    main()