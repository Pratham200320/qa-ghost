"""QA Ghost — FastAPI Backend (Cloud Run ready)"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio, uvicorn, os
from agent import run_qa_scan

app = FastAPI(title="QA Ghost", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

for d in ["recordings", "screenshots", "voice"]:
    os.makedirs(d, exist_ok=True)

app.mount("/recordings",  StaticFiles(directory="recordings"),  name="recordings")
app.mount("/screenshots", StaticFiles(directory="screenshots"), name="screenshots")
app.mount("/voice",       StaticFiles(directory="voice"),       name="voice")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
async def health():
    return {"status": "ok", "service": "qa-ghost"}

@app.get("/video/{filename}")
async def serve_video(filename: str):
    """Serve video with proper headers for Chrome seeking."""
    from fastapi.responses import FileResponse
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    # security: no path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "invalid"}, status_code=400)
    path = os.path.join("recordings", filename)
    if not os.path.exists(path):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "not found"}, status_code=404)
    media_type = "video/mp4" if filename.endswith(".mp4") else "video/webm"
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
    )

@app.post("/scan")
async def scan(request: Request):
    import json as _json
    from fastapi.responses import StreamingResponse as _SR
    data = await request.json()
    url  = data.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "No URL provided"}, status_code=400)
    if not url.startswith("http"):
        url = "https://" + url

    async def event_stream():
        try:
            loop = asyncio.get_event_loop()
            fut = loop.run_in_executor(None, run_qa_scan, url)
            while not fut.done():
                yield ": keepalive\n\n"
                await asyncio.sleep(5)
            d = await fut
            video_path = d.get("video_path")
            voice_path = d.get("voice_audio_path")
            result = {
                "results":              d["results"],
                "actions_log":          d["actions_log"],
                "video_filename":       os.path.basename(video_path) if video_path else None,
                "voice_summary":        d["voice_summary"],
                "voice_filename":       os.path.basename(voice_path) if voice_path else None,
                "healed_bugs":          d.get("healed_bugs", []),
                "accessibility_report": d.get("accessibility_report", {}),
                "core_web_vitals":      d.get("core_web_vitals", {}),
                "viewports":            d.get("viewports", {}),
                "network_issues":       d.get("network_issues", []),
                "health_score":         d.get("health_score", 0),
                "health_grade":         d.get("health_grade", "F"),
                "score_breakdown":      d.get("score_breakdown", []),
            }
            yield f"data: {_json.dumps(result)}\n\n"
        except Exception as e:
            import traceback; traceback.print_exc()
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"

    return _SR(event_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n👻 QA Ghost running on port {port}")
    print(f"🌐 Open: http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)