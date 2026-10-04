"""Personal video downloader backend (FastAPI), built for Render's free tier."""

import os
import re
import time
import uuid
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import yt_dlp

WORKDIR = "/tmp/vd-downloads"
os.makedirs(WORKDIR, exist_ok=True)

app = FastAPI(title="Video Downloader")

jobs: dict = {}
_dl_lock = threading.Lock()  # one download at a time on the small free server
_MAX_AGE = 2 * 3600  # forget files older than 2h
SAFE_NAME = re.compile(r"^[a-f0-9]{8}\.[A-Za-z0-9]+$")


class FetchReq(BaseModel):
    url: str
    quality: str = "best"  # best | 720p | audio


def _cleanup():
    now = time.time()
    for name in os.listdir(WORKDIR):
        path = os.path.join(WORKDIR, name)
        try:
            if now - os.path.getmtime(path) > _MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked for bot verification on this one. "
                "Wait a bit and try again, or try a different video.")
    if "drm" in low or "encrypted" in low:
        return ("This video is DRM-protected (like Netflix/Spotify). "
                "No downloader can grab those.")
    if "login required" in low or "log in" in low:
        return "This one needs a login on the site, so it can't be fetched."
    if "unsupported url" in low:
        return "That link isn't a video/file this downloader understands."
    return "Couldn't fetch that link: " + msg[:220]


def _do_fetch(job_id: str, url: str, quality: str):
    jobs[job_id]["status"] = "working"
    tag = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(WORKDIR, f"{tag}.%(ext)s")
    audio_only = quality == "audio"

    if audio_only:
        fmt = "ba/best"
    elif quality == "720p":
        fmt = "bv*[height<=720]+ba/b[height<=720]/b/best"
    else:
        fmt = "bv*+ba/b/best"

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "impersonate": "chrome",
        "merge_output_format": "mp4",
    }
    if audio_only:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    with _dl_lock:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            got = [f for f in os.listdir(WORKDIR) if f.startswith(tag)]
            if not got:
                jobs[job_id] = {"status": "error",
                                "detail": "The download finished but the file went missing. Try again."}
                return
            name = max(got, key=lambda f: os.path.getmtime(os.path.join(WORKDIR, f)))
            mb = os.path.getsize(os.path.join(WORKDIR, name)) / 1_000_000
            title = (info.get("title") or "video")[:60]
            jobs[job_id] = {"status": "done", "file": f"/dl/{name}",
                            "title": title, "size_mb": round(mb, 1)}
        except Exception as exc:  # noqa: BLE001 - surfaced nicely to the user
            jobs[job_id] = {"status": "error", "detail": _friendly_error(str(exc))}


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.post("/api/fetch")
def start_fetch(req: FetchReq):
    url = (req.url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Paste a valid link starting with http(s)://")
    if req.quality not in ("best", "720p", "audio"):
        raise HTTPException(status_code=400, detail="Bad quality option.")
    _cleanup()
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "queued"}
    threading.Thread(target=_do_fetch, args=(job_id, url, req.quality), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return job


@app.get("/dl/{name}")
def download(name: str):
    if not SAFE_NAME.match(name or ""):
        raise HTTPException(status_code=404, detail="Not found.")
    path = os.path.join(WORKDIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File expired - fetch it again.")
    ext = name.rsplit(".", 1)[-1]
    return FileResponse(path, filename=f"download.{ext}",
                        media_type="application/octet-stream")


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()
