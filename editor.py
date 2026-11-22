"""Auto-edit: smart layout (split screen / face tracking / gaming), word-by-word captions,
title, blur background, flip, trim dead air, colour, overlay."""
import asyncio, logging, re
from dataclasses import dataclass
from pathlib import Path

import captions, layout

log = logging.getLogger("editor")

PIPE = asyncio.subprocess.PIPE
OUT_DIR = Path("edits")
OUT_DIR.mkdir(exist_ok=True)
ENCODE_LOCK = asyncio.Semaphore(1)   # one video encode at a time, so the small server doesn't run out of memory

# key: (label, width, height)
# 720p: looks sharper than 1080p under Discord's 10 MB limit and uses far less memory.
SIZES = {
    "smart": ("✨ Smart 9:16 (split screen / face tracking)", 720, 1280),
    "9x16_full": ("9:16 centre crop", 720, 1280),
    "9x16_fit": ("9:16 fit (shows whole clip)", 720, 1280),
    "1x1": ("1:1 square", 720, 720),
    "16x9": ("Original 16:9", 1280, 720),
}
# key: (label, brightness, contrast, saturation)
COLORS = {
    "default": ("Default", 0.0, 1.0, 1.0),
    "bright": ("Brighter", 0.08, 1.0, 1.05),
    "punchy": ("Punchy", 0.0, 1.15, 1.15),
    "vivid": ("Vivid", 0.04, 1.1, 1.35),
    "muted": ("Muted", -0.03, 1.0, 0.8),
}


CAPTION_MODES = ["auto", "on", "off"]


@dataclass
class EditOpts:
    size: str = "smart"
    color: str = "default"
    blur: bool = True      # blurred background (used by "9:16 fit")
    flip: bool = False
    shorten: bool = False  # cut silent/dead sections
    overlay: bool = False  # assets/overlay.png on top
    title: str = ""
    captions: str = "auto"  # auto = skip if the video already has captions burned in
    vtype: str = ""         # podcast / gaming / ... from the AI, helps pick the layout


async def _run(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def duration(path):
    _, out, _ = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(path))
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


async def keep_segments(path, total, noise_db=-35, min_silence=0.8, pad=0.15):
    """Time ranges to keep after removing long silences. None = nothing worth cutting."""
    _, _, err = await _run("ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af",
                           f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    sil = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        s, e = max(s, 0) + pad, e - pad
        if e - s > 0.1:
            sil.append((s, e))
    if not sil:
        return None
    keep, t = [], 0.0
    for s, e in sil:
        if s - t > 0.3:
            keep.append((t, s))
        t = e
    if total - t > 0.3:
        keep.append((t, total))
    if not keep or sum(b - a for a, b in keep) < 3:
        return None
    return keep


def _between(keep):
    return "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in keep)


def _remapper(keep):
    """Maps a time in the original clip to the time after dead air is cut out."""
    if not keep:
        return lambda t: t

    def f(t):
        acc = 0.0
        for a, b in keep:
            if t < a:
                return acc          # inside a removed part: jump to where the next kept part starts
            if t <= b:
                return acc + t - a
            acc += b - a
        return acc
    return f


async def autoedit(src: Path, dst: Path, o: EditOpts, overlay_path: Path | None = None,
                   font: str | None = None, limit_mb: float = 9.0):
    """Returns a short description of what was done."""
    total = await duration(src)
    keep = await keep_segments(src, total) if o.shorten else None
    out_len = sum(b - a for a, b in keep) if keep else total
    remap = _remapper(keep)
    _, W, H = SIZES[o.size]
    notes = []

    fc, cur = [], "0:v"
    if keep:
        fc.append(f"[0:v]select='{_between(keep)}',setpts=N/FRAME_RATE/TB[v0]")
        cur = "v0"
    if o.flip:
        fc.append(f"[{cur}]hflip[vf]")
        cur = "vf"

    info = {"burned_captions": False}
    if o.size == "smart":
        async with ENCODE_LOCK:
            info = await asyncio.to_thread(layout.analyze, src, o.vtype, o.flip)
        f, place = layout.filter_for(info, cur, remap)
        fc.append(f)
        notes.append(f"layout: {info.get('layout')}")
    else:
        if o.size == "9x16_full":
            fc.append(f"[{cur}]crop=w='ih*9/16':h=ih,scale={W}:{H},setsar=1[vs]")
        elif o.size == "9x16_fit":
            if o.blur:
                fc.append(f"[{cur}]split[a][b];"
                          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5[bg];"
                          f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vs]")
            else:
                fc.append(f"[{cur}]scale={W}:-2,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[vs]")
        elif o.size == "1x1":
            fc.append(f"[{cur}]crop=w='min(iw,ih)':h='min(iw,ih)',scale={W}:{H},setsar=1[vs]")
        else:
            fc.append(f"[{cur}]scale={W}:{H},setsar=1[vs]")
        if o.size == "9x16_fit":
            band = (H - W * 9 // 16) // 2
            place = {"cap_y": H - band + 90, "title_y": band // 2, "title_box": False}
        else:
            place = {"cap_y": int(H * (0.70 if H > W else 0.82)), "title_y": int(H * 0.1), "title_box": True}
    cur = "vs"

    _, br, ct, sa = COLORS[o.color]
    if (br, ct, sa) != (0.0, 1.0, 1.0):
        fc.append(f"[{cur}]eq=brightness={br}:contrast={ct}:saturation={sa}[ve]")
        cur = "ve"

    inputs = ["-i", str(src)]
    if o.overlay and overlay_path and overlay_path.exists():
        inputs += ["-i", str(overlay_path)]
        fc.append(f"[1:v]scale={W}:{H}[ov];[{cur}][ov]overlay=0:0[vo]")
        cur = "vo"

    want_caps = o.captions == "on" or (o.captions == "auto" and not info.get("burned_captions"))
    if o.captions == "auto" and info.get("burned_captions"):
        notes.append("captions skipped (video already has them)")
    words = []
    if want_caps:
        words = await captions.words_for(src)
        if keep:
            words = [(remap(s), remap(e), w) for s, e, w in words
                     if any(a <= (s + e) / 2 <= b for a, b in keep)]
        notes.append("captions: on" if words else "captions: none (no speech found)")

    ass_file = None
    if words or o.title.strip():
        ass_file = dst.with_suffix(".ass")
        captions.build_ass(ass_file, W, H, words, place["cap_y"], o.title, place["title_y"],
                           place["title_box"], out_len)
        fc.append(f"[{cur}]ass=filename='{ass_file}'[vt]")
        cur = "vt"

    fc.append(f"[{cur}]null[vout]")

    if keep:
        fc.append(f"[0:a]aselect='{_between(keep)}',asetpts=N/SR/TB[aout]")
        amap = "[aout]"
    else:
        amap = "0:a?"

    vb = int(min(6000, max(500, limit_mb * 8000 * 0.92 / max(out_len, 1) - 128)))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", amap,
           "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-b:v", f"{vb}k",
           "-maxrate", f"{int(vb * 1.3)}k", "-bufsize", f"{vb * 2}k", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    try:
        async with ENCODE_LOCK:
            rc, _, err = await _run(*cmd)
    finally:
        if ass_file:
            ass_file.unlink(missing_ok=True)
    if rc:
        if rc < 0 or not err.strip():
            raise RuntimeError("the server ran out of memory. Wait for other clips to finish, then try again.")
        raise RuntimeError(err[-500:])
    return ", ".join(notes)
