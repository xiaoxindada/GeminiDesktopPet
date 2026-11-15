"""可开关的本地运行日志（写入用户目录 ~/.GeminiDesktopPet/Logs/YYYY-MM-DD.log，仅保存最近 7 天）。"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import threading
from typing import Optional

from models.app_version import get_user_config_dir


class AppLogger:
    _lock = threading.Lock()
    _enabled: bool = False

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._enabled

    @classmethod
    def log_directory(cls) -> str:
        return str(get_user_config_dir() / "Logs")

    @classmethod
    def initialize(cls, enabled: bool) -> None:
        with cls._lock:
            cls._enabled = bool(enabled)
            if cls._enabled:
                cls._ensure_dir_and_prune()
        if enabled:
            cls.info("Logger", f"运行日志已启用，目录: {cls.log_directory()}")

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        if enabled == cls._enabled:
            return
        if enabled:
            with cls._lock:
                cls._enabled = True
                cls._ensure_dir_and_prune()
            cls.info("Logger", f"用户在运行中开启日志，目录: {cls.log_directory()}")
        else:
            cls.info("Logger", "用户已关闭运行日志。")
            with cls._lock:
                cls._enabled = False

    @classmethod
    def info(cls, tag: str, message: str) -> None:
        cls._write("INFO", tag, message)

    @classmethod
    def warn(cls, tag: str, message: str) -> None:
        cls._write("WARN", tag, message)

    @classmethod
    def error(cls, tag: str, message: str, exc: Optional[BaseException] = None) -> None:
        detail = message if exc is None else f"{message} | {type(exc).__name__}: {exc}"
        cls._write("ERROR", tag, detail)

    @classmethod
    def _write(cls, level: str, tag: str, message: str) -> None:
        if not cls._enabled:
            return
        try:
            now = datetime.now()
            line = f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] [{level}] [{tag}] {message}\n"
            log_dir = cls.log_directory()
            file_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.log")
            with cls._lock:
                if not cls._enabled:
                    return
                os.makedirs(log_dir, exist_ok=True)
                with open(file_path, "a", encoding="utf-8") as fp:
                    fp.write(line)
        except Exception:
            pass

    @classmethod
    def _ensure_dir_and_prune(cls) -> None:
        try:
            log_dir = cls.log_directory()
            os.makedirs(log_dir, exist_ok=True)
            cutoff = datetime.now() - timedelta(days=7)
            for name in os.listdir(log_dir):
                if not name.lower().endswith(".log"):
                    continue
                full = os.path.join(log_dir, name)
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(full))
                    if mtime < cutoff:
                        os.remove(full)
                except Exception:
                    pass
        except Exception:
            pass
