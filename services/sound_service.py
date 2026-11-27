"""轻量级纯内存 PCM WAV 合成音效服务（无需外部音频素材）。"""

from __future__ import annotations

from enum import Enum
import io
import math
import struct
import threading
import time
from typing import Optional
import wave
import winsound

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
    """合成并用后台音频线程异步播放猫娘互动音效，避免主线程触碰磁盘或阻塞渲染帧。"""

    def __init__(self) -> None:
        self.enabled: bool = True
        self._disposed = False
        self._last_play_monotonic = 0.0
        self._lock = threading.Lock()
        self._play_event = threading.Event()
        self._pending_wav: Optional[bytes] = None
        # 持有当前播放中的内存 WAV 引用，防止 SND_MEMORY | SND_ASYNC 播放期间缓冲区被回收
        self._active_wav_ref: Optional[bytes] = None
        self._wav_bytes: dict[PetSoundEffect, bytes] = {
            PetSoundEffect.CHIME: _synthesize_notes([(784, 0.06, 0.16), (1046.5, 0.09, 0.14)]),
            PetSoundEffect.PURR: _synthesize_purr(),
            PetSoundEffect.SPARKLE: _synthesize_notes(
                [(880, 0.045, 0.12), (1174.6, 0.05, 0.12), (1567.9, 0.09, 0.13)]
            ),
            PetSoundEffect.BOING: _synthesize_sweep(340, 620, 0.11, 0.16),
            PetSoundEffect.ANGRY: _synthesize_sweep(480, 220, 0.12, 0.18),
        }
        # 按撞击/弹跳力度预合成三档 BOING 音色，运行时零主线程合成开销
        self._boing_tiers: tuple[bytes, bytes, bytes] = (
            _synthesize_sweep(270, 470, 0.095, 0.11),
            self._wav_bytes[PetSoundEffect.BOING],
            _synthesize_sweep(390, 760, 0.125, 0.21),
        )
        self._worker = threading.Thread(
            target=self._audio_worker_loop,
            name="GeminiDesktopPetSoundWorker",
            daemon=True,
        )
        self._worker.start()

    def play(self, effect: PetSoundEffect, *, intensity: Optional[float] = None) -> None:
        if not self.enabled or self._disposed:
            return
        now = time.monotonic()
        if effect == PetSoundEffect.BOING and intensity is not None:
            lvl = max(0.0, min(1.0, float(intensity)))
            raw = self._boing_tiers[0 if lvl < 0.35 else (1 if lvl < 0.72 else 2)]
        else:
            raw = self._wav_bytes.get(effect)
        if not raw:
            return
        with self._lock:
            if self._disposed or (now - self._last_play_monotonic) < 0.14:
                return
            self._last_play_monotonic = now
            self._pending_wav = raw
        self._play_event.set()

    def _audio_worker_loop(self) -> None:
        while not self._disposed:
            self._play_event.wait()
            self._play_event.clear()
            if self._disposed:
                break
            with self._lock:
                raw = self._pending_wav
                self._pending_wav = None
                self._active_wav_ref = raw
            if not raw or self._disposed:
                continue
            try:
                winsound.PlaySound(
                    raw,
                    winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            except Exception:
                pass

    def dispose(self) -> None:
        with self._lock:
            self._disposed = True
            self._pending_wav = None
        self._play_event.set()
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        if self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=0.25)
        self._active_wav_ref = None
