"""Word-by-word captions and titles, drawn with ASS subtitles (wraps long text, nice boxes)."""
import asyncio, logging, os, re, time
from pathlib import Path

import requests

log = logging.getLogger("captions")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FONT = os.getenv("CAPTION_FONT", "DejaVu Sans")
HIGHLIGHT = "&H00FFFF&"      # yellow (ASS colours are blue-green-red)


async def _run(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


def _whisper_words(path: Path):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return []
    for attempt in range(3):
        with open(path, "rb") as f:
            r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                              files={"file": (path.name, f, "audio/mpeg")},
                              data=[("model", "whisper-large-v3-turbo"), ("response_format", "verbose_json"),
                                    ("timestamp_granularities[]", "word"), ("temperature", "0")],
                              timeout=120)
        if r.status_code == 429:
            wait = float(r.headers.get("retry-after") or 20)
            if wait > 60:
                return []
            time.sleep(wait + 1)
            continue
        if r.status_code >= 400:
            log.warning("caption transcription failed: %s %s", r.status_code, r.text[:150])
            return []
        return [(float(w["start"]), float(w["end"]), str(w["word"]).strip())
                for w in r.json().get("words") or [] if str(w.get("word", "")).strip()]
    return []


async def words_for(clip: Path):
    """[(start, end, word)] for the clip's speech. Empty list if it can't be done."""
    audio = clip.with_suffix(".cap.mp3")
    try:
        rc, _, _ = await _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip),
                              "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio))
        if rc or not audio.exists():
            return []
        return await asyncio.to_thread(_whisper_words, audio)
    finally:
        audio.unlink(missing_ok=True)


def _ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[{}\\]", "", text).replace("\n", " ")


def _chunks(words, max_words=3, max_chars=18):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x[2]) for x in cur) + len(w[2]) > max_chars
                    or w[0] - cur[-1][1] > 0.6):
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?,]$", w[2]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def build_ass(path: Path, W, H, words, cap_y, title="", title_y=120, title_box=True, end_time=None):
    """Writes an .ass file with the title (whole clip) and word-by-word captions."""
    cap_size = int(W * 0.082)
    title_size = int(W * 0.062)
    box = 3 if title_box else 1
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{FONT},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,"
        f"{max(3, cap_size // 11)},2,5,40,40,0,1",
        f"Style: Title,{FONT},{title_size},&H00FFFFFF,&H00FFFFFF,&H8C000000,&H64000000,1,0,0,0,100,100,0,0,{box},"
        f"{12 if title_box else 4},0,5,60,60,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title.strip():
        end = end_time if end_time else 3600
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(end)},Title,,0,0,0,,{{\\pos({W // 2},{title_y})}}"
                     f"{_clean(title.strip())}")
    for chunk in _chunks(words):
        for k, (s, e, _) in enumerate(chunk):
            nxt = chunk[k + 1][0] if k + 1 < len(chunk) else e + 0.15
            parts = []
            for j, (_, _, w) in enumerate(chunk):
                w = _clean(w).upper()
                parts.append(f"{{\\c{HIGHLIGHT}}}{w}{{\\c&HFFFFFF&}}" if j == k else w)
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(max(nxt, s + 0.05))},Cap,,0,0,0,,"
                         f"{{\\pos({W // 2},{cap_y})}}" + " ".join(parts))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
