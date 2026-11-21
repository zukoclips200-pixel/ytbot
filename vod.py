"""Clip regular YouTube videos: yt-dlp (download only the needed section) + ffmpeg."""
import asyncio, json, os, re, statistics, uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import clipper, editor

CLIP_DIR = clipper.CLIP_DIR
MAX_CLIP = 90
CLIP_LEN = int(os.getenv("VOD_CLIP_SECONDS", 30))
MAX_VIDEO_SECONDS = 4 * 3600
_run = clipper._run
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


COOKIE_FILE = Path(os.getenv("DB_PATH", "data.db")).resolve().parent / "yt_cookies.txt"


def clean_cookies(text: str) -> str:
    """Turns pasted cookies into the exact format yt-dlp needs.
    Phones often turn the tabs into spaces, which makes yt-dlp ignore the file."""
    text = text.replace("\\n", "\n").replace("\r", "")
    lines = ["# Netscape HTTP Cookie File"]
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t") if line.count("\t") >= 6 else line.split(None, 6)
        if len(parts) == 6:          # cookie with an empty value
            parts.append("")
        if len(parts) == 7:
            lines.append("\t".join(p.strip() for p in parts))
    return "\n".join(lines) + "\n"


def save_cookies(text: str) -> int:
    """Saves cookies sent with /ytcookies. Returns how many cookies were found."""
    cleaned = clean_cookies(text)
    count = len(cleaned.splitlines()) - 1
    if count:
        COOKIE_FILE.write_text(cleaned)
    return count


def cookie_args():
    """Uses cookies from /ytcookies, or from a YT_COOKIES variable on Railway, as a YouTube login."""
    if not COOKIE_FILE.exists():
        c = os.getenv("YT_COOKIES", "").strip()
        if not c or not save_cookies(c):
            return []
    return ["--cookies", str(COOKIE_FILE)]


def check_url(url: str):
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or u.hostname not in _HOSTS:
        raise ValueError("Please send a youtube.com or youtu.be link.")


def video_id(url: str):
    u = urlparse(url.strip())
    if u.hostname == "youtu.be":
        return u.path.strip("/")[:20]
    if "v" in parse_qs(u.query):
        return parse_qs(u.query)["v"][0][:20]
    m = re.search(r"/(shorts|live|embed)/([\w-]{6,20})", u.path)
    return m.group(2) if m else None


def parse_ts(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        raise ValueError(f"Bad time '{s}'. Use 1:23:45, 83:45 or seconds.")
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"Bad time '{s}'. Use 1:23:45, 83:45 or seconds.")
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def fmt_ts(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def info(url: str) -> dict:
    rc, out, err = await _run("yt-dlp", *cookie_args(), "--dump-single-json", "--no-warnings", "--no-playlist", "--", url)
    if rc:
        raise RuntimeError("Couldn't read that video: " + err.strip()[-200:])
    return json.loads(out)


async def shrink(src: Path, dst: Path, limit_mb: float):
    dur = await editor.duration(src)
    vb = int(min(5000, max(500, limit_mb * 8000 * 0.92 / max(dur, 1) - 128)))
    async with editor.ENCODE_LOCK:
        rc, _, err = await _run(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
            "-b:v", f"{vb}k", "-maxrate", f"{int(vb * 1.3)}k", "-bufsize", f"{vb * 2}k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst))
    if rc:
        raise RuntimeError("Encode failed: " + err[-200:])


async def cut(url: str, start: float, end: float, limit_mb: float = 9.0) -> Path:
    """Downloads ONLY start→end of the video and returns a Discord-sized mp4."""
    check_url(url)
    start = max(0.0, start)
    cid = uuid.uuid4().hex[:8]
    out = CLIP_DIR / f"yt_{video_id(url) or 'video'}_{int(start)}_{cid}.mp4"
    raw = CLIP_DIR / f"raw_{cid}"
    rc, _, err = await _run(
        "yt-dlp", *cookie_args(), "--no-playlist", "--no-warnings",
        "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--download-sections", f"*{start:.0f}-{end:.0f}", "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4", "-o", f"{raw}.%(ext)s", "--", url)
    files = list(CLIP_DIR.glob(f"raw_{cid}.*"))
    try:
        if rc or not files:
            raise RuntimeError("Download failed: " + err.strip()[-200:])
        await shrink(files[0], out, limit_mb)
    finally:
        for f in CLIP_DIR.glob(f"raw_{cid}.*"):
            f.unlink(missing_ok=True)
    return out


# ───────────── finding highlights ─────────────
async def loudness_series(path: Path):
    """[(second, RMS dB)] — one value per second of audio."""
    rc, out, _ = await _run(
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
        "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-")
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", out)]
    vals = re.findall(r"RMS_level=(-?[\d.]+|-?inf)", out)
    series = []
    for t, v in zip(times, vals):
        try:
            db = float(v)
        except ValueError:
            continue
        series.append((t, max(db, -90.0)))
    return series


def surge_scores(series, smooth=5, window=60):
    """Score = how much louder a moment is than its surroundings (not just 'loud')."""
    if not series:
        return []
    db = [v for _, v in series]
    sm = [statistics.fmean(db[max(0, i - smooth // 2): i + smooth // 2 + 1]) for i in range(len(db))]
    out = []
    for i, (t, _) in enumerate(series):
        base = statistics.median(sm[max(0, i - window): i + window + 1])
        out.append((t, sm[i] - base))
    return out


def pick_peaks(scored, count, min_gap=45, skip_start=15):
    chosen = []
    for t, v in sorted(scored, key=lambda x: -x[1]):
        if t < skip_start:
            continue
        if all(abs(t - c) >= min_gap for c, _ in chosen):
            chosen.append((t, v))
        if len(chosen) >= count:
            break
    return chosen


async def find_moments(url: str, count: int):
    """Returns ([(start, end, reason)], method). Uses YouTube's 'Most replayed' graph when
    the video has one, otherwise audio surges."""
    check_url(url)
    meta = await info(url)
    if meta.get("is_live"):
        raise RuntimeError("That's a live stream. Add the channel with /addstreamer instead.")
    dur = float(meta.get("duration") or 0)
    if not dur or dur > MAX_VIDEO_SECONDS:
        raise RuntimeError("Video is too long (max 4 hours).")

    heat = meta.get("heatmap")
    if heat:
        scored = [((h["start_time"] + h["end_time"]) / 2, h["value"]) for h in heat]
        method, why = "Most Replayed", "most replayed"
    else:
        cid = uuid.uuid4().hex[:8]
        rc, _, err = await _run("yt-dlp", *cookie_args(), "--no-playlist", "--no-warnings", "-f", "bestaudio",
                                "-o", str(CLIP_DIR / f"aud_{cid}.%(ext)s"), "--", url)
        files = list(CLIP_DIR.glob(f"aud_{cid}.*"))
        try:
            if rc or not files:
                raise RuntimeError("Audio download failed: " + err.strip()[-200:])
            scored = surge_scores(await loudness_series(files[0]))
        finally:
            for f in CLIP_DIR.glob(f"aud_{cid}.*"):
                f.unlink(missing_ok=True)
        method, why = "audio peaks", "loud moment"

    peaks = pick_peaks(scored, count)
    if not peaks:
        raise RuntimeError("Couldn't find any standout moments.")
    moments = []
    for t, _ in sorted(peaks):
        s = max(0.0, t - CLIP_LEN / 2)
        e = min(dur, s + CLIP_LEN)
        s = max(0.0, e - CLIP_LEN)
        moments.append((s, e, why))
    return moments, method
