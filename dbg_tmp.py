import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import yt_dlp
URL = "https://www.youtube.com/watch?v=jRPjLb3ZjCo"
print("VERSION:", yt_dlp.version.__version__)
# 1) verbose that loi that
opts = {"skip_download": True, "verbose": True, "noplaylist": True,
        "js_runtimes": {"node": {}, "deno": {}, "quickjs": {}},
        "remote_components": ["ejs:github"]}
try:
    with yt_dlp.YoutubeDL(opts) as ydl:
        d = ydl.extract_info(URL, download=False)
    print("VERBOSE_OK:", (d.get("title") or "")[:100], "| n=", len(d.get("formats") or []))
except Exception as e:
    print("VERBOSE_FAIL:", str(e)[:2000])
# 2) thu android (khong can PO token / EJS) lay audio (chung minh huong fix)
opts2 = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True,
         "format": "bestaudio/best",
         "extractor_args": {"youtube": {"player_client": ["android"]}},
         "js_runtimes": {"node": {}},
         "remote_components": ["ejs:github"]}
try:
    with yt_dlp.YoutubeDL(opts2) as ydl:
        d2 = ydl.extract_info(URL, download=False)
    print("ANDROID_OK:", (d2.get("title") or "")[:100], "| id=", d2.get("id"))
except Exception as e:
    print("ANDROID_FAIL:", str(e)[:1000])
