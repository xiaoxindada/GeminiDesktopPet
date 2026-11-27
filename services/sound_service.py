"""轻量级纯内存 PCM WAV 合成音效服务（无需外部音频素材）。"""

from __future__ import annotations

import ctypes
from enum import Enum
import io
import math
import struct
import threading
import time
from typing import Any, Optional
import wave
import winsound

_SAMPLE_RATE = 22050
_SND_ASYNC = getattr(winsound, "SND_ASYNC", 0x0001)
_SND_NODEFAULT = getattr(winsound, "SND_NODEFAULT", 0x0002)
_SND_MEMORY = getattr(winsound, "SND_MEMORY", 0x0004)
_SND_PURGE = getattr(winsound, "SND_PURGE", 0x0040)


def _load_winmm_playsound() -> Any:
    """加载独立 winmm.PlaySoundW 函数句柄，用于常驻内存缓冲区的异步播放。"""
    try:
        winmm = ctypes.WinDLL("winmm")
        fn = winmm.PlaySoundW
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return fn
    except Exception:
        return None


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
        # 独立保护 Win32 播放与释放清理的互斥锁，避免与主线程 play() 竞争
        self._playback_lock = threading.Lock()
        self._play_event = threading.Event()
        self._pending_item: Optional[tuple[bytes, Any]] = None
        self._play_sound_w = _load_winmm_playsound()
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
        # 预分配常驻 C 内存缓冲区并在整个生命周期保持强引用，供 winmm.PlaySoundW 安全异步读取
        self._wav_buffers: dict[PetSoundEffect, Any] = {
            eff: ctypes.create_string_buffer(raw, len(raw))
            for eff, raw in self._wav_bytes.items()
        }
        self._boing_tier_buffers: tuple[Any, Any, Any] = (
            ctypes.create_string_buffer(self._boing_tiers[0], len(self._boing_tiers[0])),
            self._wav_buffers[PetSoundEffect.BOING],
            ctypes.create_string_buffer(self._boing_tiers[2], len(self._boing_tiers[2])),
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
            tier_idx = 0 if lvl < 0.35 else (1 if lvl < 0.72 else 2)
            raw = self._boing_tiers[tier_idx]
            c_buf = self._boing_tier_buffers[tier_idx] if tier_idx < len(self._boing_tier_buffers) else None
        else:
            raw = self._wav_bytes.get(effect)
            c_buf = self._wav_buffers.get(effect)
        if not raw:
            return
        with self._lock:
            if self._disposed or (now - self._last_play_monotonic) < 0.14:
                return
            self._last_play_monotonic = now
            self._pending_item = (raw, c_buf)
        self._play_event.set()

    def _play_item(self, item: tuple[bytes, Any]) -> bool:
        """播放内存 WAV；CPython winsound 禁止 SND_MEMORY|SND_ASYNC，优先直调 winmm.PlaySoundW。"""
        raw, c_buf = item
        with self._playback_lock:
            if self._disposed:
                return False
            if self._play_sound_w is not None and c_buf is not None:
                try:
                    ok = bool(
                        self._play_sound_w(
                            ctypes.addressof(c_buf),
                            None,
                            _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT,
                        )
                    )
                    if ok:
                        return True
                except Exception:
                    pass
            try:
                winsound.PlaySound(raw, _SND_MEMORY | _SND_NODEFAULT)
                return True
            except Exception:
                return False

    def _purge_playback(self) -> None:
        if self._play_sound_w is not None:
            try:
                self._play_sound_w(None, None, 0)
                return
            except Exception:
                pass
        try:
            winsound.PlaySound(None, _SND_PURGE)
        except Exception:
            pass

    def _audio_worker_loop(self) -> None:
        while not self._disposed:
            self._play_event.wait()
            self._play_event.clear()
            if self._disposed:
                break
            with self._lock:
                item = self._pending_item
                self._pending_item = None
            if not item or self._disposed:
                continue
            self._play_item(item)

    def dispose(self) -> None:
        with self._lock:
            self._disposed = True
            self._pending_item = None
        self._play_event.set()
        # 先中断可能正在执行的回退同步播放，避免 join 等待过久
        self._purge_playback()
        if self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=0.5)
        # 确保音频线程已退出且异步播放完全停止后再释放常驻内存缓冲区
        with self._playback_lock:
            self._purge_playback()
            self._wav_buffers.clear()
            self._boing_tier_buffers = ()
