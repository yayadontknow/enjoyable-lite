"""Where prepared descriptions live.

LocalStore   (development): memory + JSON files, voice files served by the app at /audio/...
FirestoreStore (Cloud Run): Firestore documents (the web app listens to them live) + Cloud Storage voice files.

A video in one language is one document  videos/{video_id}_{lang}
  {video_id, lang, title, duration_ms, status, error, prepared: [[a_ms, b_ms], ...], viewer_t_ms, last_seen, updated}
and its lines are the subcollection        videos/{key}/cues/{start_ms}
  {start_ms, end_ms, text, audio_url, kind}
Daily spend:                               usage/{yyyy-mm-dd}  {usd}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


def cue_id(cue: dict) -> str:
    """'61600' for a description, '61600-dub' for a voice-over line."""
    kind = cue.get("kind", "ad")
    return str(int(cue["start_ms"])) + ("" if kind == "ad" else f"-{kind}")


def key_of(video_id: str, lang: str) -> str:
    return f"{video_id}_{lang}"


class LocalStore:
    def __init__(self, root: Path):
        self.root = root
        (root / "audio").mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.videos: dict[str, dict] = {}
        self.cues: dict[str, dict[int, dict]] = {}
        for p in root.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                self.videos[p.stem] = d["video"]
                self.cues[p.stem] = {int(c["start_ms"]): c for c in d["cues"]}
            except Exception:  # noqa: BLE001
                pass

    def _save(self, key: str):
        (self.root / f"{key}.json").write_text(json.dumps(
            {"video": self.videos.get(key, {}), "cues": sorted(self.cues.get(key, {}).values(), key=lambda c: c["start_ms"])},
            ensure_ascii=False), encoding="utf-8")

    def get_video(self, key: str) -> dict | None:
        with self.lock:
            v = self.videos.get(key)
            return dict(v) if v else None

    def update_video(self, key: str, **fields):
        with self.lock:
            self.videos.setdefault(key, {}).update(fields, updated=time.time())
            self._save(key)

    def add_cue(self, key: str, cue: dict):
        with self.lock:
            self.cues.setdefault(key, {})[cue_id(cue)] = cue
            self._save(key)

    def list_cues(self, key: str, after_ms: int = -1) -> list[dict]:
        with self.lock:
            return sorted((c for c in self.cues.get(key, {}).values() if c["start_ms"] > after_ms), key=lambda c: c["start_ms"])

    def reset(self, key: str):
        """Forget a video's lines and progress (to prepare it again)."""
        with self.lock:
            self.cues.pop(key, None)
            self.videos.pop(key, None)
            self._save(key)

    def put_audio(self, key: str, name: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        d = self.root / "audio" / key
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        return f"/audio/{key}/{name}"

    def add_usage(self, usd: float):
        day = time.strftime("%Y-%m-%d")
        p = self.root / "usage.json"
        with self.lock:
            u = json.loads(p.read_text()) if p.exists() else {}
            u[day] = round(u.get(day, 0.0) + usd, 6)
            p.write_text(json.dumps(u))

    def usage_today(self) -> float:
        p = self.root / "usage.json"
        try:
            return json.loads(p.read_text()).get(time.strftime("%Y-%m-%d"), 0.0)
        except Exception:  # noqa: BLE001
            return 0.0


class FirestoreStore:
    """Firestore + Cloud Storage (Cloud Run's own service account; no keys in the code)."""

    def __init__(self, project: str, bucket: str, credentials=None):
        from google.cloud import firestore, storage

        self.fs = firestore.Client(project=project, credentials=credentials)
        self.bucket = storage.Client(project=project, credentials=credentials).bucket(bucket)
        self.bucket_name = bucket
        self.Increment = firestore.Increment

    def _doc(self, key: str):
        return self.fs.collection("videos").document(key)

    def get_video(self, key: str) -> dict | None:
        snap = self._doc(key).get()
        if not snap.exists:
            return None
        v = snap.to_dict()
        v["prepared"] = [[p["a"], p["b"]] for p in v.get("prepared", [])]  # Firestore: no nested arrays
        return v

    def update_video(self, key: str, **fields):
        if "prepared" in fields:
            fields["prepared"] = [{"a": a, "b": b} for a, b in fields["prepared"]]
        self._doc(key).set(dict(fields, updated=time.time()), merge=True)

    def add_cue(self, key: str, cue: dict):
        self._doc(key).collection("cues").document(cue_id(cue)).set(cue)

    def list_cues(self, key: str, after_ms: int = -1) -> list[dict]:
        q = self._doc(key).collection("cues").where("start_ms", ">", after_ms).order_by("start_ms")
        return [d.to_dict() for d in q.stream()]

    def reset(self, key: str):
        """Forget a video's lines and progress (to prepare it again)."""
        for d in self._doc(key).collection("cues").stream():
            d.reference.delete()
        self._doc(key).delete()

    def put_audio(self, key: str, name: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        blob = self.bucket.blob(f"audio/{key}/{name}")
        blob.cache_control = "public, max-age=31536000, immutable"
        blob.upload_from_string(data, content_type=content_type)
        return f"https://storage.googleapis.com/{self.bucket_name}/audio/{key}/{name}"  # the bucket is public-read

    def add_usage(self, usd: float):
        self.fs.collection("usage").document(time.strftime("%Y-%m-%d")).set({"usd": self.Increment(usd)}, merge=True)

    def usage_today(self) -> float:
        snap = self.fs.collection("usage").document(time.strftime("%Y-%m-%d")).get()
        return (snap.to_dict() or {}).get("usd", 0.0) if snap.exists else 0.0
