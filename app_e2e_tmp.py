import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
import app as A
print("=== EJS STATUS ===", A.EJS_STATUS)
c = A.app.test_client()
URL = "https://www.youtube.com/watch?v=jRPjLb3ZjCo"
# Test the ACTUAL /info route (goes through extract_with_fallback + BASE_YDL_OPTS)
r = c.post("/info", data={"url": URL})
print("POST /info:", r.status_code, r.get_json())
# Test /download audio (small/fast)
r2 = c.post("/download", data={"url": URL, "type": "audio", "quality": "128"})
print("POST /download/audio:", r2.status_code, r2.headers.get("Content-Disposition", "")[:120])
if r2.status_code != 200:
    print("  body:", r2.data.decode("utf-8", "ignore")[:400])
print("DONE")
