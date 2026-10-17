"""Prepare whole videos in advance, with exactly the same pipeline as the deployed service (Gemini timing), straight
into the deployed Firestore + Cloud Storage. Viewers of these videos get descriptions at once.

  python cloud/prepare.py FsHWmOPa-44:en YIgT1jVD3TQ:en,ms          add --fresh to throw away what was prepared before

Needs: `gcloud auth login` (a short-lived token is borrowed from it) and GEMINI_API_KEY in .env."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "helper"))
import worker  # noqa: E402
from store import FirestoreStore, key_of  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
PROJECT = os.environ.get("GCP_PROJECT", "paidpaid")
BUCKET = os.environ.get("BUCKET", "paidpaid-enjoyable-audio")


def token_credentials():
    from google.oauth2.credentials import Credentials
    gcloud = shutil.which("gcloud") or r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    tok = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    return Credentials(tok)


def duration_ms(store, video_id: str) -> int:
    """The video's length (the deployed service gets it from the viewer's player): from any earlier preparation of the
    video in any language, else give it after the languages: VIDEO_ID:en,ms@424 (seconds)."""
    for d in store.fs.collection("videos").where("video_id", "==", video_id).stream():
        if (d.to_dict() or {}).get("duration_ms"):
            return int(d.to_dict()["duration_ms"])
    raise SystemExit(f"{video_id}: length unknown, add @seconds")


def main(specs: list[str]):
    fresh = "--fresh" in specs
    specs = [x for x in specs if not x.startswith("--")]
    worker.AHEAD_MS = 10 ** 9  # the whole video, not just ahead of a viewer
    sys.argv = sys.argv[:1]
    import app  # the same Gemini client and voices as the service  # noqa: E402
    store = FirestoreStore(PROJECT, BUCKET, credentials=token_credentials())
    for spec in specs:
        spec, _, secs = spec.partition("@")
        vid, _, langs = spec.partition(":")
        length = int(float(secs) * 1000) if secs else duration_ms(store, vid)
        for lang in (langs or "en").split(","):
            key = key_of(vid, lang)
            if fresh:
                store.reset(key)
            w = worker.Worker(store, app.gemini, app.voices(), vid, lang, key, length, 0)
            w.start()
            t0 = time.time()
            while w.is_alive():
                w.heartbeat(0)  # keep it from stopping as "nobody watching"
                v = store.get_video(key) or {}
                if v.get("status") == "ready":
                    w.stop_flag = True
                time.sleep(3)
            v = store.get_video(key) or {}
            print(f"{key}: {v.get('status')} timing={v.get('timing')} cues={len(store.list_cues(key))} "
                  f"in {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    threading.current_thread().name = "prepare"
    main(sys.argv[1:])
