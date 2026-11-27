"""Gambling clips show only the streamer's face camera, never the casino screen.

Either the streamer's facecam box is set in config/watchlist.yaml as fractions
of the frame ({x, y, w, h}), or we find it by detecting the streamer's face on a
dozen frames and taking the region it stays in. The segment is then cropped to
that box before OpenShorts reframes it to 9:16.
"""
from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FacecamNotFound(RuntimeError):
    pass


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    def even(self) -> "Box":
        f = lambda v: max(2, int(v) // 2 * 2)  # noqa: E731  (ffmpeg wants even sizes)
        return Box(f(self.x), f(self.y), f(self.w), f(self.h))


def probe_size(video: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(video)],
        check=True, capture_output=True, text=True, timeout=60).stdout
    doc = json.loads(out)
    s = doc["streams"][0]
    return int(s["width"]), int(s["height"]), float(doc.get("format", {}).get("duration") or 0)


def box_from_fractions(frac: dict, width: int, height: int) -> Box:
    return Box(round(frac["x"] * width), round(frac["y"] * height),
               round(frac["w"] * width), round(frac["h"] * height)).even()


def sample_frames(video: Path, n: int = 12):
    import cv2  # imported here so the rest of the pipeline runs without OpenCV
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frames = []
    for i in range(n):
        if total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / n))
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()
    return frames


def detect_faces(frame) -> list[tuple[int, int, int, int]]:
    import cv2
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(max(24, h // 30),) * 2)
    return [tuple(int(v) for v in f) for f in faces]


def find_facecam(frames, width: int, height: int, padding: float = 0.35,
                 detector=detect_faces) -> Box:
    """Region around the face seen most consistently across frames."""
    hits = []
    for frame in frames:
        faces = detector(frame)
        if faces:
            hits.append(max(faces, key=lambda f: f[2] * f[3]))  # biggest face in the frame
    if len(hits) < max(2, len(frames) // 3):
        raise FacecamNotFound(f"face found in only {len(hits)} of {len(frames)} frames")
    cx = statistics.median(x + w / 2 for x, y, w, h in hits)
    cy = statistics.median(y + h / 2 for x, y, w, h in hits)
    size = statistics.median(max(w, h) for x, y, w, h in hits)
    # A facecam is usually a little wider than the face; keep a 4:3 box.
    half_w = size * (0.5 + padding) * 1.33
    half_h = size * (0.5 + padding)
    x0, x1 = max(0, cx - half_w), min(width, cx + half_w)
    y0, y1 = max(0, cy - half_h), min(height, cy + half_h)
    return Box(round(x0), round(y0), round(x1 - x0), round(y1 - y0)).even()


def crop(video: Path, box: Box, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", f"crop={box.w}:{box.h}:{box.x}:{box.y}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy", str(out)],
        check=True, capture_output=True, timeout=900)
    return out


def facecam_only(video: Path, out: Path, configured: dict | None = None,
                 sample_n: int = 12, padding: float = 0.35) -> tuple[Path, Box]:
    width, height, _ = probe_size(video)
    if configured:
        box = box_from_fractions(configured, width, height)
    else:
        box = find_facecam(sample_frames(video, sample_n), width, height, padding)
    return crop(video, box, out), box
