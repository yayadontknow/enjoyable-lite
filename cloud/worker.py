"""Prepares one YouTube video in one language, a chunk at a time, just ahead of whoever is watching.

  Gemini 3.8 Flash listens to the stretch (short pieces in parallel): when people speak, what they say, in which
  language, and a translation for the voice-over  ->  code turns the silences into gaps with word budgets
  ->  Gemini 3.8 Flash watches the stretch and writes one cue per gap (the prompt below)
  ->  code checks length and camera words, one repair turn if needed  ->  Supertonic / Kokoro voice each line,
  fitted to its gap  ->  voice file + line published to the store (Firestore: the web app receives it live).

Timing comes from Gemini only: YouTube refuses audio downloads to Cloud Run, so the deployed service has nothing else,
and videos prepared in advance (cloud/prepare.py) use the same method, so the demos sound like any new video.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("worker")

CHUNKS_MS = (15_000, 30_000, 45_000)  # growing chunks: each is ready before the viewer reaches it, and the lead grows
CHUNK_MS = 60_000
FIRST_CHUNK_MS = CHUNKS_MS[0]
AHEAD_MS = 150_000  # prepare this far ahead of the viewer, no further (cost)
IDLE_STOP_S = 90  # no heartbeat for this long: stop
LOOK_MS = 3_000  # listen and watch a little past a chunk's end (a gap may run over it; "after" needs it)
SLOT_MS, MIN_GAP_MS = 10_000, 1_200  # long silences are split into gaps of at most 10 s
DESCRIBE_MODEL = os.environ.get("ENJOYABLE_DESCRIBE_MODEL", "gemini-3.8-flash")
TIMING_MODEL = os.environ.get("ENJOYABLE_TIMING_MODEL", "gemini-3.8-flash")
THINKING = os.environ.get("ENJOYABLE_THINKING", "low")  # production writes with "medium"; live needs speed
# USD per 1M tokens (in, out incl. thinking). Gemini 3.8 Flash: introductory price through 2026-12-31,
# then 1.50 / 7.50 from 2027-01-01 (ai.google.dev/gemini-api/docs/pricing).
PRICE = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}
LANGS = {c: n for c, n, *_ in json.loads((ROOT / "app" / "src" / "languages.json").read_text(encoding="utf-8"))}
SUPERTONIC_LANG = {"ms": "id"}
CHAR_LANGS = {"zh", "ja", "th"}
RATE = {"en": 2.8, "zh": 4.5, "ja": 5.0, "th": 6.0, "ms": 2.1, "id": 2.1}  # words (or characters) a second, at most
# camera/editing language, from the production checker (a security camera or CCTV footage is an object, not editing)
NOT_CCTV = r"(?<!security )(?<!cctv )(?<!surveillance )"
CAMERA = re.compile(r"\b(shot|" + NOT_CCTV + r"camera|(?:in|into|out of|fills the) frame|frames? shows?|close-up|"
                    r"closeup|cuts? to|view of|we see|zooms? (?:in|out)|establishing|view shows|"
                    r"pans? (?:across|over|to|up|down|left|right)|panning|(?:low|high|wide|camera) angle|"
                    + NOT_CCTV + r"footage|scene shifts?)\b", re.I)


def generate(client, **kw):
    """Gemini, retried on its temporary errors (overloaded, internal, rate limit)."""
    for i in range(3):
        try:
            return client.models.generate_content(**kw)
        except Exception as e:  # noqa: BLE001
            timed_out = "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()
            if i == 2 or not (timed_out or getattr(e, "code", None) in (429, 500, 502, 503, 504)):
                raise
            LOG.warning("Gemini %s, trying again", getattr(e, "code", "?"))
            time.sleep(2 + 3 * i)


def video_part(video_id: str, a: int, b: int, fps: float = 1) -> dict:
    return {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}", "mime_type": "video/mp4"},
            "video_metadata": {"start_offset": f"{a / 1000:.1f}s", "end_offset": f"{b / 1000:.1f}s", "fps": fps}}


def youtube_title(video_id: str) -> str:
    try:
        q = urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        with urllib.request.urlopen(f"https://www.youtube.com/oembed?url={q}&format=json", timeout=6) as r:
            return json.loads(r.read()).get("title", "")
    except Exception:  # noqa: BLE001
        return ""



# --- the description prompt ------------------------------------------------------------------------------------
# A short, simple prompt for this public repository; the production prompt is not published.
SYSTEM = ("You write audio description for blind viewers: short, plain, present-tense lines about what can be seen, "
          "spoken in the silences between the dialogue. Describe only what is visible.")

TEMPLATE = """This clip is {span} of the YouTube video {title}.
{context}
The {n_gaps} silences to fill (id, start - end in seconds, target and maximum in {unit}):
{gap_table}

What is said in this clip:
{transcript}

Write one description for each silence, in {language}, within its maximum. Also return the story so far in a few
sentences, and the people named so far with a short note on who each one is."""


def schema(n: int) -> dict:
    people = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "name": {"type": "STRING"}, "who": {"type": "STRING"}}, "required": ["name", "who"]}}
    return {"type": "OBJECT", "properties": {
        "cues": {"type": "ARRAY", "minItems": n, "maxItems": n, "items": {"type": "OBJECT", "properties": {
            "id": {"type": "INTEGER"}, "cue": {"type": "STRING"}}, "required": ["id", "cue"]}},
        "story": {"type": "STRING"}, "people": people}, "required": ["cues", "story", "people"]}


REPAIR = """Rewrite these descriptions so each one fits its maximum and uses no camera words. Write in {language}.
{rows}"""

REPAIR_SCHEMA = {"type": "OBJECT", "properties": {"cues": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "INTEGER"}, "cue": {"type": "STRING"}}, "required": ["id", "cue"]}}}, "required": ["cues"]}

class EndOfVideo(Exception):
    pass


class Voices:
    """Local voices, loaded once per container: Supertonic 3 (31 languages), Kokoro for Chinese (on first use)."""

    def __init__(self):
        import sys
        sys.path.insert(0, str(ROOT / "helper"))
        from tts import EnglishTTS
        self.st = EnglishTTS()
        self.zh = None
        self.lock = threading.Lock()

    DUB_VOICE = {"st": "F1", "zh": "zf_001"}  # voice-over: a second voice, so it is not mistaken for a description

    def speak(self, text: str, max_ms: float | None, lang: str, rate: float = 1.0, voice: str | None = None):
        from tts import ChineseTTS, wav_bytes
        if lang == "zh":
            with self.lock:
                if self.zh is None:
                    self.zh = ChineseTTS()
            wav, sr, info = self.zh.synth(text, max_ms=max_ms, rate=rate,
                                          voice=self.DUB_VOICE["zh"] if voice == "dub" else None)
            if max_ms and info["ms"] > max_ms + 150:
                return None, info
        else:
            wav, sr, info = self.st.synth(text, max_ms=max_ms, rate=rate, lang=SUPERTONIC_LANG.get(lang, lang),
                                          voice=self.DUB_VOICE["st"] if voice == "dub" else None)
            if wav is None:
                return None, info
        return mp3(wav_bytes(wav, sr)), info


def _plain(t: str) -> str:
    """A line without case, spaces or punctuation, to tell a real translation from a copy of the original."""
    return re.sub(r"[\W_]+", "", t or "").lower()


def mp3(wav: bytes) -> bytes:
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1",
                        "-b:a", "64k", "-f", "mp3", "pipe:1"], input=wav, capture_output=True, timeout=30)
    if p.returncode:
        raise RuntimeError("ffmpeg mp3: " + p.stderr.decode(errors="replace")[-200:])
    return p.stdout


def words_for(gap_ms: int, lang: str) -> int:
    """The hard maximum for a gap: what the voice can say in it at its quickest comfortable speed."""
    n = int((gap_ms - 300) / 1000 * RATE.get(lang, 2.2))
    return max(4, min(60, n)) if lang in CHAR_LANGS else max(2, min(30, n))


def count(text: str, lang: str) -> int:
    return len(re.sub(r"\s", "", text)) if lang in CHAR_LANGS else len(text.split())


def mmss(ms: float) -> str:
    return f"{ms / 1000:.1f}"


class Worker(threading.Thread):
    def __init__(self, store, gemini, voices, video_id: str, lang: str, key: str, duration_ms: int, budget_usd: float):
        super().__init__(daemon=True, name=f"prep-{key}")
        self.store, self.gemini, self.voices = store, gemini, voices
        self.video_id, self.lang, self.key = video_id, lang, key
        self.duration_ms = duration_ms
        self.budget_usd = budget_usd
        self.viewer_t = 0
        self.last_seen = time.time()
        self.title = ""
        self.story = ""
        self.people: list[dict] = []
        self.recent: list[tuple[int, str]] = []  # (start_ms, cue) written so far, for continuity
        self.stop_flag = False
        self.covered_to = 0  # descriptions already planned up to here (a gap can run past a chunk's end)
        self.spent = 0.0  # USD this worker has spent

    # the web app reports where the viewer is (every ~15 s, and on seeks)
    def heartbeat(self, t_ms: int, duration_ms: int = 0):
        self.viewer_t = int(t_ms)
        self.last_seen = time.time()
        if duration_ms:
            self.duration_ms = int(duration_ms)

    def _spent(self, r, model: str, kind: str = ""):
        u = r.usage_metadata
        p = PRICE.get(model, PRICE["gemini-3.8-flash"])
        n_in, n_out, n_think = u.prompt_token_count or 0, u.candidates_token_count or 0, u.thoughts_token_count or 0
        usd = (n_in * p[0] + (n_out + n_think) * p[1]) / 1e6
        self.spent += usd
        LOG.info("%s: %s %s in=%d out=%d think=%d US$%.5f", self.key, kind, model, n_in, n_out, n_think, usd)
        self.store.add_usage(usd)

    # -- 1. Gemini listens: speech times, words, language, voice-over translation ----------
    def _speech(self, a: int, b: int) -> list[dict]:
        lang_name = LANGS.get(self.lang, "English")
        wps = RATE.get(self.lang, 2.2)
        unit = "characters" if self.lang in CHAR_LANGS else "words"

        def piece(c0: int, c1: int):
            d = (c1 - c0) / 1000
            r = generate(self.gemini, model=TIMING_MODEL, contents=[{"role": "user", "parts": [
                video_part(self.video_id, c0, c1),
                {"text": f"Listen to the audio of this {d:.0f}-second clip. List every stretch of human speech (one item per "
                         f"sentence or speaker turn; leave out singing) with start and end in seconds from the start of this "
                         f"clip (0 to {d:.0f}), one decimal; 'text': what is said, in its own language; 'language': its "
                         f"ISO 639-1 code; 'translation': a natural {lang_name} line for dubbing, about {wps:g} {unit} per "
                         f"second of speech so it fits the same time, or an empty string when the speech is already in "
                         f"{lang_name}."}]}],
                config={"response_mime_type": "application/json", "temperature": 0.0,
                        "thinking_config": {"thinking_level": "low"}, "response_schema": {
                            "type": "OBJECT", "properties": {"speech": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                                "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "text": {"type": "STRING"},
                                "language": {"type": "STRING"}, "translation": {"type": "STRING"}},
                                "required": ["start", "end", "text", "language", "translation"]}}}, "required": ["speech"]}})
            self._spent(r, TIMING_MODEL, "listen")
            if (r.usage_metadata.prompt_token_count or 0) < 750:  # only our words, no video: past the end of the video
                return None
            out = []
            for x in json.loads(r.text)["speech"]:
                s0, e0 = c0 + int(float(x["start"]) * 1000), c0 + int(float(x["end"]) * 1000)
                if c0 <= s0 < e0 <= c1 + 1000:
                    out.append({"start": s0, "end": e0, "text": str(x.get("text", "")).strip(),
                                "lang": str(x.get("language", "")).lower()[:2],
                                "translation": str(x.get("translation", "")).strip()})
            return out

        n = max(1, -(-(b - a) // 16_000))  # even pieces of about 15 s (no tiny last piece: each call has a fixed cost)
        step = (b - a) / n
        clips = [(int(a + i * step), int(a + (i + 1) * step)) for i in range(n)]
        with ThreadPoolExecutor(len(clips)) as ex:
            parts = list(ex.map(lambda c: piece(*c), clips))
        if parts[0] is None:  # the video ends before this chunk: learn its length, prepare nothing more
            self.duration_ms = a
            raise EndOfVideo()
        return sorted((x for part in parts if part for x in part), key=lambda x: x["start"])

    # -- 2. code: the gaps ---------------------------------------------------------------
    def _gaps(self, a: int, b: int, look: int, speech: list[dict]) -> list[tuple[int, int]]:
        """Silences of 2.2 s or more between Gemini's speech times, 0.6 s safety on each side (its times can be off by a
        second), split into gaps of at most SLOT_MS. Gaps start before the chunk's end; they may run into the look-ahead."""
        windows, cur = [], a
        for s0, e0 in sorted((d["start"], d["end"]) for d in speech) + [(look, look)]:
            if s0 - cur >= 2200:
                windows.append((cur + 600, s0 - 600 if s0 < look else look))
            cur = max(cur, e0)
        windows = [(max(x, self.covered_to), y) for x, y in windows if x < b and y > self.covered_to]
        gaps = []
        for x, y in windows:
            n = max(1, round((y - x) / SLOT_MS + 0.2))
            st = (y - x) / n
            gaps += [(int(x + i * st), int(x + (i + 1) * st)) for i in range(n)]
        return [g for g in gaps if g[1] - g[0] >= MIN_GAP_MS]

    # -- 3. Gemini writes the cues ------------------------------------------------------------
    def _describe(self, a: int, look: int, gaps: list[tuple[int, int]], speech: list[dict]) -> dict[int, str]:
        cs = max(0, a - LOOK_MS)  # the clip Gemini watches: a little before for context, a little after for "after"
        ce = look
        lang_name = LANGS.get(self.lang, "English")
        unit = "characters" if self.lang in CHAR_LANGS else "words"
        rows = []
        for i, (x, y) in enumerate(gaps):
            mx = words_for(y - x, self.lang)
            lo = -(-mx * 85 // 100) if mx >= 6 else 1
            until = gaps[i + 1][0] if i + 1 < len(gaps) else ce
            rows.append(f"{i + 1} | {mmss(x - cs)} - {mmss(y - cs)} | {(y - x) / 1000:.1f} s | target {lo}-{mx} {unit} | "
                        f"max {mx} | covers until {mmss(until - cs)}")
        transcript = "\n".join(f"[{mmss(d['start'] - cs)}] {d['text']}" for d in speech if d["text"]) or "(no speech heard)"
        context = ""
        if self.people:
            context += "\n## Who is who\nNamed so far (keep each name on this person):\n" + \
                       "\n".join(f"- {p['name']}: {p['who']}" for p in self.people) + "\n"
        if self.story:
            context += f"\n## The story so far\n{self.story}\n"
        if self.recent:
            context += "\n## Cues already written for earlier gaps (continue this narration)\n" + \
                       "\n".join(f"- {t}" for _, t in self.recent[-20:]) + "\n"
        clock = lambda ms: f"{ms // 60000}:{ms // 1000 % 60:02d}"  # noqa: E731
        span = f"{clock(cs)} to {clock(ce)}" if cs else f"the first {ce // 1000} seconds"
        ask = TEMPLATE.format(
            span=span, title=f'"{self.title}"' if self.title else "being watched", n_gaps=len(gaps), context=context,
            unit=unit, gap_table="\n".join(rows), transcript=transcript, language=lang_name,
            tiny="4 characters" if self.lang in CHAR_LANGS else "2 words")
        r = generate(self.gemini, model=DESCRIBE_MODEL, contents=[{"role": "user", "parts": [
            video_part(self.video_id, cs, ce), {"text": ask}]}],
            config={"system_instruction": SYSTEM, "response_mime_type": "application/json",
                    "response_schema": schema(len(gaps)), "thinking_config": {"thinking_level": THINKING}})
        self._spent(r, DESCRIBE_MODEL, "write")
        out = json.loads(r.text)
        self.story = (out.get("story") or self.story)[:800]
        self.people = (out.get("people") or self.people)[:40]
        cues = {}
        for i, c in enumerate(out.get("cues", [])[:len(gaps)]):
            cues[i] = str(c.get("cue", "")).strip().strip('"')
        return self._check(cues, gaps)

    def _check(self, cues: dict[int, str], gaps) -> dict[int, str]:
        """The production writer's hard rules, checked in code: within the gap's maximum, no camera language.
        One text-only repair turn for the cues that break them; whatever still breaks them is dropped."""
        def problem(i, t):
            mx = words_for(gaps[i][1] - gaps[i][0], self.lang)
            if count(t, self.lang) > mx + 1:
                return f"too long: {count(t, self.lang)} for a maximum of {mx}", mx
            if self.lang == "en" and CAMERA.search(t):
                return "camera or editing language", mx
            return None, mx

        bad = {i: problem(i, t) for i, t in cues.items() if t and problem(i, t)[0]}
        if not bad:
            return cues
        lang_name = LANGS.get(self.lang, "English")
        rows = "\n".join(f'- id {i + 1}: "{cues[i]}" | {why} | max {mx}' for i, (why, mx) in bad.items())
        try:
            r = generate(self.gemini, model=DESCRIBE_MODEL, contents=REPAIR.format(rows=rows, language=lang_name),
                         config={"system_instruction": SYSTEM, "response_mime_type": "application/json",
                                 "response_schema": REPAIR_SCHEMA, "thinking_config": {"thinking_level": "low"}})
            self._spent(r, DESCRIBE_MODEL, "repair")
            for c in json.loads(r.text).get("cues", []):
                i = int(c.get("id", 0)) - 1
                if i in bad:
                    cues[i] = str(c.get("cue", "")).strip()
        except Exception as e:  # noqa: BLE001
            LOG.warning("repair failed: %s", str(e)[:120])
        for i in bad:
            if cues.get(i) and problem(i, cues[i])[0]:
                LOG.info("%s: dropped cue %d (%s)", self.key, i + 1, problem(i, cues[i])[0])
                cues[i] = ""
        return cues

    # -- one chunk ----------------------------------------------------------
    def _chunk(self, a: int, b: int):
        end = self.duration_ms or b + LOOK_MS
        look = min(end, b + LOOK_MS)
        before = [c for c in self.store.list_cues(self.key, a - 20_000) if c.get("kind", "ad") == "ad" and c.get("gap")]
        self.covered_to = max([self.covered_to] + [int(c["gap"][1]) for c in before if c["gap"][0] < a])
        speech = self._speech(a, look)
        gaps = self._gaps(a, b, look, speech)
        with ThreadPoolExecutor(1) as ex:  # voice the dubbed lines while Gemini writes the descriptions
            dubbing = ex.submit(self._dub, [d for d in speech if d["start"] < b])
            cues = self._describe(a, look, gaps, speech) if gaps else {}
            n = dubbing.result()
        if not gaps:
            return n
        for i, (x, y) in enumerate(gaps):
            text = cues.get(i, "")
            if not text:
                continue
            audio, info = self.voices.speak(text, y - x - 150, self.lang)
            if audio is None:
                LOG.info("%s: cue too long for its voice, skipped: %s", self.key, text[:60])
                continue
            ms = info.get("ms", y - x)
            url = self.store.put_audio(self.key, f"{x}.mp3", audio)
            self.store.add_cue(self.key, {"start_ms": x + 50, "end_ms": x + 50 + int(ms), "text": text,
                                          "audio_url": url, "kind": "ad", "gap": [x, y]})
            self.recent.append((x, text))
            self.covered_to = max(self.covered_to, y)
            n += 1
        return n

    def _dub(self, speech: list[dict]) -> int:
        """Voice-over: speech in another language, spoken again in the viewer's language (a second voice)."""
        n = 0
        for i, d in enumerate(speech):
            text = d["translation"]
            if not text or d["lang"] == self.lang:
                continue
            if _plain(text) == _plain(d.get("text", "")):  # mislabelled: the line is already in the viewer's language
                continue
            nxt = speech[i + 1]["start"] if i + 1 < len(speech) else d["end"] + 1500
            room = max(d["end"] - d["start"] + 300, min(nxt - d["start"] - 100, d["end"] - d["start"] + 1500))
            audio, info = self.voices.speak(text, room, self.lang, voice="dub")
            if audio is None:
                continue
            url = self.store.put_audio(self.key, f"dub-{d['start']}.mp3", audio)
            self.store.add_cue(self.key, {"start_ms": d["start"], "end_ms": d["start"] + int(info.get("ms", room)),
                                          "text": text, "audio_url": url, "kind": "dub", "gap": [d["start"], d["end"]]})
            n += 1
        if n:
            self.store.update_video(self.key, dubbed=True)
        return n

    def _prepared(self) -> list[list[int]]:
        v = self.store.get_video(self.key) or {}
        return [list(p) for p in v.get("prepared", [])]

    def run(self):
        try:
            self.title = youtube_title(self.video_id)
            self.store.update_video(self.key, video_id=self.video_id, lang=self.lang, status="preparing", error="",
                                    title=self.title, timing="gemini", duration_ms=self.duration_ms)
            n_done = 0
            while not self.stop_flag:
                if time.time() - self.last_seen > IDLE_STOP_S:
                    LOG.info("%s: nobody watching, stopping", self.key)
                    break
                if self.budget_usd and self.store.usage_today() >= self.budget_usd:
                    self.store.update_video(self.key, status="paused", error="Today's limit is reached.")
                    break
                prepared = self._prepared()
                a = self.viewer_t
                for x, y in sorted(prepared):  # skip what is already prepared (from earlier viewers too)
                    if x <= a < y:
                        a = y
                end = self.duration_ms or 10 ** 9
                if a >= end - 500:
                    if all(any(x <= t < y for x, y in prepared) for t in range(0, max(1, end - 1000), 5000)):
                        self.store.update_video(self.key, status="ready")
                    time.sleep(2)
                    continue
                if a - self.viewer_t > AHEAD_MS:
                    time.sleep(1.5)
                    continue
                b = min(end, a + (CHUNKS_MS[n_done] if n_done < len(CHUNKS_MS) else CHUNK_MS))
                t0 = time.time()
                try:
                    n = self._chunk(a, b)
                except EndOfVideo:
                    LOG.info("%s: the video ends at %d s", self.key, a // 1000)
                    self.store.update_video(self.key, duration_ms=a)
                    continue
                n_done += 1
                prepared.append([a, b])
                merged = []
                for x, y in sorted(prepared):
                    if merged and x <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], y)
                    else:
                        merged.append([x, y])
                self.store.update_video(self.key, prepared=merged, status="preparing", story=self.story, people=self.people)
                LOG.info("%s: %d-%d s, %d lines in %.1f s", self.key, a // 1000, b // 1000, n, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            LOG.exception("worker failed")
            self.store.update_video(self.key, status="error", error=str(e)[:200])
