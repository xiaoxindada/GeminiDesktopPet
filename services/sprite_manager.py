"""立绘、动作片段（静态 PNG / 透明动态 WebP）与骨骼部件资源管理器。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap

from models.app_version import get_bundled_resource_dir
from models.pet_settings import PetStyleMode
from models.pet_state import PetState
from services.action_clip import (
    ActionClip,
    AnimatedClipCache,
    AnimatedWebpClip,
    ClipMetadata,
    SpriteMetrics,
    StaticSpriteClip,
    analyze_qimage_metrics,
    load_manifest_entries,
    parse_clip_metadata,
    prime_pixmap_metrics,
    probe_animated_webp,
)


_SMOOTH_DOWNSCALE_CACHE_MAX = 192
_smooth_downscale_cache: OrderedDict[tuple[int, int, int, int, int, int, int], QPixmap] = OrderedDict()
# 后台线程预缩放的 QImage 缓冲（按文件规范路径 + 源矩形 + 目标物理尺寸索引，供主线程零计算转 QPixmap）
_predecoded_downscale_lock = threading.Lock()
_predecoded_downscale_images: OrderedDict[tuple[str, int, int, int, int, int, int], QImage] = OrderedDict()
_pixmap_source_paths: dict[int, str] = {}
_PREDECODED_DOWNSCALE_MAX = 160

# 标准立绘内置归一化默认参数（可通过 manifest.json 覆盖），仅对蹲坐等低重心姿态缩放高度
_DEFAULT_STANDARD_METADATA: dict[str, ClipMetadata] = {
    PetState.SULK.value: ClipMetadata(render_scale=0.68),
    PetState.COMPACT.value: ClipMetadata(render_scale=0.82),
}


def _register_pixmap_path(pixmap: Optional[QPixmap], path: str) -> None:
    if pixmap is None or pixmap.isNull() or not path:
        return
    _pixmap_source_paths[int(pixmap.cacheKey())] = os.path.normcase(os.path.abspath(path))


def _store_predecoded_downscale(
    norm_path: str,
    source: QRect,
    phys_w: int,
    phys_h: int,
    scaled_img: QImage,
) -> None:
    key = (norm_path, source.x(), source.y(), source.width(), source.height(), phys_w, phys_h)
    with _predecoded_downscale_lock:
        _predecoded_downscale_images[key] = scaled_img
        _predecoded_downscale_images.move_to_end(key)
        while len(_predecoded_downscale_images) > _PREDECODED_DOWNSCALE_MAX:
            _predecoded_downscale_images.popitem(last=False)


def _take_predecoded_downscale(
    pixmap: QPixmap,
    source: QRect,
    phys_w: int,
    phys_h: int,
) -> Optional[QImage]:
    norm_path = _pixmap_source_paths.get(int(pixmap.cacheKey()))
    if not norm_path:
        return None
    key = (norm_path, source.x(), source.y(), source.width(), source.height(), phys_w, phys_h)
    with _predecoded_downscale_lock:
        return _predecoded_downscale_images.get(key)


def _get_smooth_downscaled_pixmap(
    pixmap: QPixmap,
    source: QRect,
    phys_w: int,
    phys_h: int,
) -> QPixmap:
    """用预乘 Alpha 面积平均滤波缓存缩图，优先消费后台预缩好的 QImage 消除主线程卡顿。"""
    key = (int(pixmap.cacheKey()), source.x(), source.y(), source.width(), source.height(), phys_w, phys_h)
    cached = _smooth_downscale_cache.get(key)
    if cached is not None:
        _smooth_downscale_cache.move_to_end(key)
        return cached

    pre_img = _take_predecoded_downscale(pixmap, source, phys_w, phys_h)
    if pre_img is not None and not pre_img.isNull():
        scaled_pm = QPixmap.fromImage(pre_img)
    else:
        full_rect = QRect(0, 0, pixmap.width(), pixmap.height())
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if source != full_rect:
            image = image.copy(source)
        scaled_img = image.scaled(
            phys_w,
            phys_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled_pm = QPixmap.fromImage(scaled_img)
    _smooth_downscale_cache[key] = scaled_pm
    while len(_smooth_downscale_cache) > _SMOOTH_DOWNSCALE_CACHE_MAX:
        _smooth_downscale_cache.popitem(last=False)
    return scaled_pm


def draw_pixmap_smooth(
    painter: QPainter,
    pixmap: Optional[QPixmap],
    target: QRectF,
    source: Optional[QRect] = None,
) -> None:
    """按设备物理像素尺寸高质量绘制贴图，大倍率缩小时自动使用面积平均滤波。"""
    if pixmap is None or pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
        return
    full_rect = QRect(0, 0, pixmap.width(), pixmap.height())
    src = full_rect if source is None else source.intersected(full_rect)
    if src.isEmpty():
        return

    device = painter.device()
    dpr = max(1.0, float(device.devicePixelRatioF())) if device is not None else 1.0
    phys_w = max(1, int(round(target.width() * dpr)))
    phys_h = max(1, int(round(target.height() * dpr)))

    if src.width() > phys_w or src.height() > phys_h:
        scaled = _get_smooth_downscaled_pixmap(pixmap, src, phys_w, phys_h)
        painter.drawPixmap(target, scaled, QRectF(0.0, 0.0, float(scaled.width()), float(scaled.height())))
        return

    painter.drawPixmap(target, pixmap, QRectF(src))


@dataclass(frozen=True)
class WalkRigAssets:
    chibi_body: Optional[QPixmap] = None
    chibi_arm_l: Optional[QPixmap] = None
    chibi_arm_r: Optional[QPixmap] = None
    chibi_leg_l: Optional[QPixmap] = None
    chibi_leg_r: Optional[QPixmap] = None
    chibi_tail: Optional[QPixmap] = None
    std_body: Optional[QPixmap] = None
    std_front_thigh: Optional[QPixmap] = None
    std_front_shin: Optional[QPixmap] = None
    std_back_thigh: Optional[QPixmap] = None
    std_back_shin: Optional[QPixmap] = None
    std_front_arm: Optional[QPixmap] = None
    std_back_arm: Optional[QPixmap] = None
    std_tail: Optional[QPixmap] = None

    @property
    def chibi_complete(self) -> bool:
        return all(
            getattr(self, name) is not None
            for name in ("chibi_body", "chibi_arm_l", "chibi_arm_r", "chibi_leg_l", "chibi_leg_r")
        )

    @property
    def standard_complete(self) -> bool:
        return all(
            getattr(self, name) is not None
            for name in ("std_body", "std_front_thigh", "std_front_shin")
        )


_SEMANTIC_FALLBACK_GROUPS: tuple[tuple[PetState, ...], ...] = (
    (PetState.PAT, PetState.HAPPY, PetState.COAXED, PetState.FORGIVEN, PetState.PROUD),
    (PetState.THINKING, PetState.READING, PetState.WRITING, PetState.DRAWING, PetState.LISTENING),
    (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP, PetState.TIRED, PetState.YAWN),
    (PetState.RUNNING, PetState.SEARCHING),
    (PetState.ANNOYED, PetState.PUFFED, PetState.REDHOT, PetState.SULK),
    (PetState.EDGE_LEFT_PEEK, PetState.EDGE_LEFT),
    (PetState.EDGE_RIGHT_PEEK, PetState.EDGE_RIGHT),
)


class SpriteManager:
    """加载静态立绘、透明动态 WebP 片段、manifest 归一化元数据与步行骨架。"""

    def __init__(self, assets_root: Optional[str] = None) -> None:
        base = assets_root or os.path.join(get_bundled_resource_dir(), "Assets")
        self._assets_root = base
        self._sprites_dir = os.path.join(base, "Sprites")
        self._standard_dir = os.path.join(base, "Standard")
        self._animations_dir = os.path.join(base, "Animations")
        self._walkrig_dir = os.path.join(base, "WalkRig")

        # 静态 QPixmap 缓存（保持与原有调用方完全兼容）
        self._cache: dict[PetState, QPixmap] = {}
        self._standard_cache: dict[str, QPixmap] = {}
        self._fallback: Optional[QPixmap] = None
        self._standard_fallback: Optional[QPixmap] = None
        self.walk_rig = WalkRigAssets()

        # 动作片段（ActionClip）与动态 WebP 有界 LRU 缓存
        self._anim_cache = AnimatedClipCache()
        self._chibi_clips: dict[str, ActionClip] = {}
        self._standard_clips: dict[str, ActionClip] = {}
        self._chibi_manifest: dict[str, dict[str, Any]] = {}
        self._standard_manifest: dict[str, dict[str, Any]] = {}
        self._clip_ready_callbacks: list[Callable[[], None]] = []

        # 后台静默全量 PNG 解码与预缩图流水线（仅操作线程安全 QImage，零主线程卡顿）
        self._preload_lock = threading.Lock()
        self._predecoded_standard: dict[str, tuple[str, QImage, SpriteMetrics]] = {}
        self._predecoded_metrics_by_path: dict[str, SpriteMetrics] = {}
        self._preload_stop = threading.Event()
        self._preload_generation = 0
        self._preload_thread: Optional[threading.Thread] = None
        self._last_warm_key: Optional[tuple[PetStyleMode, int, float]] = None

    def add_clip_ready_listener(self, callback: Callable[[], None]) -> None:
        """注册动态 WebP 后台解码完成通知（回调可能在后台线程触发，调用方需线程安全转发）。"""
        if callback not in self._clip_ready_callbacks:
            self._clip_ready_callbacks.append(callback)

    def remove_clip_ready_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._clip_ready_callbacks:
            self._clip_ready_callbacks.remove(callback)

    def _notify_clip_ready(self) -> None:
        for cb in tuple(self._clip_ready_callbacks):
            try:
                cb()
            except Exception:
                pass

    def dispose(self) -> None:
        """释放后台动态解码线程、预热线程与帧缓存。"""
        self._preload_stop.set()
        thread = self._preload_thread
        self._preload_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.35)
        self._clip_ready_callbacks.clear()
        self._anim_cache.dispose()

    def warm_render_cache(
        self,
        style_mode: PetStyleMode,
        sprite_height_px: float,
        dpr: float = 1.0,
    ) -> None:
        """在后台线程静默预解码全量立绘 PNG、计算 Alpha 重心并预缩至当前物理分辨率。

        全程只操作线程安全的 QImage，主线程零阻塞；后续首次切换到任意标准立绘动作时
        直接命中预解码图像，从 35~40ms 磁盘解码降至 < 1ms。
        """
        eff_dpr = round(max(1.0, float(dpr)), 2)
        target_h = max(64, int(round(float(sprite_height_px))))
        warm_key = (style_mode, target_h, eff_dpr)
        if warm_key == self._last_warm_key:
            return
        self._last_warm_key = warm_key
        with self._preload_lock:
            self._preload_generation += 1
            gen = self._preload_generation

        # 优先预热当前画风高频动作，再补全全部其余动作（覆盖全部 52 张标准立绘）
        priority_states: tuple[PetState, ...] = (
            PetState.IDLE,
            PetState.FLOAT,
            PetState.HAPPY,
            PetState.PAT,
            PetState.SURPRISED,
            PetState.STRETCH,
            PetState.THINKING,
            PetState.DIZZY,
            PetState.ANNOYED,
            PetState.PUFFED,
            PetState.REDHOT,
            PetState.SULK,
            PetState.HUM,
            PetState.READING,
            PetState.WRITING,
            PetState.RUNNING,
            PetState.GREET_MORNING,
            PetState.GREET_NOON,
            PetState.GREET_AFTERNOON,
            PetState.GREET_EVENING,
            PetState.GREET_NIGHT,
            PetState.GREET_DAWN,
        )
        seen_keys: set[str] = set()
        ordered_keys: list[str] = []
        for st in priority_states + tuple(PetState):
            k = st.to_sprite_key()
            if k not in seen_keys:
                seen_keys.add(k)
                ordered_keys.append(k)

        already_loaded_standard = set(self._standard_cache.keys())
        tasks: list[tuple[PetStyleMode, str, str, float]] = []
        if style_mode == PetStyleMode.STANDARD:
            std_keys: list[str] = []
            std_seen: set[str] = set()
            for k in ("standee", "front", "side", "back", *ordered_keys):
                if k not in std_seen:
                    std_seen.add(k)
                    std_keys.append(k)
            for k in std_keys:
                path = os.path.join(self._standard_dir, f"{k}.png")
                if os.path.isfile(path):
                    meta = self._metadata_for_key(k, PetStyleMode.STANDARD)
                    tasks.append((PetStyleMode.STANDARD, k, path, meta.render_scale))
        else:
            for k in ordered_keys:
                path = os.path.join(self._sprites_dir, f"{k}.png")
                if os.path.isfile(path):
                    meta = self._metadata_for_key(k, PetStyleMode.CHIBI)
                    tasks.append((PetStyleMode.CHIBI, k, path, meta.render_scale))

        # 优先在后台预缩当前画风的 WalkRig 部件，消除首次启动散步时的 15~25ms 首帧缩图尖峰
        walkrig_tasks: list[tuple[str, int, int]] = []
        if style_mode == PetStyleMode.STANDARD:
            rig_rh = float(target_h)
            rig_rw = rig_rh * (337.0 / 977.0)
            rig_pw = max(1, int(round(rig_rw * eff_dpr)))
            rig_ph = max(1, int(round(rig_rh * eff_dpr)))
            for fname in (
                "std_body.png",
                "std_thigh.png",
                "std_shin.png",
                "std_thigh_back.png",
                "std_shin_back.png",
                "std_arm.png",
                "std_arm_back.png",
                "std_tail.png",
            ):
                p = os.path.join(self._walkrig_dir, fname)
                if os.path.isfile(p):
                    walkrig_tasks.append((p, rig_pw, rig_ph))
        else:
            rig_rh = float(target_h)
            rig_rw = rig_rh * (555.0 / 640.0)
            rig_pw = max(1, int(round(rig_rw * eff_dpr)))
            rig_ph = max(1, int(round(rig_rh * eff_dpr)))
            for fname in (
                "chibi_body.png",
                "chibi_arm_l.png",
                "chibi_arm_r.png",
                "chibi_leg_l.png",
                "chibi_leg_r.png",
                "chibi_tail.png",
            ):
                p = os.path.join(self._walkrig_dir, fname)
                if os.path.isfile(p):
                    walkrig_tasks.append((p, rig_pw, rig_ph))

        cache_max_h = max(900, int(round(float(target_h) * eff_dpr * 1.6)))

        def _worker() -> None:
            for rig_path, rig_pw, rig_ph in walkrig_tasks:
                if self._preload_stop.is_set() or self._preload_generation != gen:
                    return
                norm_path = os.path.normcase(os.path.abspath(rig_path))
                decoded = QImage(rig_path)
                if decoded.isNull():
                    continue
                with self._preload_lock:
                    rig_metrics = self._predecoded_metrics_by_path.get(norm_path)
                if rig_metrics is None:
                    rig_metrics = analyze_qimage_metrics(decoded)
                    with self._preload_lock:
                        self._predecoded_metrics_by_path[norm_path] = rig_metrics
                # 按紧致非透明包围盒与 draw_pixmap_uniform 同比例预缩图，确保首步行走 100% 命中缓存
                src = rig_metrics.content_rect
                ratio = min(rig_pw / max(1.0, float(decoded.width())), rig_ph / max(1.0, float(decoded.height())))
                sub_pw = max(1, int(round(src.width() * ratio)))
                sub_ph = max(1, int(round(src.height() * ratio)))
                if src.width() > sub_pw or src.height() > sub_ph:
                    down_key = (norm_path, src.x(), src.y(), src.width(), src.height(), sub_pw, sub_ph)
                    with _predecoded_downscale_lock:
                        already = down_key in _predecoded_downscale_images
                    if not already:
                        full_rect = QRect(0, 0, decoded.width(), decoded.height())
                        premul = decoded.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                        if src != full_rect:
                            premul = premul.copy(src)
                        scaled_img = premul.scaled(
                            sub_pw,
                            sub_ph,
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        _store_predecoded_downscale(norm_path, src, sub_pw, sub_ph, scaled_img)

            for mode, key, path, render_scale in tasks:
                if self._preload_stop.is_set() or self._preload_generation != gen:
                    return
                norm_path = os.path.normcase(os.path.abspath(path))
                img: Optional[QImage] = None
                metrics: Optional[SpriteMetrics] = None
                from_compact_cache = False
                if mode == PetStyleMode.STANDARD and key not in already_loaded_standard:
                    with self._preload_lock:
                        cached_entry = self._predecoded_standard.get(key)
                    if cached_entry is not None and cached_entry[1].height() >= int(round(target_h * eff_dpr)):
                        _, img, metrics = cached_entry
                        from_compact_cache = True
                if img is None:
                    decoded = QImage(path)
                    if decoded.isNull():
                        continue
                    img = decoded
                    with self._preload_lock:
                        metrics = self._predecoded_metrics_by_path.get(norm_path)
                    if metrics is None:
                        metrics = analyze_qimage_metrics(img)
                        with self._preload_lock:
                            self._predecoded_metrics_by_path[norm_path] = metrics

                if metrics is None:
                    continue
                src = metrics.content_rect
                cw = float(src.width())
                ch = float(src.height())
                if cw <= 0.0 or ch <= 0.0:
                    continue
                rh = (
                    float(target_h) * render_scale
                    if (render_scale == 1.0 or mode == PetStyleMode.STANDARD or (cw / ch) > 0.68)
                    else float(target_h)
                )
                rw = rh * (cw / ch)
                phys_w = max(1, int(round(rw * eff_dpr)))
                phys_h = max(1, int(round(rh * eff_dpr)))

                # 仅在未加载到主缓存的标准立绘上保留紧致高分 QImage（裁掉透明边并封顶高度，省去 ~260MB 内存）
                premul_cropped: Optional[QImage] = None
                down_src = src
                if mode == PetStyleMode.STANDARD and key not in already_loaded_standard:
                    if not from_compact_cache:
                        full_rect = QRect(0, 0, img.width(), img.height())
                        premul_cropped = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                        if src != full_rect:
                            premul_cropped = premul_cropped.copy(src)
                        if premul_cropped.height() > cache_max_h:
                            cw_cap = max(1, int(round(premul_cropped.width() * (cache_max_h / premul_cropped.height()))))
                            compact_img = premul_cropped.scaled(
                                cw_cap,
                                cache_max_h,
                                Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        else:
                            compact_img = premul_cropped
                        compact_metrics = SpriteMetrics(
                            QRect(0, 0, compact_img.width(), compact_img.height()),
                            metrics.anchor_x_norm,
                        )
                        with self._preload_lock:
                            self._predecoded_standard[key] = (norm_path, compact_img, compact_metrics)
                        down_src = compact_metrics.content_rect
                    else:
                        premul_cropped = img
                        down_src = src

                if down_src.width() > phys_w or down_src.height() > phys_h:
                    down_key = (
                        norm_path,
                        down_src.x(),
                        down_src.y(),
                        down_src.width(),
                        down_src.height(),
                        phys_w,
                        phys_h,
                    )
                    with _predecoded_downscale_lock:
                        already = down_key in _predecoded_downscale_images
                    if not already:
                        if premul_cropped is None:
                            full_rect = QRect(0, 0, img.width(), img.height())
                            premul_cropped = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                            if down_src != full_rect:
                                premul_cropped = premul_cropped.copy(down_src)
                        scaled_img = premul_cropped.scaled(
                            phys_w,
                            phys_h,
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        _store_predecoded_downscale(norm_path, down_src, phys_w, phys_h, scaled_img)

        self._preload_stop.clear()
        self._preload_thread = threading.Thread(
            target=_worker,
            name="GeminiDesktopPetSpritePreload",
            daemon=True,
        )
        self._preload_thread.start()

    def preload_all(self) -> None:
        self._load_manifests()

        self._fallback = self._load_pixmap(os.path.join(self._sprites_dir, "idle.png"))
        for state in PetState:
            key = state.to_sprite_key()
            pix = self._load_pixmap(os.path.join(self._sprites_dir, f"{key}.png"))
            if pix is not None:
                self._cache[state] = pix

        # 预加载标准立绘分面贴图（standee / front / side / back / edge_*）
        self._standard_fallback = self._load_pixmap(os.path.join(self._standard_dir, "standee.png"))
        for std_key in (
            "standee",
            "front",
            "side",
            "back",
            "edge_left",
            "edge_left_peek",
            "edge_right",
            "edge_right_peek",
            "busy_left",
            "busy_right",
            "swamped_left",
            "swamped_right",
            "hum",
        ):
            pix = self._load_pixmap(os.path.join(self._standard_dir, f"{std_key}.png"))
            if pix is not None:
                self._standard_cache[std_key] = pix

        self.walk_rig = WalkRigAssets(
            chibi_body=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_body.png")),
            chibi_arm_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_l.png")),
            chibi_arm_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_arm_r.png")),
            chibi_leg_l=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_l.png")),
            chibi_leg_r=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_leg_r.png")),
            chibi_tail=self._load_pixmap(os.path.join(self._walkrig_dir, "chibi_tail.png")),
            std_body=self._load_pixmap(os.path.join(self._walkrig_dir, "std_body.png")),
            std_front_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh.png")),
            std_front_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin.png")),
            std_back_thigh=self._load_pixmap(os.path.join(self._walkrig_dir, "std_thigh_back.png")),
            std_back_shin=self._load_pixmap(os.path.join(self._walkrig_dir, "std_shin_back.png")),
            std_front_arm=self._load_pixmap(os.path.join(self._walkrig_dir, "std_arm.png")),
            std_back_arm=self._load_pixmap(os.path.join(self._walkrig_dir, "std_arm_back.png")),
            std_tail=self._load_pixmap(os.path.join(self._walkrig_dir, "std_tail.png")),
        )
        self._ensure_base_poses()

        # 轻量探测已存在的动态 WebP 片段（仅读头信息，不预解码全部帧）
        self._chibi_clips.clear()
        self._standard_clips.clear()
        for state in PetState:
            key = state.to_sprite_key()
            chibi_clip = self._build_clip_for_key(key, PetStyleMode.CHIBI, self._cache.get(state))
            if chibi_clip is not None:
                self._chibi_clips[key] = chibi_clip
            std_clip = self._build_clip_for_key(
                key,
                PetStyleMode.STANDARD,
                self._standard_cache.get(key),
            )
            if std_clip is not None:
                self._standard_clips[key] = std_clip

    def _load_manifests(self) -> None:
        self._chibi_manifest = {}
        self._standard_manifest = {}

        root_manifest_path = os.path.join(self._assets_root, "manifest.json")
        if os.path.isfile(root_manifest_path):
            root_entries = load_manifest_entries(root_manifest_path)
            if isinstance(root_entries.get("chibi"), dict):
                self._chibi_manifest.update(
                    {
                        str(k).strip().lower(): v
                        for k, v in root_entries["chibi"].items()
                        if isinstance(v, dict)
                    }
                )
            if isinstance(root_entries.get("standard"), dict):
                self._standard_manifest.update(
                    {
                        str(k).strip().lower(): v
                        for k, v in root_entries["standard"].items()
                        if isinstance(v, dict)
                    }
                )

        self._chibi_manifest.update(
            load_manifest_entries(os.path.join(self._sprites_dir, "manifest.json"))
        )
        self._standard_manifest.update(
            load_manifest_entries(os.path.join(self._standard_dir, "manifest.json"))
        )

    def _resolve_animated_path(
        self,
        key: str,
        style_mode: PetStyleMode,
        entry: Optional[dict[str, Any]],
    ) -> Optional[str]:
        style_dir = self._standard_dir if style_mode == PetStyleMode.STANDARD else self._sprites_dir
        anim_sub = "Standard" if style_mode == PetStyleMode.STANDARD else "Sprites"
        candidates: list[str] = []

        if isinstance(entry, dict):
            raw_file = entry.get("file") or entry.get("webp")
            if isinstance(raw_file, str) and raw_file.strip():
                rel = raw_file.strip()
                if os.path.isabs(rel):
                    candidates.append(rel)
                else:
                    candidates.append(os.path.join(style_dir, rel))
                    candidates.append(os.path.join(self._animations_dir, anim_sub, rel))
                    candidates.append(os.path.join(self._assets_root, rel))

        candidates.append(os.path.join(self._animations_dir, anim_sub, f"{key}.webp"))
        candidates.append(os.path.join(style_dir, f"{key}.webp"))

        for path in candidates:
            if path.lower().endswith(".webp") and os.path.isfile(path):
                return path
        return None

    def _metadata_for_key(self, key: str, style_mode: PetStyleMode) -> ClipMetadata:
        norm_key = key.strip().lower()
        base = (
            _DEFAULT_STANDARD_METADATA.get(norm_key, ClipMetadata())
            if style_mode == PetStyleMode.STANDARD
            else ClipMetadata()
        )
        manifest = self._standard_manifest if style_mode == PetStyleMode.STANDARD else self._chibi_manifest
        entry = manifest.get(norm_key)
        return parse_clip_metadata(entry, base) if entry else base

    def _build_clip_for_key(
        self,
        key: str,
        style_mode: PetStyleMode,
        static_pixmap: Optional[QPixmap],
    ) -> Optional[ActionClip]:
        norm_key = key.strip().lower()
        manifest = self._standard_manifest if style_mode == PetStyleMode.STANDARD else self._chibi_manifest
        entry = manifest.get(norm_key)
        meta = self._metadata_for_key(norm_key, style_mode)
        clip_id = f"{style_mode.value.lower()}:{norm_key}"

        webp_path = self._resolve_animated_path(key, style_mode, entry)
        if webp_path is not None:
            probed = probe_animated_webp(webp_path)
            if probed is not None:
                frame_count, natural_size = probed
                if frame_count > 1:
                    return AnimatedWebpClip(
                        clip_id=clip_id,
                        file_path=webp_path,
                        cache=self._anim_cache,
                        frame_count=frame_count,
                        natural_size=natural_size,
                        fallback_pixmap=static_pixmap,
                        metadata=meta,
                        on_decoded=self._notify_clip_ready,
                    )
                # 单帧 .webp 按静态贴图加载
                single_pm = self._load_pixmap(webp_path)
                if single_pm is not None:
                    return StaticSpriteClip(clip_id, single_pm, meta)

        if static_pixmap is not None and not static_pixmap.isNull():
            return StaticSpriteClip(clip_id, static_pixmap, meta)
        return None

    def _ensure_base_poses(self) -> None:
        rig = self.walk_rig
        if self._fallback is None and rig.chibi_complete:
            chibi_parts = [rig.chibi_arm_l, rig.chibi_leg_r, rig.chibi_leg_l, rig.chibi_body, rig.chibi_arm_r]
            if rig.chibi_tail is not None:
                chibi_parts.insert(0, rig.chibi_tail)
            self._fallback = self._compose(tuple(chibi_parts))
        if self._standard_fallback is None and rig.standard_complete:
            std_parts = [
                rig.std_back_thigh or rig.std_front_thigh,
                rig.std_back_shin or rig.std_front_shin,
                rig.std_front_thigh,
                rig.std_front_shin,
            ]
            if rig.std_tail is not None:
                std_parts.append(rig.std_tail)
            std_parts.append(rig.std_body)
            if rig.std_front_arm is not None:
                std_parts.append(rig.std_front_arm)
            self._standard_fallback = self._compose(tuple(std_parts))
        # 单风格资源包也保持原版形象，允许后续只提供一张基础图。
        self._fallback = self._fallback or self._standard_fallback
        self._standard_fallback = self._standard_fallback or self._fallback

    @staticmethod
    def _compose(parts: tuple[QPixmap, ...]) -> QPixmap:
        result = QPixmap(parts[0].size())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        for part in parts:
            painter.drawPixmap(result.rect(), part)
        painter.end()
        return result

    def has_animated_clip(
        self,
        state: PetState,
        style_mode: PetStyleMode = PetStyleMode.CHIBI,
    ) -> bool:
        """判断指定状态在当前画风下是否拥有多帧动态素材（用于覆盖默认 WalkRig 等）。"""
        clip = self._get_exact_clip(state, style_mode)
        return clip is not None and clip.is_animated

    def _get_exact_clip(
        self,
        state: PetState,
        style_mode: PetStyleMode,
    ) -> Optional[ActionClip]:
        key = state.to_sprite_key()
        if style_mode == PetStyleMode.STANDARD:
            clip = self._standard_clips.get(key)
            if clip is not None:
                return clip
            pix = self._standard_cache.get(key)
            primed_from_compact = False
            if pix is None:
                # 优先消费后台线程已解出的紧致 QImage + SpriteMetrics，将首次切图耗时从 ~35ms 降至 <1ms
                with self._preload_lock:
                    predecoded = self._predecoded_standard.pop(key, None)
                if predecoded is not None:
                    norm_path, pre_img, pre_metrics = predecoded
                    pix = QPixmap.fromImage(pre_img)
                    if not pix.isNull():
                        _register_pixmap_path(pix, norm_path)
                        prime_pixmap_metrics(pix, pre_metrics)
                        self._standard_cache[key] = pix
                        primed_from_compact = True
                    else:
                        pix = None
                if pix is None:
                    pix = self._load_pixmap(os.path.join(self._standard_dir, f"{key}.png"))
                    if pix is not None:
                        self._standard_cache[key] = pix
            if pix is not None and not primed_from_compact:
                norm_path = _pixmap_source_paths.get(int(pix.cacheKey()))
                if norm_path:
                    with self._preload_lock:
                        pre_metrics = self._predecoded_metrics_by_path.get(norm_path)
                    if pre_metrics is not None:
                        prime_pixmap_metrics(pix, pre_metrics)
            clip = self._build_clip_for_key(key, PetStyleMode.STANDARD, pix)
            if clip is not None:
                self._standard_clips[key] = clip
            return clip

        clip = self._chibi_clips.get(key)
        if clip is not None:
            return clip
        pix = self._cache.get(state)
        if pix is not None:
            norm_path = _pixmap_source_paths.get(int(pix.cacheKey()))
            if norm_path:
                with self._preload_lock:
                    pre_metrics = self._predecoded_metrics_by_path.get(norm_path)
                if pre_metrics is not None:
                    prime_pixmap_metrics(pix, pre_metrics)
        clip = self._build_clip_for_key(key, PetStyleMode.CHIBI, pix)
        if clip is not None:
            self._chibi_clips[key] = clip
        return clip

    def get_clip(
        self,
        state: PetState,
        style_mode: PetStyleMode = PetStyleMode.CHIBI,
    ) -> Optional[ActionClip]:
        """按语义回落链返回统一 ActionClip（优先透明动态 WebP，其次精确静态图，再按语义兜底）。"""
        if style_mode == PetStyleMode.STANDARD:
            std_clip = self._get_standard_clip_for_state(state)
            if std_clip is not None:
                return std_clip

        group = next((g for g in _SEMANTIC_FALLBACK_GROUPS if state in g), ())
        for candidate in (state,) + group:
            clip = self._get_exact_clip(candidate, PetStyleMode.CHIBI)
            if clip is not None:
                return clip

        if self._fallback is not None:
            fallback_key = "fallback:chibi"
            cached = self._chibi_clips.get(fallback_key)
            if cached is None:
                cached = StaticSpriteClip(fallback_key, self._fallback, ClipMetadata())
                self._chibi_clips[fallback_key] = cached
            return cached
        return None

    def _get_standard_clip_for_state(self, state: PetState) -> Optional[ActionClip]:
        exact = self._get_exact_clip(state, PetStyleMode.STANDARD)
        if exact is not None:
            return exact

        if state in (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP):
            for sleep_state in (PetState.SLEEPING, PetState.EDGE_LEFT_SLEEP, PetState.EDGE_RIGHT_SLEEP):
                clip = self._get_exact_clip(sleep_state, PetStyleMode.STANDARD)
                if clip is not None:
                    return clip
            if self._standard_fallback is not None:
                return self._wrap_standard_named_clip("standee", self._standard_fallback)
            return None

        fallback_key = self._standard_fallback_key(state)
        pix = self._standard_cache.get(fallback_key) or self._standard_fallback
        if pix is not None:
            return self._wrap_standard_named_clip(fallback_key, pix)
        return None

    def _wrap_standard_named_clip(self, key: str, pixmap: QPixmap) -> ActionClip:
        cache_key = f"__std_named__:{key}"
        clip = self._standard_clips.get(cache_key)
        if clip is not None and clip.preview_pixmap() is pixmap:
            return clip
        built = self._build_clip_for_key(key, PetStyleMode.STANDARD, pixmap)
        if built is None:
            built = StaticSpriteClip(f"standard:{key}", pixmap, self._metadata_for_key(key, PetStyleMode.STANDARD))
        self._standard_clips[cache_key] = built
        return built

    @staticmethod
    def _standard_fallback_key(state: PetState) -> str:
        if state == PetState.EDGE_LEFT:
            return "edge_left"
        if state == PetState.EDGE_LEFT_PEEK:
            return "edge_left_peek"
        if state == PetState.EDGE_RIGHT:
            return "edge_right"
        if state == PetState.EDGE_RIGHT_PEEK:
            return "edge_right_peek"
        if state in (PetState.RUNNING, PetState.SEARCHING):
            return "side"
        if state in (PetState.SULK, PetState.BYE, PetState.TIRED):
            return "back"
        if state in (
            PetState.IDLE2,
            PetState.IDLE3,
            PetState.THINKING,
            PetState.READING,
            PetState.WRITING,
            PetState.LISTENING,
            PetState.ANNOYED,
            PetState.PUFFED,
            PetState.REDHOT,
            PetState.COMPACT,
        ):
            return "front"
        return "standee"

    def get(self, state: PetState, style_mode: PetStyleMode = PetStyleMode.CHIBI) -> Optional[QPixmap]:
        """向后兼容接口：统一经由 ActionClip 语义回落链返回代表静态 QPixmap，消除双轨解析。"""
        clip = self.get_clip(state, style_mode)
        if clip is not None:
            pm = clip.preview_pixmap()
            if pm is not None and not pm.isNull():
                return pm
        return self._standard_fallback if style_mode == PetStyleMode.STANDARD else self._fallback

    @staticmethod
    def _load_pixmap(path: str) -> Optional[QPixmap]:
        if not os.path.exists(path):
            return None
        pix = QPixmap(path)
        if pix.isNull():
            return None
        _register_pixmap_path(pix, path)
        return pix
