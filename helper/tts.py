"""Local CPU text-to-speech, fitted to a time slot.

Most languages: Supertonic 3 (31 languages). Its duration predictor
gives a line's length before synthesis, so the speed is chosen up front.
Chinese: Kokoro-82M v1.1-zh (Supertonic 3 has no Chinese; its language-agnostic
mode produced unintelligible Mandarin in our ASR round-trip test).
"""
from __future__ import annotations

import io
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np

LOG = logging.getLogger("tts")
ROOT = Path(__file__).resolve().parent.parent
MAX_SPEED_EN = 1.3
MAX_SPEED_ZH = 1.45


def trim_silence(wav: np.ndarray, sr: int, thresh=0.01, keep_ms=40) -> np.ndarray:
    idx = np.where(np.abs(wav) > thresh)[0]
    if len(idx) == 0:
        return wav
    keep = int(sr * keep_ms / 1000)
    return wav[max(0, idx[0] - keep):min(len(wav), idx[-1] + keep)]


def wav_bytes(wav: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(wav, -1, 1) * 32767 * 0.95).astype(np.int16).tobytes())
    return buf.getvalue()


class EnglishTTS:
    def __init__(self, voice="M1", steps=6):
        from supertonic_helper import load_text_to_speech, load_voice_style

        d = ROOT / "models" / "supertonic3"
        self.tts = load_text_to_speech(str(d / "onnx"))
        self.style = load_voice_style([str(d / "voice_styles" / f"{voice}.json")])
        self.voice, self.styles, self.dir = voice, {voice: self.style}, d
        self.sr = self.tts.sample_rate
        self.steps = steps
        self.lock = threading.Lock()

    def _style(self, voice: str | None):
        """Another of Supertonic's voices (e.g. F1 for voice-over), loaded on first use."""
        voice = voice or self.voice
        if voice not in self.styles:
            from supertonic_helper import load_voice_style
            self.styles[voice] = load_voice_style([str(self.dir / "voice_styles" / f"{voice}.json")])
        return self.styles[voice]

    def predict_ms(self, text: str, speed=1.0, lang: str = "en", voice: str | None = None) -> float:
        ids, mask = self.tts.text_processor([text], [lang])
        d, *_ = self.tts.dp_ort.run(None, {"text_ids": ids, "style_dp": self._style(voice).dp, "text_mask": mask})
        return float(d[0]) * 1000 / speed

    def synth(self, text: str, max_ms: float | None = None, rate: float = 1.0, lang: str = "en", voice: str | None = None):
        """-> (wav float32, sr, info) or (None, sr, info) when it cannot fit. rate: the viewer's voice speed;
        lang: any of Supertonic 3's languages (supertonic_helper.AVAILABLE_LANGS)."""
        with self.lock:
            base = self.predict_ms(text, lang=lang, voice=voice)
            speed = 1.05 * rate
            if max_ms:  # the prediction counts ~0.35 s of lead-in and tail that trimming removes
                speed = max(speed, max(base - 350, base * 0.6) / max_ms * 1.04)
                if base / MAX_SPEED_EN > max_ms * 1.6:  # hopeless even without the silent lead-in and tail
                    return None, self.sr, {"predicted_ms": round(base), "reason": "too long"}
                speed = min(speed, MAX_SPEED_EN)
            t0 = time.perf_counter()
            wav, _ = self.tts(text, lang, self._style(voice), self.steps, speed)
            wav = trim_silence(wav[0], self.sr)
            el = time.perf_counter() - t0
        dur = len(wav) / self.sr * 1000
        calm = 1.05 * rate
        if max_ms and speed > calm + 0.15 and dur < max_ms * 0.7:  # sped up a lot for nothing: say it again, calmer
            speed2 = max(calm, speed * dur / (max_ms * 0.92))
            with self.lock:
                wav2, _ = self.tts(text, lang, self._style(voice), self.steps, speed2)
                wav2 = trim_silence(wav2[0], self.sr)
            if len(wav2) / self.sr * 1000 <= max_ms:
                wav, speed, dur = wav2, speed2, len(wav2) / self.sr * 1000
        if max_ms and dur > max_ms and speed < MAX_SPEED_EN:  # a little too long: once more at the top speed
            with self.lock:
                wav2, _ = self.tts(text, lang, self._style(voice), self.steps, MAX_SPEED_EN)
                wav2 = trim_silence(wav2[0], self.sr)
            wav, speed, dur = wav2, MAX_SPEED_EN, len(wav2) / self.sr * 1000
        if max_ms and dur > max_ms:  # the prediction counts silence that trimming removes: judge the real length
            return None, self.sr, {"ms": round(dur), "reason": "too long"}
        return wav, self.sr, {"speed": round(speed, 2), "ms": round(dur), "synth_s": round(el, 2)}


class ChineseTTS:
    def __init__(self, voice="zm_010", threads=3):
        import warnings

        warnings.filterwarnings("ignore", category=SyntaxWarning)
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        from misaki import zh

        d = ROOT / "models" / "kokoro"
        self.g2p = zh.ZHG2P(version="1.1")
        # few threads: it shares the CPU with the other voice and the next request
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        sess = ort.InferenceSession(str(d / "kokoro-v1.1-zh.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
        self.k = Kokoro.from_session(sess, str(d / "voices-v1.1-zh.bin"), vocab_config=str(d / "config.json"))
        self.voice = voice
        self.lock = threading.Lock()

    MS_PER_CHAR = 205  # Kokoro zh at speed 1.0, measured (punctuation included)

    def synth(self, text: str, max_ms: float | None = None, rate: float = 1.0, voice: str | None = None):
        """One synthesis: the speed is chosen up front from the character count."""
        with self.lock:
            t0 = time.perf_counter()
            ph, _ = self.g2p(text)
            speed = 1.1 * rate
            if max_ms:
                est = len(text) * self.MS_PER_CHAR / rate
                speed = min(MAX_SPEED_ZH, max(rate, est / max_ms * 1.05 * rate))
            wav, sr = self.k.create(ph, voice=voice or self.voice, speed=speed, is_phonemes=True)
            wav = trim_silence(wav, sr)
            dur = len(wav) / sr * 1000
            el = time.perf_counter() - t0
        return wav, sr, {"speed": round(speed, 2), "ms": round(dur), "synth_s": round(el, 2)}
