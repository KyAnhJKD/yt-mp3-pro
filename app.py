import os
import re
import uuid
import glob
import time
import logging
import shutil
import subprocess
from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-mp3-pro")
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = "ffmpeg"
AUDIO_QUALITIES = {"128", "192", "256", "320"}
# Client YouTube thử theo thứ tự: web trước (đủ format FullHD-4K),
# rớt sang android/ios/mweb khi bị chặn "Could not extract any player response".
YOUTUBE_CLIENT_FALLBACKS = (
    ["web", "android"],
    ["android", "ios"],
    ["android"],
    ["mweb"],
    ["web"],
)
# PO token provider (máy chủ datacenter / IP bị chặn thường cần PO token cho GVS web).
# Tham khảo: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
POT_PROVIDER_URL = os.environ.get("YTDLP_PO_PROVIDER", "")  # vd: http://bgutil-pot-provider:8080
BASE_YDL_OPTS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "windowsfilenames": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "extractor_retries": 3,
    "concurrent_fragment_downloads": 4,
    # --- EJS (External JS Scripts) theo wiki https://github.com/yt-dlp/yt-dlp/wiki/EJS ---
    # YouTube bat giai ma JS challenge bang runtime ngoai (Deno/Node/QuickJS)
    # + script yt-dlp-ejs. Bat ca 2 de tu fallback runtime kha dung.
    "js_runtimes": {"node": {}, "deno": {}, "quickjs": {}},
    "remote_components": ["ejs:github"],
}


from pathlib import Path
from contextlib import contextmanager
import subprocess
try:
    import bgutil_pot
    from bgutil_pot import BGUtil
    _BGUTIL_OK = True
    BGUTIL_VER = getattr(bgutil_pot, "__version__", None) or "unknown"
except Exception:
    _BGUTIL_OK = False
    BGUTIL_VER = None


@contextmanager
def po_token_for(video_id=""):
    """Cap 1 PO token tu bgutil trong vong 30s (datacenter IP). Tra ve token string."""
    if not _BGUTIL_OK:
        yield None
    else:
        try:
            util = BGUtil()
            token = util.get_po_token(video_id=video_id) if video_id else util.get_po_token()
            yield token
        except Exception as exc:
            logger.warning("bgutil pot failed: %s", exc)
            yield None


def get_ejs_status():
    """Kiem tra runtime JS + goi yt-dlp-ejs + PO token (de hien thi /ejs + log khi start)."""
    runtimes = {}
    for exe in ("deno", "node", "quickjs"):
        path = shutil.which(exe)
        ver = None
        if path:
            try:
                flag = "--version" if exe != "deno" else "--version"
                out = subprocess.run([path, flag], capture_output=True, text=True, timeout=10)
                ver = (out.stdout or out.stderr or "").strip().splitlines()[0][:80] if (out.stdout or out.stderr) else None
            except Exception:
                ver = None
        runtimes[exe] = {"path": path, "version": ver, "ok": bool(path)}
    try:
        import importlib.metadata as md
        ejs_ver = md.version("yt-dlp-ejs")
    except Exception:
        ejs_ver = None
    # yt-dlp >= 2025.x moi ho tro js_runtimes/remote_components
    opts_ok = True
    try:
        with yt_dlp.YoutubeDL(dict(BASE_YDL_OPTS, quiet=True, skip_download=True)):
            pass
    except Exception as exc:
        opts_ok = False
        logger.warning("EJS opts not supported by installed yt-dlp: %s", exc)
    ready = (any(v["ok"] for v in runtimes.values()) and opts_ok)
    return {"runtimes": runtimes, "ejs_package": ejs_ver, "bgutil": (BGUTIL_VER, _BGUTIL_OK),
            "opts_ok": opts_ok, "ready": ready, "po_provider": POT_PROVIDER_URL or "(khong cau hinh)",
            "ytdlp": yt_dlp.version.__version__}


EJS_STATUS = get_ejs_status()
logger.info("EJS status: %s", EJS_STATUS)
VIDEO_QUALITIES = {
    "480": {"label": "480p (SD)", "height": 480, "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best"},
    "720": {"label": "720p (HD)", "height": 720, "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best"},
    "1080": {"label": "1080p (Full HD)", "height": 1080, "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"},
    "1440": {"label": "1440p (2K QHD)", "height": 1440, "format": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best"},
    "2160": {"label": "2160p (4K UHD)", "height": 2160, "format": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo+bestaudio/best"},
    "best": {"label": "Tot nhat (len toi 4K/8K)", "height": None, "format": "bestvideo+bestaudio/best"},
}
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')

def is_valid_url(url):
    return bool(url) and bool(URL_RE.match(url.strip())) and len(url.strip()) < 2048


def safe_download_name(title, ext):
    name = (title or "media").strip() or "media"
    name = SAFE_NAME_RE.sub("", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > 120:
        name = name[:120].rstrip()
    return "%s.%s" % (name, ext)


def find_downloaded_file(id_tag):
    pattern = os.path.join(DOWNLOAD_FOLDER, "%s_*" % id_tag)
    matches = glob.glob(pattern)
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def is_player_response_error(exc):
    msg = str(exc or "").lower()
    keys = (
        "could not extract any player response",
        "unable to extract player response",
        "requested format is not available",
        "http error 403",
        "sign in to confirm",
        "player response",
    )
    return any(k in msg for k in keys)


def with_client(opts, clients):
    out = dict(opts)
    ya = {"player_client": list(clients)}
    # PO token provider (datacenter IP thuong can) - giu nguyen qua ca client fallback.
    # Tham khao: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
    if POT_PROVIDER_URL:
        ya["po_token"] = "true"
        ya["pot_provider_url"] = POT_PROVIDER_URL
    out["extractor_args"] = {"youtube": ya}
    # Dam bao EJS opts luon di kem moi request (ke ca khi goi voi base_opts cu).
    out.setdefault("js_runtimes", BASE_YDL_OPTS["js_runtimes"])
    out.setdefault("remote_components", BASE_YDL_OPTS["remote_components"])
    return out


def extract_with_fallback(url, base_opts, download, format_fallbacks=()):
    """Thử lần lượt các player_client YouTube cho tới khi thành công."""
    last_exc = None
    format_tries = [None] + list(format_fallbacks or [])
    for clients in YOUTUBE_CLIENT_FALLBACKS:
        for fmt in format_tries:
            opts = with_client(base_opts, clients)
            if fmt is not None:
                opts = dict(opts)
                opts["format"] = fmt
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=download)
                logger.info("youtube client OK: %s format=%s", ",".join(clients), opts.get("format"))
                return info, opts
            except Exception as exc:
                last_exc = exc
                logger.warning("youtube client %s format=%s failed: %s", ",".join(clients), opts.get("format"), str(exc)[:300])
                if not is_player_response_error(exc):
                    # Lỗi khác (riêng tư/xóa/URL sai): không cần thử tiếp
                    raise
                continue
    raise last_exc


def build_audio_opts(id_tag, quality):
    opts = dict(BASE_YDL_OPTS)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%s_%%(title)s.%%(ext)s" % id_tag),
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}],
    })
    return opts


def build_video_opts(id_tag, vq):
    fmt = VIDEO_QUALITIES.get(vq, VIDEO_QUALITIES["1080"])["format"]
    opts = dict(BASE_YDL_OPTS)
    opts.update({
        "format": fmt,
        "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%s_%%(title)s.%%(ext)s" % id_tag),
        "ffmpeg_location": FFMPEG_PATH,
        "merge_output_format": "mp4",
    })
    return opts


def friendly_error(exc, with_code=False):
    raw = str(exc or "")
    low = raw.lower()
    if "could not extract any player response" in low or "unable to extract player response" in low:
        msg = ("YouTube bat JS challenge nhung server chua giai duoc (can EJS + PO token). "
               "Server da cau hinh: js_runtimes = %s, remote_components = ejs:github, "
               "player_client fallback = web,android/ios/mweb. "
               "Neu van loi: server cua ban dang dung IP datacenter bi chan. "
               "Cai offline yt-dlp: pip install -U \"yt-dlp[default]\"; kiem tra Node/Deno: de /ejs; "
               "hoac cau hinh PO token provider (env YTDLP_PO_PROVIDER). "
               "Chi tiet: https://github.com/yt-dlp/yt-dlp/wiki/EJS")
        code = 502
    elif "requested format is not available" in low:
        msg = ("Video nay khong co dung dinh dang/chat luong da chon (dac biet voi 4K). "
               "Hay chon 1080p hoac 'Tot nhat' roi thu lai.")
        code = 400
    elif "private video" in low or "login required" in low or "sign in to confirm" in low:
        msg = "Khong tai duoc (video rieng tu / yeu cau dang nhap / bi gioi han do tuoi): %s" % raw[:300]
        code = 400
    elif "video unavailable" in low or "removed" in low or "deleted" in low:
        msg = "Video khong kha dung (da xoa / an / gioi han khu vuc)."
        code = 400
    elif "http error 403" in low:
        msg = ("YouTube chan IP/may chu tam thoi (403). Hay doi mang, thu lai sau, hoac chay o may local.")
        code = 502
    else:
        msg = "Loi may chu khi xu ly: %s" % raw[:500]
        code = 500
    if with_code:
        return msg, code
    return msg


def cleanup_old_files(max_age_seconds=3600):
    try:
        now = time.time()
        for path in glob.glob(os.path.join(DOWNLOAD_FOLDER, "*")):
            try:
                if os.path.isfile(path) and (now - os.path.getmtime(path) > max_age_seconds):
                    os.remove(path)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("cleanup_old_files failed: %s", exc)


@app.route("/ejs")
def ejs():
    """Endpoint chuẩn đoạn EJS/PO theo wiki: runtime + yt-dlp-ejs + options."""
    status = get_ejs_status()
    status["po_token_provider"] = POT_PROVIDER_URL or "(không cau hinh)"
    return jsonify({"ok": True, "ejs": status})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
def info():
    url = (request.form.get("url") or "").strip()
    if not is_valid_url(url):
        return jsonify({"ok": False, "error": "URL khong hop le."}), 400
    try:
        base = dict(BASE_YDL_OPTS)
        base["skip_download"] = True
        data, _opts = extract_with_fallback(url, base, download=False)
        return jsonify({"ok": True, "title": data.get("title"), "duration": data.get("duration"), "uploader": data.get("uploader"), "thumbnail": data.get("thumbnail")})
    except Exception as exc:
        logger.exception("info failed")
        return jsonify({"ok": False, "error": friendly_error(exc)}), 500


@app.route("/download", methods=["POST"])
def download():
    form = request.form if request.form else {}
    url = (form.get("url") or "").strip()
    dl_type = (form.get("type") or "audio").strip().lower()
    quality = str(form.get("quality") or "").strip()
    if not is_valid_url(url):
        return "URL khong hop le. Vui long dan link YouTube bat dau bang http(s)://", 400
    if quality in VIDEO_QUALITIES:
        dl_type = "video"
    elif dl_type in ("video", "mp4"):
        dl_type = "video"
    else:
        dl_type = "audio"
    cleanup_old_files()
    id_tag = uuid.uuid4().hex[:8]
    try:
        if dl_type == "video":
            vq = quality if quality in VIDEO_QUALITIES else "1080"
            ydl_opts = build_video_opts(id_tag, vq)
            # Fallback format: nếu video không có đúng height yêu cầu / client chặn,
            # tự hạ xuống best để vẫn tải được thay vì báo "Requested format is not available".
            info, _opts = extract_with_fallback(url, ydl_opts, download=True, format_fallbacks=("bestvideo+bestaudio/best", "best"))
            filepath = find_downloaded_file(id_tag)
            if not filepath or not os.path.isfile(filepath):
                return "Tai video that bai: khong tim thay file sau khi xu ly.", 500
            ext = os.path.splitext(filepath)[1].lstrip(".") or "mp4"
            title = (info.get("title") if isinstance(info, dict) else None) or os.path.basename(filepath)
            download_name = safe_download_name(title, ext)

            @after_this_request
            def _cleanup_v(response):
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                except OSError:
                    pass
                return response

            return send_file(filepath, as_attachment=True, download_name=download_name, mimetype="video/mp4")
        if quality not in AUDIO_QUALITIES:
            quality = "192"
        ydl_opts = build_audio_opts(id_tag, quality)
        info, _opts = extract_with_fallback(url, ydl_opts, download=True, format_fallbacks=("bestaudio/best", "best"))
        filepath = find_downloaded_file(id_tag)
        if not filepath or not os.path.isfile(filepath):
            return "Tai MP3 that bai: khong tim thay file sau khi chuyen doi.", 500
        title = (info.get("title") if isinstance(info, dict) else None) or os.path.basename(filepath)
        download_name = safe_download_name(title, "mp3")

        @after_this_request
        def _cleanup_a(response):
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
            except OSError:
                pass
            return response

        return send_file(filepath, as_attachment=True, download_name=download_name, mimetype="audio/mpeg")
    except Exception as exc:
        logger.exception("download failed")
        msg, code = friendly_error(exc, with_code=True)
        return msg, code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
