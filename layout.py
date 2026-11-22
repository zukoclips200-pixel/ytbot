"""Looks at a clip and decides how to frame it for 9:16.

Layouts:
  split  - two people on screen (podcast): one on top, one below
  track  - one main person: a 9:16 crop that follows their face (also handles camera cuts)
  gaming - small facecam in a corner: facecam on top, gameplay below
  fit    - no clear face: whole video in the middle with a blurred background
"""
import logging, os, statistics
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("layout")
MODEL = Path(os.getenv("FACE_MODEL", "models/yunet.onnx"))
OUT_W, OUT_H = 720, 1280
SAMPLE_FPS = 2
MAX_SAMPLES = 180


class _Faces:
    """YuNet face detector if the model file is there, otherwise OpenCV's built-in (weaker) one."""

    def __init__(self):
        self.yunet = None
        if MODEL.exists():
            try:
                self.yunet = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.75, 0.3, 50)
            except Exception as e:
                log.warning("YuNet failed to load: %s", e)
        if self.yunet is None:
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def __call__(self, img):
        h, w = img.shape[:2]
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(img)
            if faces is None:
                return []
            return [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, 1.1, 6, minSize=(max(24, w // 30), max(24, w // 30)))
        return [tuple(map(float, f)) for f in found]


def _caption_text(small):
    """True if the lower part of the frame has caption-style text: bright (white or coloured) letters
    with a dark outline. Faces, shirts and backgrounds don't have that pattern."""
    h = small.shape[0]
    band = small[int(h * 0.55):int(h * 0.97)]
    v = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)[..., 2]
    bright = (v > 185).astype(np.uint8)
    dark = (v < 60).astype(np.uint8)
    outline = cv2.dilate(dark, np.ones((3, 3), np.uint8), iterations=2) & bright
    rows = outline.mean(axis=1)
    return (rows > 0.03).sum() >= max(8, int(band.shape[0] * 0.06))


def sample(path):
    cap = cv2.VideoCapture(str(path))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(fps / SAMPLE_FPS)))
    det = _Faces()
    scale = 640 / W if W > 640 else 1.0
    out, present, i = [], 0, 0
    while len(out) < MAX_SAMPLES:
        if not cap.grab():
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
            faces = [(x / scale, y / scale, w / scale, h / scale) for x, y, w, h in det(small)]
            faces = [f for f in faces if f[2] >= W * 0.035]
            out.append((i / fps, faces))
            if _caption_text(cv2.resize(small, (640, max(2, int(640 * H / W))))):
                present += 1
        i += 1
    cap.release()
    n = len(out) or 1
    burned = present / n >= 0.3     # captions are on screen for a good part of the clip
    return W, H, out, burned


def _clusters(samples, W):
    pts = sorted((x + w / 2, y + h / 2, w, h, t) for t, faces in samples for x, y, w, h in faces)
    groups = []
    for p in pts:
        if groups and p[0] - groups[-1][-1][0] < W * 0.12:
            groups[-1].append(p)
        else:
            groups.append([p])
    n = len(samples) or 1
    res = []
    for g in groups:
        times = {p[4] for p in g}
        res.append({"cx": statistics.median(p[0] for p in g), "cy": statistics.median(p[1] for p in g),
                    "w": statistics.median(p[2] for p in g), "h": statistics.median(p[3] for p in g),
                    "share": len(times) / n, "times": times})
    return sorted(res, key=lambda c: -c["share"])


def _track_moves(samples, W, H, flip):
    """Crop positions over time for one person: steady, glides for small moves, jumps on camera cuts."""
    cw = int(H * 9 / 16) // 2 * 2
    cw = min(cw, W // 2 * 2)
    biggest = []
    last = W / 2
    for t, faces in samples:
        if faces:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            last = x + w / 2
        biggest.append((t, last))
    xs = [c for _, c in biggest]
    smooth = [statistics.median(xs[max(0, k - 2):k + 3]) for k in range(len(xs))]
    if flip:
        smooth = [W - c for c in smooth]

    def clamp(c):
        return int(min(max(c - cw / 2, 0), W - cw))

    x0 = clamp(smooth[0]) if smooth else (W - cw) // 2
    moves, cur = [], x0
    for (t, _), c in zip(biggest, smooth):
        nx = clamp(c)
        if abs(nx - cur) > W * 0.06:
            jump = abs(nx - cur) > W * 0.22
            moves.append((t, nx - cur, 0.01 if jump else 0.6))
            cur = nx
    return cw, x0, moves


def analyze(path, vtype="", flip=False):
    """Returns a dict describing the layout. Never raises: falls back to 'fit'."""
    try:
        W, H, samples, burned = sample(path)
    except Exception as e:
        log.warning("analysis failed: %s", e)
        return {"layout": "fit", "burned_captions": False}
    info = {"W": W, "H": H, "burned_captions": burned, "flip": flip}
    if not samples or W < H:
        info["layout"] = "fit" if W >= H else "vertical"
        return info
    if burned:
        # The video already has captions at the bottom: show the whole frame so they aren't cut off
        # and the bot doesn't add a second set.
        info["layout"] = "fit"
        return info
    cl = [c for c in _clusters(samples, W) if c["share"] >= 0.25]

    def mirror(c):
        return dict(c, cx=W - c["cx"]) if flip else c

    cam = [c for c in cl if c["w"] < W * 0.11 and (c["cx"] < W * 0.33 or c["cx"] > W * 0.67)
           and (c["cy"] < H * 0.4 or c["cy"] > H * 0.6)]
    if cam and (vtype == "gaming" or len(cl) == 1):
        info.update(layout="gaming", cam=mirror(cam[0]))
        return info

    big = [c for c in cl if c["w"] >= W * 0.05]
    if len(big) >= 2:
        a, b = big[0], big[1]
        both = len(a["times"] & b["times"]) / max(1, min(len(a["times"]), len(b["times"])))
        gap = abs(a["cx"] - b["cx"])
        if both >= 0.5 and gap > W * 0.15:
            left, right = sorted((mirror(a), mirror(b)), key=lambda c: c["cx"])
            info.update(layout="split", top=left, bottom=right, gap=gap)
            return info
    if big:
        cw, x0, moves = _track_moves(samples, W, H, flip)
        info.update(layout="track", cw=cw, x0=x0, moves=moves)
        return info
    info["layout"] = "fit"
    return info


# ───────────── turning a layout into an ffmpeg filter ─────────────
def _even(v):
    return int(v) // 2 * 2


def _person_crop(c, W, H, aspect, face_at=0.42, max_w=None):
    """A crop around one face; face_at = how far down the crop the face sits (0.5 = middle).
    max_w keeps the crop narrow when two people sit close, so each half shows one person."""
    ch = min(H, max(c["h"] * 2.8, H * 0.5))
    if max_w:
        ch = min(ch, max(max_w / aspect, c["h"] * 1.8))
    cw = ch * aspect
    if cw > W:
        cw, ch = W, W / aspect
    x = min(max(c["cx"] - cw / 2, 0), W - cw)
    y = min(max(c["cy"] - ch * face_at, 0), H - ch)
    return _even(cw), _even(ch), _even(x), _even(y)


def filter_for(info, src, remap=lambda t: t):
    """ffmpeg filter text that turns [src] into a 720x1280 [vs]. Also returns where captions should go."""
    lay = info.get("layout", "fit")
    W, H = info.get("W", 1280), info.get("H", 720)
    if lay == "split":
        mw = info.get("gap", W) * 1.15
        a = _person_crop(info["top"], W, H, OUT_W / (OUT_H / 2), 0.55, mw)   # lower, so the title fits above
        b = _person_crop(info["bottom"], W, H, OUT_W / (OUT_H / 2), 0.42, mw)
        f = (f"[{src}]split[p1][p2];"
             f"[p1]crop={a[0]}:{a[1]}:{a[2]}:{a[3]},scale={OUT_W}:{OUT_H // 2},setsar=1[t1];"
             f"[p2]crop={b[0]}:{b[1]}:{b[2]}:{b[3]},scale={OUT_W}:{OUT_H // 2},setsar=1[t2];"
             f"[t1][t2]vstack,setsar=1[vs]")
        return f, {"cap_y": OUT_H // 2, "title_y": 95, "title_box": True}
    if lay == "gaming":
        top_h = 460
        c = info["cam"]
        cw = min(max(c["w"] * 3.4, W * 0.2), W * 0.45)
        ch = cw / (OUT_W / top_h)
        cx = min(max(c["cx"] - cw / 2, 0), W - cw)
        cy = min(max(c["cy"] - ch / 2, 0), H - ch)
        gh = H
        gw = min(W, gh * OUT_W / (OUT_H - top_h))
        gx = (W - gw) / 2
        f = (f"[{src}]split[p1][p2];"
             f"[p1]crop={_even(cw)}:{_even(ch)}:{_even(cx)}:{_even(cy)},scale={OUT_W}:{top_h},setsar=1[t1];"
             f"[p2]crop={_even(gw)}:{_even(gh)}:{_even(gx)}:0,scale={OUT_W}:{OUT_H - top_h},setsar=1[t2];"
             f"[t1][t2]vstack,setsar=1[vs]")
        return f, {"cap_y": top_h, "title_y": 90, "title_box": True}
    if lay == "track":
        cw = info["cw"]
        parts = [str(info["x0"])]
        for t, d, dur in info["moves"]:
            t2 = remap(t)
            if t2 is None:
                continue
            parts.append(f"({d})*clip((t-{t2:.2f})/{dur},0,1)")
        x = "+".join(parts)
        f = (f"[{src}]crop=w={cw}:h={_even(H)}:x='min(max({x},0),{W - cw})':y=0,"
             f"scale={OUT_W}:{OUT_H},setsar=1[vs]")
        return f, {"cap_y": int(OUT_H * 0.70), "title_y": 150, "title_box": True}
    if lay == "vertical":
        return (f"[{src}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[vs]"), \
            {"cap_y": int(OUT_H * 0.70), "title_y": 150, "title_box": True}
    vid_h = int(OUT_W * H / W) // 2 * 2
    f = (f"[{src}]split[a][b];"
         f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=30:5[bg];"
         f"[b]scale={OUT_W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vs]")
    band = (OUT_H - vid_h) // 2
    return f, {"cap_y": min(OUT_H - band + 90, int(OUT_H * 0.78)), "title_y": band // 2, "title_box": False}
