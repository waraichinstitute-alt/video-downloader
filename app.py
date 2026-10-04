"""Personal video downloader - Streamlit app for Streamlit Community Cloud (free tier)."""

import os
import re
import time
import uuid
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
import yt_dlp

WORKDIR = "/tmp/vd-downloads"
os.makedirs(WORKDIR, exist_ok=True)
_MAX_AGE = 2 * 3600
_MAX_BYTES = 250 * 1_000_000  # free server guardrail (through-server downloads)

_BROWSER_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
               "Mobile/15E148 Safari/604.1")
_VIDEO_EXTS = ("mp4", "m3u8", "mpd", "webm", "mov", "m4v", "ogv")


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


# ---- "Find video on page" mode -------------------------------------------
# Reads a page's HTML (plus one level of embedded iframes) and collects the
# video file addresses written in its code. The phone then downloads the
# chosen one straight from the source.

def _get_html(page_url: str) -> str:
    r = requests.get(page_url, headers={"User-Agent": _BROWSER_UA}, timeout=20)
    r.raise_for_status()
    return r.text


def _extract_candidates(html: str, base: str, add):
    html = html.replace("\\/", "/")  # unescape JSON-style slashes
    patterns = [
        r'<video[^>]+src=["\']([^"\']+)["\']',
        r'<video[^>]+data-src=["\']([^"\']+)["\']',
        r'<source[^>]+src=["\']([^"\']+)["\']',
        r'<source[^>]+data-src=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']',
        # JS player configs (jwplayer / videojs style):  file: "https://..." / src: '...'
        r'(?:file|src)\s*:\s*["\'](https?://[^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            add(m.group(1), base)
    # plain download anchors pointing at video files
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        if _looks_like_video(urljoin(base, m.group(1).strip())):
            add(m.group(1), base)
    # any quoted http(s) URL ending in a video extension (catches JS-embedded URLs)
    exts = "|".join(_VIDEO_EXTS)
    for m in re.finditer(r'["\'](https?://[^"\']+?\.(?:' + exts + r')(?:\?[^"\']*)?)["\']',
                         html, re.IGNORECASE):
        add(m.group(1), base)


def _iframe_srcs(html: str, base: str):
    srcs = []
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        u = urljoin(base, m.group(1).strip())
        if u.startswith("http"):
            srcs.append(u)
    return srcs


def _looks_like_video(u: str) -> bool:
    path = urlparse(u).path.lower()
    return any(path.endswith("." + e) for e in _VIDEO_EXTS)


def _probe(u: str):
    """HEAD the URL. Returns (size_bytes_or_None, is_video_bool)."""
    try:
        r = requests.head(u, headers={"User-Agent": _BROWSER_UA},
                          timeout=8, allow_redirects=True)
        if r.status_code >= 400:
            return None, False
        ctype = (r.headers.get("content-type") or "").lower()
        size = r.headers.get("content-length")
        size = int(size) if size and size.isdigit() else None
        ok = ("video" in ctype or "mpegurl" in ctype
              or "mp2t" in ctype or "dash" in ctype)
        return size, ok
    except Exception:
        return None, False


def find_page_videos(page_url: str):
    seen = set()
    cands = []

    def add(u, base):
        u = urljoin(base, (u or "").strip())
        if not u.startswith("http") or u in seen:
            return
        seen.add(u)
        cands.append(u)

    html = _get_html(page_url)
    _extract_candidates(html, page_url, add)
    for src in _iframe_srcs(html, page_url)[:5]:
        try:
            _extract_candidates(_get_html(src), src, add)
        except Exception:
            pass

    results = []
    for u in cands:
        if len(results) >= 10:
            break
        size, is_video = _probe(u)
        if not _looks_like_video(u) and not is_video:
            continue
        ext = urlparse(u).path.rsplit(".", 1)[-1].lower()[:4]
        results.append({"url": u, "size": size, "ext": ext,
                        "host": urlparse(u).netloc})
    return results


def _pick_direct(info):
    """Best direct file URL from extracted info.

    Returns (url, size_bytes_or_None, ext, is_hls). The phone downloads
    straight from the source, so the free server's limits don't apply.
    """
    fmts = [f for f in (info.get("formats") or []) if f.get("url")]
    if not fmts:
        u = info.get("url")  # single direct file, no format list
        if not u:
            return None, None, "", False
        return (u, info.get("filesize") or info.get("filesize_approx"),
                info.get("ext") or "", ".m3u8" in u)

    def key(f):
        v = (f.get("vcodec") or "none") != "none"
        a = (f.get("acodec") or "none") != "none"
        ext = (f.get("ext") or "")
        size = f.get("filesize") or f.get("filesize_approx") or 0
        return (v and a, ext == "mp4", size)  # progressive mp4 first

    best = max(fmts, key=key)
    u = best["url"]
    is_hls = "m3u8" in str(best.get("protocol") or "") or ".m3u8" in u
    return (u, best.get("filesize") or best.get("filesize_approx"),
            best.get("ext") or "", is_hls)


# ---- UI -------------------------------------------------------------------

st.set_page_config(page_title="Video Downloader", page_icon="⬇️")
st.title("⬇️ Video Downloader")
st.caption("Paste a link, get the file. YouTube, TikTok, Instagram, X and a thousand more "
           "sites. Won't work on Netflix, Spotify or Disney+ (DRM-protected). "
           "Got a huge file? Use “Direct link (big files)” — your phone grabs it "
           "straight from the source with no size limit. "
           "See a video playing on some page? “Find video on page” hunts it down in the page's code.")

# Optional ?url= prefill (lets an iPhone Shortcut hand a link straight in).
_prefill = st.query_params.get("url", "") or ""
_modes = ["720p or smaller", "Best quality", "Audio only (MP3)",
          "Direct link (big files)", "🔍 Find video on page"]
_default_mode = ("Direct link (big files)" if _looks_like_video(_prefill)
                 else "720p or smaller")

url = st.text_input("Link", value=_prefill, placeholder="Paste video or page link…")
quality = st.radio("Mode", _modes, index=_modes.index(_default_mode))

if st.button("Download", type="primary"):
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        st.error("Paste a valid link starting with http(s)://")
        st.stop()
    _cleanup()

    if quality.startswith("🔍"):
        with st.spinner("Reading the page and hunting for videos…"):
            try:
                vids = find_page_videos(url)
            except requests.RequestException:
                st.error("That page blocks automatic reading (bot protection). "
                         "Your phone's browser is the only thing allowed in there, "
                         "so the server can't see its videos.")
                st.stop()
            except Exception as exc:  # noqa: BLE001 - surfaced nicely
                st.error(_friendly_error(exc))
                st.stop()
            if not vids:
                st.warning("No downloadable videos found in that page's code. "
                           "Its player probably builds the video with live scripts, "
                           "which only your phone's browser can see — the server can't.")
            else:
                n = len(vids)
                st.success(f"Found {n} video{'s' if n != 1 else ''} on that page:")
                for v in vids:
                    size_txt = (f" (~{v['size'] / 1_000_000:.0f} MB)"
                                if v["size"] else " (size unknown)")
                    st.link_button(f"⬇️ Open video{size_txt} — {v['host']}", v["url"])
                    if v["ext"] == "m3u8":
                        st.caption("Stream link — your iPhone can play it but not save it.")
        st.stop()

    if quality.startswith("Direct"):
        with st.spinner("Finding the direct file link…"):
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                       "noplaylist": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
                durl, dsize, _dext, is_hls = _pick_direct(info)
                if not durl:
                    st.error("No direct file link found for that page — "
                             "try a download option instead.")
                else:
                    title = (info.get("title") or "video")[:60]
                    size_txt = f" (~{dsize / 1_000_000:.0f} MB)" if dsize else ""
                    st.success(f"Direct link ready — {title}{size_txt}")
                    st.link_button("🔗 Open direct file link", durl)
                    low_url = url.lower()
                    if "youtube.com" in low_url or "youtu.be" in low_url:
                        st.warning("YouTube locks these links to this server, so it "
                                   "won't open on your phone. For YouTube use a "
                                   "download option (up to 250 MB).")
                    elif is_hls:
                        st.info("That's a stream link — your iPhone can play it but "
                                "not save it. Use a download option to get a real file.")
                    else:
                        st.caption("Your phone downloads straight from the source, "
                                   "so there's no size limit. Use it soon — these "
                                   "links expire after a while.")
            except Exception as exc:  # noqa: BLE001 - surfaced nicely
                st.error(_friendly_error(exc))
        st.stop()

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
                st.error("That file is too big for the free server — "
                         "try “Direct link (big files)” instead.")
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
