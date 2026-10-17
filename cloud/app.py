"""Enjoyable Lite Web: the Cloud Run service (also runs locally for development).

  GET  /api/health            wake-up ping (the web app calls it as soon as the page opens)
  POST /api/watch             {video_id, lang, t, duration}  -> start (or reuse) preparing that video
  POST /api/heartbeat         {key, t, duration}             -> where the viewer is (prepare ahead of them)
  GET  /api/cues?key=&after=  the lines prepared so far (development; deployed, the web app listens to Firestore)
  POST /api/scene             {video_id, t, lang}            -> "what is on screen now": text + voice
  POST /api/say               {text, rate}                   -> a short spoken confirmation in the app's voice
  GET  /api/usage             today's spend and the limit
Locally it also serves the web app (../web) and the voice files (/audio/...).

Environment: GEMINI_BACKEND=vertex (Gemini on Vertex AI, the Cloud Run service account) or an API key in
GEMINI_API_KEY / .env; STORE=firestore|local, GCP_PROJECT, BUCKET, DAILY_LIMIT_USD (default 3).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "helper"))
from store import FirestoreStore, LocalStore, key_of  # noqa: E402
from worker import DESCRIBE_MODEL, LANGS, PRICE, Voices, Worker, generate, mp3  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
LOG = logging.getLogger("app")
VIDEO_ID = re.compile(r"^[\w-]{11}$")
LIMIT_USD = float(os.environ.get("DAILY_LIMIT_USD", "3"))


def gemini_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        from dotenv import dotenv_values
        return dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or ""
    except ImportError:
        return ""


app = FastAPI(title="Enjoyable Lite")
if os.environ.get("STORE", "local") == "firestore":
    store = FirestoreStore(os.environ["GCP_PROJECT"], os.environ["BUCKET"])
else:
    store = LocalStore(ROOT / "scratch" / "web_store")
from google import genai  # noqa: E402

if os.environ.get("GEMINI_BACKEND") == "vertex":
    gemini = genai.Client(vertexai=True, project=os.environ["GCP_PROJECT"],
                          location=os.environ.get("VERTEX_LOCATION", "global"))
else:
    gemini = genai.Client(api_key=gemini_key(), http_options={"timeout": 60_000})  # never wait forever on one call
_voices: Voices | None = None
_voices_lock = threading.Lock()
workers: dict[str, Worker] = {}
workers_lock = threading.Lock()


def voices() -> Voices:
    global _voices
    with _voices_lock:
        if _voices is None:
            _voices = Voices()
        return _voices


@app.on_event("startup")
def warm():
    threading.Thread(target=voices, daemon=True).start()  # load the voices while the first request is coming


@app.get("/api/health")
def health():
    return {"ok": True, "voices": _voices is not None}


@app.post("/api/watch")
async def watch(request: Request):
    body = await request.json()
    vid, lang = str(body.get("video_id", "")), str(body.get("lang", "en"))
    if not VIDEO_ID.match(vid):
        raise HTTPException(400, "not a YouTube video id")
    lang = lang if lang in LANGS else "en"
    key = key_of(vid, lang)
    t, dur = int(float(body.get("t", 0)) * 1000), int(float(body.get("duration", 0)) * 1000)
    with workers_lock:
        w = workers.get(key)
        if w is None or not w.is_alive():
            w = Worker(store, gemini, voices(), vid, lang, key, dur, LIMIT_USD)
            w.heartbeat(t, dur)
            workers[key] = w
            w.start()
        else:
            w.heartbeat(t, dur)
    v = store.get_video(key) or {}
    return {"key": key, "status": v.get("status", "preparing"), "title": v.get("title", "")}


@app.post("/api/heartbeat")
async def heartbeat(request: Request):
    body = await request.json()
    key = str(body.get("key", ""))
    w = workers.get(key)
    t, dur = int(float(body.get("t", 0)) * 1000), int(float(body.get("duration", 0)) * 1000)
    if w and w.is_alive():
        w.heartbeat(t, dur)
        return {"ok": True}
    vid, _, lang = key.rpartition("_")
    if VIDEO_ID.match(vid) and lang in LANGS:  # the worker stopped (idle, or a new instance): start it again
        with workers_lock:
            w = Worker(store, gemini, voices(), vid, lang, key, dur, LIMIT_USD)
            w.heartbeat(t, dur)
            workers[key] = w
            w.start()
    return {"ok": True}


@app.get("/api/cues")
def cues(key: str, after: int = -1):
    v = store.get_video(key) or {}
    return {"video": v, "cues": store.list_cues(key, after)}


SCENE = """You are a professional audio describer. A blind viewer has paused the video and asks "what is on screen right now?".
Describe the frame at the very END of this clip, in 2 or 3 short sentences: first who is there and what they are doing at
this exact moment, then where we are, then any legible text (read it). Use a person's name only as the descriptions so far
have used it; otherwise describe them (the boy in round glasses). Present tense, decisive verbs, plain words. Never use
camera words (shot, camera, frame, close-up), never say what anyone thinks, never add a detail you cannot see."""


@app.post("/api/scene")
async def scene(request: Request):
    body = await request.json()
    vid, lang = str(body.get("video_id", "")), str(body.get("lang", "en"))
    if not VIDEO_ID.match(vid):
        raise HTTPException(400, "not a YouTube video id")
    t = max(0.0, float(body.get("t", 0)))
    lang = lang if lang in LANGS else "en"
    key = key_of(vid, lang)

    def run():
        v = store.get_video(key) or {}
        said = [c["text"] for c in store.list_cues(key) if c.get("kind", "ad") == "ad" and c["start_ms"] < t * 1000][-8:]
        ctx = ""
        if v.get("people"):
            ctx += "Named so far: " + "; ".join(f"{p['name']} ({p['who']})" for p in v["people"]) + "\n"
        if v.get("story"):
            ctx += f"The story so far: {v['story']}\n"
        if said:
            ctx += "The descriptions so far: " + " / ".join(said) + "\n"
        r = generate(gemini, model=DESCRIBE_MODEL, contents=[{"role": "user", "parts": [
            {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={vid}", "mime_type": "video/mp4"},
             "video_metadata": {"start_offset": f"{max(0, t - 3):.1f}s", "end_offset": f"{t + 0.3:.1f}s", "fps": 2}},
            {"text": f"{ctx}Describe the moment at the END of this clip, in {LANGS[lang]}."}]}],
            config={"system_instruction": SCENE, "thinking_config": {"thinking_level": "low"}})
        u = r.usage_metadata
        p = PRICE.get(DESCRIBE_MODEL, (0.75, 3.75))
        store.add_usage(((u.prompt_token_count or 0) * p[0] +
                         ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) * p[1]) / 1e6)
        text = (r.text or "").strip()
        audio, _ = voices().speak(text, None, lang)
        name = "scene-" + hashlib.sha1(f"{vid}{t}{text}".encode()).hexdigest()[:12] + ".mp3"
        return text, store.put_audio(key, name, audio)

    import asyncio
    text, url = await asyncio.to_thread(run)
    return {"text": text, "audio_url": url}


SAY_CACHE: dict = {}


@app.post("/api/say")
async def say(request: Request):
    body = await request.json()
    text = str(body.get("text", ""))[:200]
    rate = max(0.7, min(1.5, float(body.get("rate", 1.0))))
    if not text:
        raise HTTPException(400, "no text")
    k = (text, round(rate, 1))
    if k not in SAY_CACHE:
        import asyncio

        def run():
            from tts import wav_bytes
            v = voices()
            wav, sr, _ = v.st.synth(text, rate=rate * 1.05)
            name = "say-" + hashlib.sha1(repr(k).encode()).hexdigest()[:12] + ".mp3"
            return store.put_audio("app", name, mp3(wav_bytes(wav, sr)))
        SAY_CACHE[k] = await asyncio.to_thread(run)
    return {"audio_url": SAY_CACHE[k]}


@app.get("/api/usage")
def usage():
    return {"usd_today": round(store.usage_today(), 4), "limit_usd": LIMIT_USD}


# --- development: the web app and the voice files from this same server -------------------------
if isinstance(store, LocalStore):
    @app.get("/audio/{key}/{name}")
    def audio(key: str, name: str):
        p = store.root / "audio" / Path(key).name / Path(name).name
        if not p.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(p, media_type="audio/mpeg")

    web = ROOT / "web"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
