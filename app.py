"""Personal video downloader - Streamlit app for Streamlit Community Cloud (free tier)."""

import os
import re
import time
import uuid

import streamlit as st
import yt_dlp

WORKDIR = "/tmp/vd-downloads"
os.makedirs(WORKDIR, exist_ok=True)
_MAX_AGE = 2 * 3600
_MAX_BYTES = 250 * 1_000_000  # free server guardrail


def _cleanup():
    now = time.time()
    for name in os.listdir(WORKDIR):
        path = os.path.join(WORKDIR, name)
        try:
            if now - os.path.getmtime(path) > _MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def _friendly_error(exc: Exception) -> str:
    msg = str(exc).strip() or f"{type(exc).__name__} (no detail given)"
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked for bot verification on this one. "
                "Wait a bit and try again, or try a different video.")
    if "drm" in low or "encrypted" in low:
        return "This video is DRM-protected (like Netflix/Spotify). No downloader can grab those."
    if "login required" in low or "log in" in low:
        return "This one needs a login on the site, so it can't be fetched."
    if "unsupported url" in low:
        return "That link isn't a video/file this downloader understands."
    return "Couldn't fetch that link: " + msg[:220]


st.set_page_config(page_title="Video Downloader", page_icon="⬇️")
st.title("⬇️ Video Downloader")
st.caption("Paste a link, get the file. YouTube, TikTok, Instagram, X and a thousand more "
           "sites. Won't work on Netflix, Spotify or Disney+ (DRM-protected).")

url = st.text_input("Link", placeholder="Paste video link…")
quality = st.radio("Quality",
                   ["720p or smaller", "Best quality", "Audio only (MP3)"],
                   horizontal=True)

if st.button("Download", type="primary"):
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        st.error("Paste a valid link starting with http(s)://")
        st.stop()
    _cleanup()

    tag = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(WORKDIR, f"{tag}.%(ext)s")
    audio_only = quality.startswith("Audio")
    fmt = ("ba/best" if audio_only
           else "bv*+ba/b/best" if quality.startswith("Best")
           else "bv*[height<=720]+ba/b[height<=720]/b/best")

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    if audio_only:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    with st.spinner("Downloading… big videos can take a minute."):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            got = [f for f in os.listdir(WORKDIR) if f.startswith(tag)]
            if not got:
                st.error("The download finished but the file went missing. Try again.")
                st.stop()
            name = max(got, key=lambda f: os.path.getmtime(os.path.join(WORKDIR, f)))
            path = os.path.join(WORKDIR, name)
            size = os.path.getsize(path)
            if size > _MAX_BYTES:
                st.error("That file is too big for the free server — try “720p or smaller”.")
                st.stop()
            ext = name.rsplit(".", 1)[-1]
            title = (info.get("title") or "video")[:60]
            with open(path, "rb") as f:
                data = f.read()
            st.success(f"Done — {title} ({size / 1_000_000:.1f} MB)")
            st.download_button("⬇️ Tap to save your file", data=data,
                               file_name=f"download.{ext}")
        except Exception as exc:  # noqa: BLE001 - surfaced nicely
            st.error(_friendly_error(exc))
