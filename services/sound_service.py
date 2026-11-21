"""轻量级纯内存 PCM WAV 合成音效服务（无需外部音频素材）。"""

from __future__ import annotations

from enum import Enum
import io
import math
import os
import struct
import threading
import time
import wave
import winsound

from models.app_version import get_user_config_dir

_SAMPLE_RATE = 22050


class PetSoundEffect(str, Enum):
    CHIME = "chime"
    PURR = "purr"
    SPARKLE = "sparkle"
    BOING = "boing"
    ANGRY = "angry"


def _clamp16(val: float) -> int:
    scaled = int(max(-1.0, min(1.0, val)) * 32767)
    return max(-32768, min(32767, scaled))


def _encode_wav(samples: list[int], sample_rate: int = _SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def _synthesize_notes(notes: list[tuple[float, float, float]]) -> bytes:
    samples: list[int] = []
    for freq, dur, amp in notes:
        count = int(_SAMPLE_RATE * dur)
        for i in range(count):
            t = i / _SAMPLE_RATE
            env = math.sin(math.pi * i / count)
            wave_val = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
            samples.append(_clamp16(wave_val * env * amp))
    return _encode_wav(samples)


def _synthesize_purr() -> bytes:
    dur = 0.22
    count = int(_SAMPLE_RATE * dur)
    samples: list[int] = []
    for i in range(count):
        t = i / _SAMPLE_RATE
        env = math.sin(math.pi * i / count)
        mod = 0.55 + 0.45 * math.sin(2 * math.pi * 26 * t)
        tone = math.sin(2 * math.pi * 185 * t) * 0.6 + math.sin(2 * math.pi * 370 * t) * 0.3
        samples.append(_clamp16(tone * mod * env * 0.16))
    return _encode_wav(samples)


def _synthesize_sweep(start_freq: float, end_freq: float, dur: float, amp: float) -> bytes:
    count = int(_SAMPLE_RATE * dur)
    samples: list[int] = []
    phase = 0.0
    for i in range(count):
        progress = i / count
        freq = start_freq + (end_freq - start_freq) * progress
        phase += 2 * math.pi * freq / _SAMPLE_RATE
        env = math.sin(math.pi * progress)
        samples.append(_clamp16(math.sin(phase) * env * amp))
    return _encode_wav(samples)


class SoundService:
    """合成并用 winsound 异步播放猫娘互动音效，自动防抖避免重叠爆音。"""

    def __init__(self) -> None:
        self.enabled: bool = True
        self._disposed = False
        self._last_play_monotonic = 0.0
        self._lock = threading.Lock()
        self._wav_bytes: dict[PetSoundEffect, bytes] = {
            PetSoundEffect.CHIME: _synthesize_notes([(784, 0.06, 0.16), (1046.5, 0.09, 0.14)]),
            PetSoundEffect.PURR: _synthesize_purr(),
            PetSoundEffect.SPARKLE: _synthesize_notes(
                [(880, 0.045, 0.12), (1174.6, 0.05, 0.12), (1567.9, 0.09, 0.13)]
            ),
            PetSoundEffect.BOING: _synthesize_sweep(340, 620, 0.11, 0.16),
            PetSoundEffect.ANGRY: _synthesize_sweep(480, 220, 0.12, 0.18),
        }
        self._wav_files: dict[PetSoundEffect, str] = {}
        try:
            cache_dir = os.path.join(str(get_user_config_dir()), "sounds")
            os.makedirs(cache_dir, exist_ok=True)
            for eff, raw in self._wav_bytes.items():
                fp_path = os.path.join(cache_dir, f"{eff.value}.wav")
                if not os.path.exists(fp_path) or os.path.getsize(fp_path) != len(raw):
                    with open(fp_path, "wb") as fp:
                        fp.write(raw)
                self._wav_files[eff] = fp_path
        except Exception:
            pass

    def play(self, effect: PetSoundEffect) -> None:
        if not self.enabled or self._disposed:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_play_monotonic < 0.14:
                return
            self._last_play_monotonic = now

        wav_path = self._wav_files.get(effect)
        if wav_path and os.path.exists(wav_path):
            try:
                winsound.PlaySound(
                    wav_path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return
            except Exception:
                pass

        raw = self._wav_bytes.get(effect)
        if raw:
            threading.Thread(
                target=self._play_memory_sync,
                args=(raw,),
                daemon=True,
                name="SoundFallback",
            ).start()

    def _play_memory_sync(self, raw: bytes) -> None:
        if self._disposed:
            return
        try:
            winsound.PlaySound(raw, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def dispose(self) -> None:
        self._disposed = True
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
