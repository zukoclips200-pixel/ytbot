"""Smart clip picking for YouTube videos.

1. Reads what is said (Groq Whisper, or YouTube's own captions as a backup).
2. Skips ads using SponsorBlock plus a check for ad phrases.
3. Works out what kind of video it is (podcast, gaming, entertainment, ...).
4. Asks the AI for complete moments: a hook at the start and a finished thought at the end.
5. Writes a short title and a human-sounding caption for each clip.
"""
import asyncio, json, logging, math, os, re, time, uuid
from pathlib import Path

import requests

import vod
from clipper import CLIP_DIR, _run

log = logging.getLogger("smart")

GROQ_URL = "https://api.groq.com/openai/v1"
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
WHISPER = os.getenv("GROQ_WHISPER", "whisper-large-v3-turbo")
WHISPER_MAX_MIN = int(os.getenv("WHISPER_MAX_MINUTES", 110))   # Groq free plan: ~2 hours of audio per hour
CHUNK_SEC = 600                                                # audio is sent in 10-minute pieces
WINDOW_CHARS = 9000                                            # transcript sent to the AI per request

SB_CATS = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic"]
AD_WORDS = re.compile(
    r"sponsored by|this video is sponsored|today'?s sponsor|thanks to .{0,40} for sponsoring|promo code|"
    r"use (my )?code|link in (the )?description|first \d+ (people|users)|% off|free trial|"
    r"brought to you by|download .{0,30} (app|today)|sign up (today|now)|patreon", re.I)

TYPES = {
    "podcast": "Podcast or interview. Pick ONE complete idea each: a story, a strong opinion, a surprising answer, "
               "advice, or a heated exchange. Start on the hook (a bold line or the question being answered), not on "
               "small talk. Include the context the viewer needs and end when the point lands. 30-60 seconds.",
    "entertainment": "Entertainment, comedy, vlog, challenge or reaction video. Pick funny, shocking, emotional or "
                     "crazy moments. Always include the setup AND the payoff/punchline/reaction. 15-45 seconds.",
    "gaming": "Gaming video or stream. Pick big plays, clutch moments, fails, rage or funny reactions. Include the "
              "build-up and the reaction after. 15-40 seconds.",
    "educational": "Educational, explainer, finance or how-to video. Pick a surprising fact, a myth being busted, a "
                   "clear tip, or a strong take, with the full explanation. 30-60 seconds.",
    "commentary": "Commentary, news, drama or exposé video. Pick the biggest reveals, strongest opinions and most "
                  "shocking details, each with enough context to make sense alone. 25-55 seconds.",
    "other": "Pick the most interesting, surprising or emotional self-contained moments. 20-50 seconds.",
}
LENGTHS = {"podcast": (25, 75), "entertainment": (12, 55), "gaming": (10, 50),
           "educational": (25, 75), "commentary": (20, 65), "other": (15, 60)}


class SmartError(RuntimeError):
    pass


def _key():
    k = os.getenv("GROQ_API_KEY", "").strip()
    if not k:
        raise SmartError("GROQ_API_KEY is missing. Add it in Railway → ytbot → Variables.")
    return k


# ───────────── ads ─────────────
def sponsor_segments(video_id):
    """Ad/intro/outro times from SponsorBlock (a free community database)."""
    if not video_id:
        return []
    try:
        r = requests.get("https://sponsor.ajay.app/api/skipSegments",
                         params={"videoID": video_id, "categories": json.dumps(SB_CATS)}, timeout=15)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [(float(s["segment"][0]), float(s["segment"][1]), s["category"]) for s in r.json()]
    except Exception as e:
        log.warning("SponsorBlock failed: %s", e)
        return []


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


# ───────────── transcript ─────────────
def _whisper_file(path: Path):
    for attempt in range(4):
        with open(path, "rb") as f:
            r = requests.post(f"{GROQ_URL}/audio/transcriptions",
                              headers={"Authorization": f"Bearer {_key()}"},
                              files={"file": (path.name, f, "audio/mpeg")},
                              data={"model": WHISPER, "response_format": "verbose_json",
                                    "timestamp_granularities[]": "segment", "temperature": "0"},
                              timeout=300)
        if r.status_code == 429:
            wait = float(r.headers.get("retry-after") or 30)
            if wait > 90:
                raise SmartError(f"Groq's free hourly audio limit is used up. Try again in about "
                                 f"{math.ceil(wait / 60)} minutes.")
            time.sleep(wait + 1)
            continue
        if r.status_code >= 400:
            raise SmartError(f"Transcription failed ({r.status_code}): {r.text[:150]}")
        return r.json()
    raise SmartError("Transcription kept hitting Groq's limit. Try again later.")


async def whisper_transcript(url):
    cid = uuid.uuid4().hex[:8]
    rc, _, err = await _run("yt-dlp", *vod.cookie_args(), "--no-playlist", "--no-warnings",
                            "-f", "bestaudio/best", "-o", str(CLIP_DIR / f"aud_{cid}.%(ext)s"), "--", url)
    try:
        src = [f for f in CLIP_DIR.glob(f"aud_{cid}.*")]
        if rc or not src:
            raise SmartError("Audio download failed: " + err.strip()[-200:])
        rc, _, err = await _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src[0]),
                                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "segment",
                                "-segment_time", str(CHUNK_SEC), "-reset_timestamps", "1",
                                str(CLIP_DIR / f"aud_{cid}_part%03d.mp3"))
        parts = sorted(CLIP_DIR.glob(f"aud_{cid}_part*.mp3"))
        if rc or not parts:
            raise SmartError("Couldn't prepare the audio: " + err[-200:])
        segs = []
        for n, p in enumerate(parts):
            data = await asyncio.to_thread(_whisper_file, p)
            off = n * CHUNK_SEC
            for s in data.get("segments") or []:
                t = (s.get("text") or "").strip()
                if t:
                    segs.append((off + float(s["start"]), off + float(s["end"]), t))
        return segs
    finally:
        for f in CLIP_DIR.glob(f"aud_{cid}*"):
            f.unlink(missing_ok=True)


async def youtube_captions(url):
    """Backup: YouTube's own (often automatic) English captions."""
    cid = uuid.uuid4().hex[:8]
    base = CLIP_DIR / f"subs_{cid}"
    await _run("yt-dlp", *vod.cookie_args(), "--no-playlist", "--no-warnings", "--skip-download",
               "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,-live_chat", "--sub-format", "json3",
               "-o", f"{base}.%(ext)s", "--", url)
    files = sorted(CLIP_DIR.glob(f"subs_{cid}*.json3"), key=lambda p: ("orig" not in p.name, len(p.name)))
    try:
        if not files:
            return []
        data = json.loads(files[0].read_text(errors="ignore"))
        words = []
        for ev in data.get("events", []):
            t0 = ev.get("tStartMs", 0) / 1000
            for s in ev.get("segs") or []:
                w = (s.get("utf8") or "").strip()
                if w and w != "\n":
                    words.append((t0 + s.get("tOffsetMs", 0) / 1000, w))
        segs, cur, start, last = [], [], None, 0.0
        for t, w in words:
            if cur and (t - last > 0.8 or t - start > 10 or re.search(r"[.!?]$", cur[-1])):
                segs.append((start, last + 0.4, " ".join(cur)))
                cur, start = [], None
            if start is None:
                start = t
            cur.append(w)
            last = t
        if cur:
            segs.append((start, last + 0.6, " ".join(cur)))
        return segs
    finally:
        for f in CLIP_DIR.glob(f"subs_{cid}*"):
            f.unlink(missing_ok=True)


async def get_transcript(url, duration, status):
    long_video = duration > WHISPER_MAX_MIN * 60
    if not long_video:
        try:
            await status("🎧 Listening to the video…")
            segs = await whisper_transcript(url)
            if segs:
                return segs, "Whisper"
        except Exception as e:
            log.warning("whisper: %s", e)
            first_err = str(e)
        else:
            first_err = "no speech found"
    else:
        first_err = "video longer than the free audio limit"
    await status("📝 Reading YouTube's captions…")
    segs = await youtube_captions(url)
    if segs:
        return segs, "YouTube captions"
    if long_video:
        raise SmartError(f"This video is over {WHISPER_MAX_MIN} minutes and has no captions I can read.")
    raise SmartError(f"Couldn't get what's said in this video ({first_err}).")


# ───────────── AI ─────────────
def _chat(system, user, max_tokens=2500):
    body = {"model": MODEL, "temperature": 0.4, "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "gpt-oss" in MODEL:
        body["reasoning_effort"] = "low"
    for attempt in range(8):
        r = requests.post(f"{GROQ_URL}/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {_key()}"}, timeout=180)
        if r.status_code == 429:
            wait = float(r.headers.get("retry-after") or 15)
            if wait > 300:
                raise SmartError("Groq's free daily AI limit is used up. Try again tomorrow.")
            time.sleep(min(wait, 70) + 1)
            continue
        if r.status_code >= 400:
            raise SmartError(f"AI error ({r.status_code}): {r.text[:150]}")
        text = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except Exception:
            if attempt >= 2:
                raise SmartError("The AI gave an answer I couldn't read.")
    raise SmartError("Groq kept saying 'too many requests'. Try again in a few minutes.")


def classify(meta, segs):
    intro = " ".join(t for _, _, t in segs[:60])[:2000]
    chapters = ", ".join(c.get("title", "") for c in (meta.get("chapters") or [])[:20])
    user = (f"Title: {meta.get('title')}\nChannel: {meta.get('channel') or meta.get('uploader')}\n"
            f"YouTube category: {', '.join(meta.get('categories') or [])}\n"
            f"Tags: {', '.join((meta.get('tags') or [])[:15])}\nChapters: {chapters}\n"
            f"Description: {(meta.get('description') or '')[:700]}\n\nStart of transcript: {intro}")
    out = _chat("You sort YouTube videos into one type so they can be clipped well. Types: "
                + ", ".join(TYPES) + '. Answer as JSON: {"type": "...", "why": "one short sentence"}', user, 800)
    t = str(out.get("type", "other")).lower().strip()
    return t if t in TYPES else "other"


def _windows(lines):
    """Splits transcript lines into pieces small enough for the free AI plan, with a little overlap."""
    out, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > WINDOW_CHARS and cur:
            out.append(cur)
            cur = cur[-6:]
            size = sum(len(x) for x in cur)
        cur.append(ln)
        size += len(ln)
    if cur:
        out.append(cur)
    return out


def hot_ranges(meta, dur):
    """Parts of the video people re-watch most (YouTube's 'Most replayed'), widened a bit.
    Only these parts are sent to the AI, which makes long videos much faster."""
    heat = meta.get("heatmap") or []
    if dur < 20 * 60 or len(heat) < 20:
        return None
    vals = sorted(h["value"] for h in heat)
    cut = vals[int(len(vals) * 0.55)]
    if cut >= vals[-1]:
        return None
    ranges = []
    for h in heat:
        if h["value"] > cut:
            a, b = h["start_time"] - 60, h["end_time"] + 60
            if ranges and a <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], b)
            else:
                ranges.append([a, b])
    return ranges


def pick(meta, vtype, segs, ads, want, allowed=None):
    lo, hi = LENGTHS[vtype]
    lines, prev = [], -1
    for i, (s, e, t) in enumerate(segs):
        if allowed is not None and i not in allowed:
            continue
        if prev >= 0 and i != prev + 1:
            lines.append("--- (part of the video skipped, never join clips across this line) ---")
        prev = i
        if any(_overlap(s, e, a0, a1) > 0.5 * (e - s) for a0, a1, _ in ads):
            lines.append(f"[{i}] (AD - never use)")
        else:
            lines.append(f"[{i}|{vod.fmt_ts(s)}] {t}")
    wins = _windows(lines)
    per = max(2, math.ceil(want * 1.6 / len(wins)))
    system = (
        "You are a top short-form video editor who finds the moments that go viral on TikTok, Reels and Shorts.\n"
        f"Video type rules: {TYPES[vtype]}\n"
        "Hard rules:\n"
        "- Each clip must make sense on its own to someone who never saw the video.\n"
        "- start_id must be the line where the hook begins, never mid-sentence, never small talk or greetings.\n"
        "- end_id must be the line where the thought, story or joke is FINISHED. Never stop mid-point.\n"
        f"- Clip length must be between {lo} and {hi} seconds (use the timestamps).\n"
        "- NEVER pick ads, sponsor reads, 'use my code', merch, 'like and subscribe', intros or outros.\n"
        "- Clips must not overlap. Only pick genuinely strong moments; quality beats quantity.\n"
        "- score: 10 = very likely to go viral, 5 = okay, below 5 = don't include it.\n"
        'Answer as JSON: {"clips": [{"start_id": 0, "end_id": 0, "score": 0, "why": "few words"}]}')
    found = []
    for w in wins:
        user = (f"Video title: {meta.get('title')}\nFind up to {per} clips in this part of the transcript. "
                f"Each line is [id|time] text.\n\n" + "\n".join(w))
        try:
            out = _chat(system, user)
        except SmartError:
            if found:
                break          # keep what we already have
            raise
        for c in out.get("clips") or []:
            try:
                a, b, sc = int(c["start_id"]), int(c["end_id"]), float(c.get("score", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= a <= b < len(segs) and sc >= 5:
                found.append((a, b, sc, str(c.get("why", ""))[:80]))
    return found


def clean(segs, cands, ads, vtype, allowed=None):
    lo, hi = LENGTHS[vtype]
    keep = []
    ok = (lambda i: True) if allowed is None else (lambda i: i in allowed)
    for a, b, sc, why in sorted(cands, key=lambda x: -x[2]):
        if not all(ok(i) for i in range(a, b + 1)):
            continue
        while b > a and segs[b][1] - segs[a][0] > hi + 5:   # too long: end on an earlier sentence
            b -= 1
        while segs[b][1] - segs[a][0] < lo and b + 1 < len(segs) and ok(b + 1) \
                and segs[b + 1][1] - segs[a][0] <= hi:      # too short: add the next sentence(s)
            b += 1
        s, e = max(0.0, segs[a][0] - 0.25), segs[b][1] + 0.4
        if e - s < lo - 3:
            continue
        text = " ".join(t for _, _, t in segs[a:b + 1])
        if AD_WORDS.search(text) or any(_overlap(s, e, a0, a1) > 2 for a0, a1, _ in ads):
            continue
        if any(_overlap(s, e, k["start"], k["end"]) > 3 for k in keep):
            continue
        keep.append({"start": s, "end": e, "score": sc, "why": why, "text": text})
    return keep


def write_captions(meta, vtype, clips):
    items = "\n\n".join(f"Clip {n}: {c['text'][:1100]}" for n, c in enumerate(clips, 1))
    system = (
        "You write titles and post captions for short clips, like a real person who runs a clip page.\n"
        "Title: max 60 characters, punchy, makes people want to watch, no clickbait lies.\n"
        "Caption: 1-2 short sentences about what happens in the clip, casual and natural. "
        "Only say things that are actually in the clip. At most one emoji. No hashtags. "
        "Never use words like 'dive', 'unleash', 'delve', 'game-changer', 'journey', 'insane' or 'mind-blowing'.\n"
        'Answer as JSON: {"clips": [{"n": 1, "title": "...", "caption": "..."}]}')
    user = f"Video: {meta.get('title')} by {meta.get('channel') or meta.get('uploader')}\nType: {vtype}\n\n{items}"
    try:
        out = {int(c["n"]): c for c in _chat(system, user, 3000).get("clips") or [] if "n" in c}
    except Exception as e:
        log.warning("captions failed: %s", e)
        out = {}
    for n, c in enumerate(clips, 1):
        c["title"] = str(out.get(n, {}).get("title") or c["why"] or "Clip")[:80]
        c["caption"] = str(out.get(n, {}).get("caption") or "")[:600]


async def find_clips(url, count, status):
    """Returns (video_type, transcript_source, [clip dicts]) ready to cut."""
    _key()
    await status("🔎 Checking the video…")
    meta = await vod.info(url)
    if meta.get("is_live"):
        raise SmartError("That's a live stream. Send a normal video link.")
    dur = float(meta.get("duration") or 0)
    if not dur or dur > vod.MAX_VIDEO_SECONDS:
        raise SmartError("Video is too long (max 4 hours).")
    if dur < 60:
        raise SmartError("That video is already short. Send a longer one.")

    ads = await asyncio.to_thread(sponsor_segments, meta.get("id") or vod.video_id(url))
    segs, source = await get_transcript(url, dur, status)

    await status("🧠 Working out what kind of video this is…")
    vtype = await asyncio.to_thread(classify, meta, segs)
    await status(f"✂️ Finding the best **{vtype}** moments (skipping {len(ads)} ad/intro parts)…")
    hot = hot_ranges(meta, dur)
    allowed = None
    if hot:
        allowed = {i for i, (s, e, _) in enumerate(segs) if any(a <= s <= b for a, b in hot)}
        if len(allowed) < len(segs) * 0.25:
            allowed = None
    cands = await asyncio.to_thread(pick, meta, vtype, segs, ads, count, allowed)
    clips = clean(segs, cands, ads, vtype, allowed)[:count]
    if not clips:
        raise SmartError("I couldn't find any moments strong enough to clip in this video.")
    clips.sort(key=lambda c: c["start"])
    await status(f"✍️ Writing titles and captions for {len(clips)} clips…")
    await asyncio.to_thread(write_captions, meta, vtype, clips)
    return vtype, source, clips
