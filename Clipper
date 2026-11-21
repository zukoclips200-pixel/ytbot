"""Live capture + highlight detection + clip cutting (streamlink + ffmpeg)."""
import asyncio, os, re, shutil, statistics, time, uuid, logging
from collections import deque
from pathlib import Path

log = logging.getLogger("clipper")
PIPE, DEVNULL = asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL

BUFFER_DIR = Path("buffer")
CLIP_DIR = Path("clips")
CLIP_DIR.mkdir(exist_ok=True)

SEG_SECONDS = 3
CLIP_SECONDS = int(os.getenv("CLIP_SECONDS", 15))
AFTER_SECONDS = int(os.getenv("CLIP_AFTER_SECONDS", 4))
LOUD_DB_JUMP = float(os.getenv("LOUD_DB_JUMP", 6))
COOLDOWN = int(os.getenv("CLIP_COOLDOWN", 60))


async def _run(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


def channel_url(slug):
    return f"https://www.youtube.com/@{slug}/live"


async def get_stream_url(slug):
    """Returns the HLS url if the YouTube channel (@handle) is live right now, else None."""
    rc, out, _ = await _run("streamlink", "--stream-url", channel_url(slug), "720p60,720p,480p,best")
    out = out.strip()
    return out if rc == 0 and out.startswith("http") else None


async def segment_loudness(seg: Path):
    rc, _, err = await _run("ffmpeg", "-hide_banner", "-i", str(seg), "-vn",
                            "-af", "volumedetect", "-f", "null", "-")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


class Recorder:
    """Keeps a rolling ~2 minute buffer of one live stream and cuts clips from it."""

    def __init__(self, slug, stream_url, on_clip):
        self.slug, self.url, self.on_clip = slug, stream_url, on_clip
        self.dir = BUFFER_DIR / slug
        self.task = None
        self.proc = None
        self._lock = asyncio.Lock()
        self._last_clip = 0.0

    def start(self):
        self.task = asyncio.create_task(self._run())

    def done(self):
        return self.task is None or self.task.done()

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()

    async def _run(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        cmd = ["ffmpeg", "-loglevel", "error", "-i", self.url, "-c", "copy",
               "-f", "hls", "-hls_time", str(SEG_SECONDS), "-hls_list_size", "40",
               "-hls_flags", "delete_segments+independent_segments",
               "-hls_segment_filename", str(self.dir / "seg_%06d.ts"),
               str(self.dir / "live.m3u8")]
        self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=DEVNULL, stderr=DEVNULL)
        detector = asyncio.create_task(self._detect())
        try:
            await self.proc.wait()
        finally:
            detector.cancel()
            shutil.rmtree(self.dir, ignore_errors=True)
            log.info("recorder for %s ended", self.slug)

    async def _detect(self):
        """Trigger a clip when a segment is much louder than the recent baseline."""
        seen, history = set(), deque(maxlen=60)
        while True:
            await asyncio.sleep(SEG_SECONDS)
            segs = sorted(self.dir.glob("seg_*.ts"))[:-1]  # newest may still be writing
            for seg in segs:
                if seg.name in seen:
                    continue
                seen.add(seg.name)
                vol = await segment_loudness(seg)
                if vol is None:
                    continue
                if len(history) >= 15:
                    base = statistics.median(history)
                    if (vol - base >= LOUD_DB_JUMP and vol > -35
                            and time.time() - self._last_clip > COOLDOWN):
                        self._last_clip = time.time()
                        asyncio.create_task(self._auto_clip(f"loud moment (+{vol - base:.0f} dB)"))
                history.append(vol)
            seen &= {s.name for s in segs}

    async def _auto_clip(self, reason):
        await asyncio.sleep(AFTER_SECONDS)  # let the reaction play out
        path = await self.make_clip(CLIP_SECONDS)
        if path:
            await self.on_clip(self.slug, path, reason)

    async def make_clip(self, seconds=CLIP_SECONDS):
        async with self._lock:
            segs = sorted(self.dir.glob("seg_*.ts"))[:-1]
            segs = segs[-(seconds // SEG_SECONDS + 3):]
            if len(segs) < 2:
                return None
            cid = uuid.uuid4().hex[:10]
            listing = self.dir / f"concat_{cid}.txt"
            joined = self.dir / f"join_{cid}.ts"
            out = CLIP_DIR / f"{self.slug}_{cid}.mp4"
            try:
                listing.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
                rc, _, err = await _run("ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                                        "-safe", "0", "-i", str(listing), "-c", "copy", str(joined))
                if rc:
                    log.warning("concat failed: %s", err[-300:])
                    return None
                rc, _, err = await _run(
                    "ffmpeg", "-y", "-loglevel", "error", "-sseof", f"-{seconds}", "-i", str(joined),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-maxrate", "4000k", "-bufsize", "8000k", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out))
                if rc:
                    log.warning("encode failed: %s", err[-300:])
                    return None
                return out
            finally:
                listing.unlink(missing_ok=True)
                joined.unlink(missing_ok=True)
