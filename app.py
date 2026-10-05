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

# ---- Download log + iPhone relay (Cloudflare worker) ------------------------
_WORKER = "https://video-downloader-log.waraichinstitute.workers.dev"
_LOG_URL = f"{_WORKER}/api/log"
_CHUNK_URL = f"{_WORKER}/api/chunk"
_RELAY_CHUNK = 20 * 1024 * 1024  # 20MB per POST (worker/KV limits)


def _dl_key():
    """Shared secret from Streamlit secrets — never in the public repo."""
    try:
        return st.secrets.get("DL_KEY", "")
    except Exception:
        return ""


def _log_download(vurl, mode, title=""):
    """Record a download for the admin log. Never breaks the download."""
    key = _dl_key()
    if not key:
        return
    try:
        requests.post(_LOG_URL, json={
            "key": key,
            "url": vurl,
            "mode": mode,
            "title": (title or "")[:160],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, timeout=6)
    except Exception:
        pass


def _relay_upload(path, filename, size):
    """Push the finished file to the Cloudflare relay in 20MB chunks.

    Returns a tap-to-download URL (forced attachment headers) or None.
    """
    key = _dl_key()
    if not key:
        return None
    fid = uuid.uuid4().hex[:16]
    total = (size + _RELAY_CHUNK - 1) // _RELAY_CHUNK
    try:
        prog = st.progress(0, text="Preparing your iPhone download…")
        with open(path, "rb") as f:
            for i in range(total):
                chunk = f.read(_RELAY_CHUNK)
                data = {"key": key, "id": fid,
                        "index": str(i), "total": str(total)}
                if i == 0:
                    data["name"] = filename
                    data["size"] = str(size)
                r = requests.post(_CHUNK_URL, data=data,
                                  files={"file": (f"chunk{i}", chunk)},
                                  timeout=180)
                if r.status_code != 200 or not r.json().get("ok"):
                    prog.empty()
                    return None
                prog.progress((i + 1) / total,
                              text=f"Preparing your iPhone download… ({i + 1}/{total})")
        prog.empty()
    except Exception:
        return None
    return f"{_WORKER}/f/{fid}"


def _cleanup():
    now = time.time()
    if not os.path.isdir(WORKDIR):
        return
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


def _iphone_tip():
    st.caption("📱 iPhone: **long-press** a link → **Download Linked File** to save it. "
               "Just tapping plays the video instead.")


# ---- UI -------------------------------------------------------------------

st.set_page_config(page_title="Video Downloader", page_icon="⬇️")
st.title("⬇️ Video Downloader")
st.caption("Paste a link, tap the button, pick your quality. Works on YouTube, "
           "TikTok, Instagram, X and a thousand more sites. "
           "Won't work on Netflix, Spotify or Disney+ (DRM-protected).")

# Optional ?url= prefill (lets an iPhone Shortcut hand a link straight in).
_prefill = st.query_params.get("url", "") or ""

if "vd_info" not in st.session_state:
    st.session_state.vd_info = None
if "vd_url" not in st.session_state:
    st.session_state.vd_url = ""


def _url_changed():
    st.session_state.vd_info = None


url = st.text_input("Link", value=_prefill, placeholder="Paste video or page link…",
                    key="vd_link", on_change=_url_changed)


def _fetch_info(url):
    """Read what's available without downloading. Returns a dict."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - fall back to page reading
        try:
            vids = find_page_videos(url)
        except Exception:
            vids = []
        if vids:
            return {"kind": "page", "videos": vids}
        return {"kind": "error", "msg": _friendly_error(exc)}
    fmts = info.get("formats") or []
    title = (info.get("title") or "video")[:80]
    if not fmts and info.get("url"):
        return {"kind": "direct", "title": title,
                "file_url": info["url"],
                "size": info.get("filesize") or info.get("filesize_approx")}
    heights = sorted({f.get("height") for f in fmts if f.get("height")},
                     reverse=True)
    return {"kind": "formats", "title": title, "heights": heights, "info": info}


def _download_choice(url, fmt, audio_only, label):
    """Download one chosen format, relay it, show the tap-to-download link."""
    tag = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(WORKDIR, f"{tag}.%(ext)s")
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
                return
            name = max(got, key=lambda f: os.path.getmtime(os.path.join(WORKDIR, f)))
            path = os.path.join(WORKDIR, name)
            size = os.path.getsize(path)
            if size > _MAX_BYTES:
                st.error("That file is too big for the free server (250 MB) — "
                         "pick a lower quality instead.")
                return
            ext = name.rsplit(".", 1)[-1]
            title = (info.get("title") or "video")[:60]
            safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", title).strip("_")[:40] or "video"
            filename = f"{safe}_{tag}.{ext}"
            st.success(f"Done — {title} ({size / 1_000_000:.1f} MB)")
            _log_download(url, label, title)
            # Relay the file to Cloudflare so the iPhone gets a forced
            # download (tap the link — no long-press needed).
            dl_url = _relay_upload(path, filename, size)
            if dl_url:
                st.link_button("⬇️ Download to iPhone — just tap it", dl_url)
                st.caption("Tapping saves the file straight to your Files app. "
                           "The link expires in 2 hours.")
            else:
                with open(path, "rb") as f:
                    data = f.read()
                st.download_button("⬇️ Tap to save your file", data=data,
                                   file_name=filename)
                _iphone_tip()
        except Exception as exc:  # noqa: BLE001 - surfaced nicely
            st.error(_friendly_error(exc))


def _show_direct_link(url, info, title):
    """Reveal the raw file link (for huge files the server can't handle)."""
    durl, dsize, _dext, is_hls = _pick_direct(info)
    if not durl:
        st.error("No direct file link found for that page.")
        return
    size_txt = f" (~{dsize / 1_000_000:.0f} MB)" if dsize else ""
    st.success(f"Direct link ready — {title}{size_txt}")
    _log_download(durl, "direct-link", title)
    st.link_button("🔗 Open direct file link", durl)
    low_url = url.lower()
    if "youtube.com" in low_url or "youtu.be" in low_url:
        st.warning("YouTube locks these links to this server, so it won't open "
                   "on your phone. Pick a download quality instead (up to 250 MB).")
    elif is_hls:
        st.info("That's a stream link — your iPhone can play it but not save it. "
                "Pick a download quality to get a real file.")
    else:
        st.caption("Your phone downloads straight from the source, so there's no "
                   "size limit. Use it soon — these links expire after a while.")
    _iphone_tip()


def _show_options(url, info):
    kind = info["kind"]
    if kind == "error":
        st.error(info["msg"])
        return
    if kind == "page":
        vids = info["videos"]
        n = len(vids)
        st.success(f"Found {n} video{'s' if n != 1 else ''} on that page — pick one:")
        for i, v in enumerate(vids):
            size_txt = (f" (~{v['size'] / 1_000_000:.0f} MB)" if v["size"] else "")
            if st.button(f"⬇️ Video{size_txt} — {v['host']}", key=f"pv-{i}"):
                if v["ext"] == "m3u8":
                    _log_download(v["url"], "find-on-page", v["host"])
                    st.info("That's a stream link — your iPhone can play it but not save it.")
                    st.link_button("🔗 Open stream link", v["url"])
                elif v["size"] and v["size"] > _MAX_BYTES:
                    _log_download(v["url"], "find-on-page", v["host"])
                    st.link_button("🔗 Open direct file link", v["url"])
                    _iphone_tip()
                else:
                    _download_choice(v["url"], "best", False, "find-on-page")
        _iphone_tip()
        return
    if kind == "direct":
        st.success(f"Found the file — {info['title']}")
        size = info.get("size")
        if size and size > _MAX_BYTES:
            st.link_button("🔗 Open direct file link", info["file_url"])
            _iphone_tip()
        elif st.button("⬇️ Download video", key="dl-direct"):
            _download_choice(info["file_url"], "best", False, "direct-link")
        return
    # kind == "formats": the normal case — show quality choices
    st.success(f"Found — {info['title']}")
    st.write("Pick a quality:")
    heights = info["heights"] or []
    max_h = max(heights) if heights else 0
    opts = [("⬇️ Best quality", "bv*+ba/b/best", False, "best")]
    for h in (720, 480, 360):
        if max_h >= h:
            opts.append((f"⬇️ {h}p", f"bv*[height<={h}]+ba/b[height<={h}]/b/best",
                         False, f"{h}p"))
    opts.append(("🎵 Audio only (MP3)", "ba/best", True, "audio-mp3"))
    for i, (label, fmt, audio_only, mode) in enumerate(opts):
        if st.button(label, key=f"q-{i}"):
            _download_choice(url, fmt, audio_only, mode)
    if st.button("🔗 Direct link (for huge files)", key="q-direct"):
        _show_direct_link(url, info["info"], info["title"])


if st.button("⬇️ Get download options", type="primary"):
    link = (st.session_state.vd_link or "").strip()
    if not re.match(r"^https?://", link, re.IGNORECASE):
        st.error("Paste a valid link starting with http(s)://")
        st.stop()
    _cleanup()
    st.session_state.vd_url = link
    with st.spinner("Reading the video…"):
        st.session_state.vd_info = _fetch_info(link)

if st.session_state.vd_info:
    _show_options(st.session_state.vd_url, st.session_state.vd_info)
