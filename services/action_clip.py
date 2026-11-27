"""统一动作片段抽象（ActionClip）与零依赖透明动态 WebP 解码后端。

兼容静态 PNG 与多帧透明动态 WebP（基于 PySide6 内置 QImageReader）：
- 静态 PNG 零开销直接包装为 StaticSpriteClip，保留原有 QPixmap 引用语义。
- 动态 WebP 启动仅探测元数据，运行时经由后台线程解帧 + 有界 LRU 缓存转为 QPixmap，
  避免 bailong 式全量预解码导致内存膨胀，也无需外挂 ffmpeg 或 Electron。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
import os
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QSize, Qt
from PySide6.QtGui import QBitmap, QImage, QImageReader, QPixmap, QRegion


_METRICS_CACHE_MAX = 384
_ANIM_CACHE_MAX_CLIPS = 4
_ANIM_MAX_DECODE_HEIGHT = 512
_DEFAULT_FRAME_DELAY_MS = 42  # ~24fps
_MIN_FRAME_DELAY_MS = 16


@dataclass(frozen=True)
class ClipMetadata:
    """单个动作片段的渲染归一化与播放节奏元数据。"""

    render_scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    anchor_x_norm: Optional[float] = None
    fps: float = 24.0
    duration_sec: float = 0.0
    loop: bool = True
    move_start_ratio: float = 0.0
    move_stop_ratio: float = 1.0
    source_facing: int = 1
    enter_file: str = ""
    exit_file: str = ""


@dataclass(frozen=True)
class SpriteMetrics:
    """贴图的紧致内容矩形与躯干/脚底水平视觉重心（归一化到内容矩形 0..1）。"""

    content_rect: QRect
    anchor_x_norm: float


_metrics_cache: OrderedDict[int, SpriteMetrics] = OrderedDict()


def _compute_anchor_x_from_image(crop: QImage) -> float:
    """用小尺寸 Alpha8 缓冲快速计算躯干至脚底非透明像素的 X 轴中位数重心。"""
    if crop.width() <= 0 or crop.height() <= 0:
        return 0.5
    sample_h = min(64, crop.height())
    sample_w = max(1, int(round(crop.width() * (sample_h / float(crop.height())))))
    small = crop.scaled(
        sample_w,
        sample_h,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    ).convertToFormat(QImage.Format.Format_Alpha8)
    raw = bytes(small.constBits())
    bpl = small.bytesPerLine()
    # 重点采样躯干到脚底区间（y ∈ [28%, 98%]），避开头顶大饰品或高举手臂拉偏重心
    y_start = int(sample_h * 0.28)
    y_end = max(y_start + 1, min(sample_h, int(sample_h * 0.98)))
    col_counts = [0] * sample_w
    total = 0
    for yy in range(y_start, y_end):
        row_start = yy * bpl
        row = raw[row_start : row_start + sample_w]
        for xx, alpha in enumerate(row):
            if alpha > 24:
                col_counts[xx] += 1
                total += 1
    if total <= 0:
        return 0.5
    half = total * 0.5
    acc = 0
    med_x = sample_w * 0.5
    for xx, cnt in enumerate(col_counts):
        acc += cnt
        if acc >= half:
            med_x = xx + 0.5
            break
    raw_anchor = med_x / float(sample_w)
    # 温和钳制在 [0.42, 0.58]，既修正偏心道具又避免常规动作切换引起明显横移
    return max(0.42, min(0.58, raw_anchor))


def _tight_alpha_bounds(alpha_img: QImage) -> QRect:
    """扫描 Format_Alpha8 内存缓冲计算非零 Alpha 紧致包围盒，避免构建 QBitmap/QRegion 开销。"""
    w = alpha_img.width()
    h = alpha_img.height()
    if w <= 0 or h <= 0:
        return QRect()
    raw = bytes(alpha_img.constBits())
    bpl = alpha_img.bytesPerLine()
    zero_row = b"\x00" * w

    top = -1
    bottom = -1
    for yy in range(h):
        off = yy * bpl
        if raw[off : off + w] != zero_row:
            top = yy
            break
    if top < 0:
        return QRect()

    for yy in range(h - 1, top - 1, -1):
        off = yy * bpl
        if raw[off : off + w] != zero_row:
            bottom = yy
            break

    left = w
    right = -1
    for yy in range(top, bottom + 1):
        off = yy * bpl
        row = raw[off : off + w]
        if row == zero_row:
            continue
        # 仅在当前行可能突破已知左右边界时细查，大幅减少扫描字节数
        if left > 0 and row[:left] != zero_row[:left]:
            stripped_l = row.lstrip(b"\x00")
            cand_l = w - len(stripped_l)
            if cand_l < left:
                left = cand_l
        if right < w - 1 and row[right + 1 :] != zero_row[right + 1 :]:
            stripped_r = row.rstrip(b"\x00")
            cand_r = len(stripped_r) - 1
            if cand_r > right:
                right = cand_r
        if left == 0 and right == w - 1:
            break

    if right < left:
        return QRect()
    return QRect(left, top, right - left + 1, bottom - top + 1)


def analyze_qimage_metrics(image: Optional[QImage]) -> SpriteMetrics:
    """线程安全地从 QImage 计算紧致 Alpha 包围盒与视觉重心 X（可在后台线程运行）。"""
    if image is None or image.isNull():
        return SpriteMetrics(QRect(0, 0, 1, 1), 0.5)
    full_bounds = QRect(0, 0, image.width(), image.height())
    bounds = full_bounds
    anchor_x = 0.5
    try:
        alpha_img = (
            image
            if image.format() == QImage.Format.Format_Alpha8
            else image.convertToFormat(QImage.Format.Format_Alpha8)
        )
        tight = _tight_alpha_bounds(alpha_img)
        if not tight.isEmpty():
            # 保留 2px 抗锯齿余量，防止发丝/半透明边缘被裁切
            bounds = tight.adjusted(-2, -2, 2, 2).intersected(full_bounds)
        anchor_x = _compute_anchor_x_from_image(alpha_img.copy(bounds))
    except Exception:
        pass
    return SpriteMetrics(bounds, anchor_x)


def prime_pixmap_metrics(pixmap: Optional[QPixmap], metrics: SpriteMetrics) -> None:
    """将后台线程预计算好的 SpriteMetrics 注册到主线程缓存，消除首次切图分析开销。"""
    if pixmap is None or pixmap.isNull():
        return
    full_rect = QRect(0, 0, pixmap.width(), pixmap.height())
    # 防御性校验：仅接受与当前 pixmap 尺寸一致的包围盒，防止紧致缓存图被原图尺寸覆盖
    if not full_rect.contains(metrics.content_rect):
        return
    key = int(pixmap.cacheKey())
    _metrics_cache[key] = metrics
    _metrics_cache.move_to_end(key)
    while len(_metrics_cache) > _METRICS_CACHE_MAX:
        _metrics_cache.popitem(last=False)


def analyze_pixmap_metrics(pixmap: Optional[QPixmap]) -> SpriteMetrics:
    """计算贴图紧致 Alpha 包围盒与视觉重心 X（躯干至脚底区间的透明像素中位数）。

    参考 dsh-pet normalize_step03 的中位数居中思路：当角色抬手或持道具偏向一侧时，
    包围盒几何中心会偏移，而中位数重心仍稳定锚定在身体与双脚上。
    """
    if pixmap is None or pixmap.isNull():
        return SpriteMetrics(QRect(0, 0, 1, 1), 0.5)

    key = int(pixmap.cacheKey())
    cached = _metrics_cache.get(key)
    if cached is not None:
        _metrics_cache.move_to_end(key)
        return cached

    result = analyze_qimage_metrics(pixmap.toImage())
    _metrics_cache[key] = result
    while len(_metrics_cache) > _METRICS_CACHE_MAX:
        _metrics_cache.popitem(last=False)
    return result


class ActionClip(ABC):
    """统一动作片段抽象，屏蔽静态贴图与逐帧动态图差异。"""

    def __init__(self, clip_id: str, metadata: Optional[ClipMetadata] = None) -> None:
        self.clip_id = clip_id
        self.metadata = metadata or ClipMetadata()

    @property
    @abstractmethod
    def is_animated(self) -> bool:
        """是否为多帧动态素材。"""

    @property
    @abstractmethod
    def duration_sec(self) -> float:
        """片段单周期时长（秒）；静态图返回 0。"""

    @abstractmethod
    def frame_at(self, elapsed_sec: float, target_height: int = 0) -> Optional[QPixmap]:
        """返回指定播放时刻的帧画面。"""

    @abstractmethod
    def preview_pixmap(self) -> Optional[QPixmap]:
        """返回代表帧（供图鉴缩略图及向后兼容接口使用）。"""

    def content_rect(self, frame: Optional[QPixmap] = None) -> QRect:
        pm = frame if frame is not None else self.preview_pixmap()
        return analyze_pixmap_metrics(pm).content_rect

    def anchor_x_ratio(self, frame: Optional[QPixmap] = None) -> float:
        if self.metadata.anchor_x_norm is not None:
            return max(0.0, min(1.0, float(self.metadata.anchor_x_norm)))
        pm = frame if frame is not None else self.preview_pixmap()
        return analyze_pixmap_metrics(pm).anchor_x_norm

    def is_finished(self, elapsed_sec: float) -> bool:
        if self.metadata.loop or self.duration_sec <= 0.0:
            return False
        return elapsed_sec >= self.duration_sec

    def movement_fraction(self, elapsed_sec: float) -> float:
        """将当前播放进度映射到移动窗口 [move_start_ratio, move_stop_ratio] 内的 [0, 1]。"""
        dur = self.duration_sec
        if dur <= 0.0:
            return 0.0
        if self.metadata.loop:
            ratio = (elapsed_sec % dur) / dur
        else:
            ratio = max(0.0, min(1.0, elapsed_sec / dur))
        start = self.metadata.move_start_ratio
        stop = self.metadata.move_stop_ratio
        if stop <= start:
            return 0.0
        return max(0.0, min(1.0, (ratio - start) / (stop - start)))


class StaticSpriteClip(ActionClip):
    """包装单张静态 PNG 的零开销 ActionClip。"""

    def __init__(
        self,
        clip_id: str,
        pixmap: QPixmap,
        metadata: Optional[ClipMetadata] = None,
    ) -> None:
        super().__init__(clip_id, metadata)
        self._pixmap = pixmap

    @property
    def is_animated(self) -> bool:
        return False

    @property
    def duration_sec(self) -> float:
        return max(0.0, float(self.metadata.duration_sec))

    def frame_at(self, elapsed_sec: float, target_height: int = 0) -> Optional[QPixmap]:
        del elapsed_sec, target_height
        return self._pixmap

    def preview_pixmap(self) -> Optional[QPixmap]:
        return self._pixmap


@dataclass
class _DecodedFrameBuffer:
    """后台线程解出的 QImage 序列，在 GUI 主线程按需转为 QPixmap。"""

    images: list[QImage]
    delays_ms: list[int]
    cumulative_ms: list[int]
    total_ms: int
    pixmaps: list[Optional[QPixmap]]
    content_rect: QRect
    anchor_x_norm: float


def _decode_webp_images_from_disk(
    file_path: str,
    max_height: int,
    fallback_fps: float,
) -> Optional[_DecodedFrameBuffer]:
    """在后台线程用内存 QBuffer 解码透明动态 WebP，彻底避免 Windows 文件句柄占用。"""
    try:
        with open(file_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError:
        return None
    if not raw_bytes:
        return None

    byte_array = QByteArray(raw_bytes)
    buf = QBuffer(byte_array)
    if not buf.open(QIODevice.OpenModeFlag.ReadOnly):
        return None

    images: list[QImage] = []
    delays_ms: list[int] = []
    default_delay = max(
        _MIN_FRAME_DELAY_MS,
        int(round(1000.0 / max(1.0, fallback_fps or 24.0))),
    )
    try:
        reader = QImageReader(buf)
        if not reader.canRead():
            return None
        count = max(1, reader.imageCount())
        for _ in range(count):
            img = reader.read()
            if img.isNull():
                break
            # Qt 的 nextImageDelay() 返回刚读出那一帧的停留时长，必须在 read() 后调用
            raw_delay = int(reader.nextImageDelay())
            delay = raw_delay if raw_delay >= _MIN_FRAME_DELAY_MS else default_delay
            if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
                img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            if max_height > 0 and img.height() > max_height:
                new_w = max(1, int(round(img.width() * (max_height / float(img.height())))))
                img = img.scaled(
                    new_w,
                    max_height,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            images.append(img)
            delays_ms.append(delay)
    finally:
        buf.close()

    if not images:
        return None

    cumulative: list[int] = []
    running = 0
    for d in delays_ms:
        running += d
        cumulative.append(running)

    # 以首帧与中段帧并集估算稳定内容边界，避免播动画时目标矩形逐帧抖动
    first = images[0]
    union_rect = QRect(0, 0, first.width(), first.height())
    anchor_x = 0.5
    try:
        sample_indices = {0, len(images) // 2, len(images) - 1}
        tight_union = QRect()
        for idx in sample_indices:
            reg = QRegion(QBitmap.fromImage(images[idx].createAlphaMask()))
            br = reg.boundingRect()
            if not br.isEmpty():
                tight_union = br if tight_union.isEmpty() else tight_union.united(br)
        if not tight_union.isEmpty():
            union_rect = tight_union.adjusted(-2, -2, 2, 2).intersected(union_rect)
        # 用首帧（基准站姿）估算水平重心
        crop = first.copy(union_rect)
        if crop.width() > 0 and crop.height() > 0:
            sh = min(64, crop.height())
            sw = max(1, int(round(crop.width() * (sh / float(crop.height())))))
            small = crop.scaled(
                sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
            )
            y0, y1 = int(sh * 0.28), max(int(sh * 0.28) + 1, int(sh * 0.98))
            cols = [0] * sw
            total = 0
            for yy in range(y0, min(sh, y1)):
                for xx in range(sw):
                    if small.pixelColor(xx, yy).alpha() > 24:
                        cols[xx] += 1
                        total += 1
            if total > 0:
                acc = 0
                half = total * 0.5
                for xx, c in enumerate(cols):
                    acc += c
                    if acc >= half:
                        anchor_x = max(0.42, min(0.58, (xx + 0.5) / float(sw)))
                        break
    except Exception:
        pass

    return _DecodedFrameBuffer(
        images=images,
        delays_ms=delays_ms,
        cumulative_ms=cumulative,
        total_ms=max(1, running),
        pixmaps=[None] * len(images),
        content_rect=union_rect,
        anchor_x_norm=anchor_x,
    )


class AnimatedClipCache:
    """管理透明动态 WebP 的异步后台解帧与有界 LRU 内存缓存。"""

    def __init__(
        self,
        max_clips: int = _ANIM_CACHE_MAX_CLIPS,
        max_workers: int = 2,
    ) -> None:
        self._max_clips = max(1, int(max_clips))
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, int], _DecodedFrameBuffer] = OrderedDict()
        self._inflight: set[tuple[str, int]] = set()
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="PetWebpDecode")
        self._disposed = False

    def get(self, file_path: str, max_height: int) -> Optional[_DecodedFrameBuffer]:
        key = (file_path, max_height)
        with self._lock:
            buf = self._entries.get(key)
            if buf is not None:
                self._entries.move_to_end(key)
            return buf

    def request_async(
        self,
        file_path: str,
        max_height: int,
        fallback_fps: float,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        key = (file_path, max_height)
        with self._lock:
            if self._disposed or key in self._entries or key in self._inflight:
                return
            self._inflight.add(key)

        def _worker() -> None:
            decoded = _decode_webp_images_from_disk(file_path, max_height, fallback_fps)
            with self._lock:
                self._inflight.discard(key)
                if decoded is not None and not self._disposed:
                    self._entries[key] = decoded
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_clips:
                        self._entries.popitem(last=False)
            if decoded is not None and on_ready is not None:
                try:
                    on_ready()
                except Exception:
                    pass

        self._executor.submit(_worker)

    def decode_sync(
        self,
        file_path: str,
        max_height: int,
        fallback_fps: float,
    ) -> Optional[_DecodedFrameBuffer]:
        existing = self.get(file_path, max_height)
        if existing is not None:
            return existing
        decoded = _decode_webp_images_from_disk(file_path, max_height, fallback_fps)
        if decoded is None:
            return None
        key = (file_path, max_height)
        with self._lock:
            if not self._disposed:
                self._entries[key] = decoded
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_clips:
                    self._entries.popitem(last=False)
        return decoded

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def dispose(self) -> None:
        with self._lock:
            self._disposed = True
            self._entries.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)


def probe_animated_webp(file_path: str) -> Optional[tuple[int, QSize]]:
    """轻量探测 .webp 文件帧数与尺寸（仅读头信息并立即释放文件句柄）。"""
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return None
    ba = QByteArray(raw)
    buf = QBuffer(ba)
    if not buf.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        reader = QImageReader(buf)
        if not reader.canRead():
            return None
        count = int(reader.imageCount())
        size = reader.size()
        return (count, size)
    finally:
        buf.close()


class AnimatedWebpClip(ActionClip):
    """基于 PySide6 原生 QImageReader 的透明动态 WebP 动作片段（后台解帧 + LRU 缓存）。"""

    def __init__(
        self,
        clip_id: str,
        file_path: str,
        cache: AnimatedClipCache,
        *,
        frame_count: int,
        natural_size: QSize,
        fallback_pixmap: Optional[QPixmap] = None,
        metadata: Optional[ClipMetadata] = None,
        on_decoded: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(clip_id, metadata)
        self.file_path = file_path
        self._cache = cache
        self._frame_count = max(1, int(frame_count))
        self._natural_size = natural_size
        self._fallback_pixmap = fallback_pixmap
        self._preview_cache: Optional[QPixmap] = None
        self._on_decoded = on_decoded
        self._last_buffer_height = 0

    @property
    def is_animated(self) -> bool:
        return self._frame_count > 1

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def _effective_decode_height(self, target_height: int) -> int:
        if target_height <= 0:
            return _ANIM_MAX_DECODE_HEIGHT
        # 按 64px 档位量化，避免窗口微小缩放时重复解码
        quantized = int(((target_height + 63) // 64) * 64)
        return max(192, min(_ANIM_MAX_DECODE_HEIGHT, quantized))

    @property
    def duration_sec(self) -> float:
        if self.metadata.duration_sec > 0.0:
            return float(self.metadata.duration_sec)
        buf = self._cache.get(self.file_path, self._last_buffer_height or _ANIM_MAX_DECODE_HEIGHT)
        if buf is not None:
            return buf.total_ms / 1000.0
        fps = max(1.0, float(self.metadata.fps or 24.0))
        return self._frame_count / fps

    def _get_or_request_buffer(self, target_height: int) -> Optional[_DecodedFrameBuffer]:
        decode_h = self._effective_decode_height(target_height)
        buf = self._cache.get(self.file_path, decode_h)
        if buf is not None:
            self._last_buffer_height = decode_h
            return buf
        self._cache.request_async(
            self.file_path,
            decode_h,
            self.metadata.fps,
            on_ready=self._on_decoded,
        )
        # 若此前有另一档清晰度在缓存中，先继续使用避免切换缩放时闪烁
        if self._last_buffer_height and self._last_buffer_height != decode_h:
            prev_buf = self._cache.get(self.file_path, self._last_buffer_height)
            if prev_buf is not None:
                return prev_buf
        return None

    def ensure_loaded_sync(self, target_height: int = _ANIM_MAX_DECODE_HEIGHT) -> bool:
        """同步解码到缓存（供单测或首帧强同步场景使用）。"""
        decode_h = self._effective_decode_height(target_height)
        buf = self._cache.decode_sync(self.file_path, decode_h, self.metadata.fps)
        if buf is not None:
            self._last_buffer_height = decode_h
            return True
        return False

    def _pixmap_for_index(self, buf: _DecodedFrameBuffer, index: int) -> QPixmap:
        idx = max(0, min(len(buf.images) - 1, index))
        pm = buf.pixmaps[idx]
        if pm is None:
            pm = QPixmap.fromImage(buf.images[idx])
            buf.pixmaps[idx] = pm
        return pm

    def frame_at(self, elapsed_sec: float, target_height: int = 0) -> Optional[QPixmap]:
        buf = self._get_or_request_buffer(target_height)
        if buf is None:
            return self.preview_pixmap()

        elapsed_ms = max(0.0, elapsed_sec * 1000.0)
        if self.metadata.loop:
            t_ms = elapsed_ms % float(buf.total_ms)
        else:
            t_ms = min(elapsed_ms, float(buf.total_ms - 1))

        idx = bisect_right(buf.cumulative_ms, t_ms)
        if idx >= len(buf.images):
            idx = len(buf.images) - 1
        return self._pixmap_for_index(buf, idx)

    def preview_pixmap(self) -> Optional[QPixmap]:
        if self._fallback_pixmap is not None and not self._fallback_pixmap.isNull():
            return self._fallback_pixmap
        if self._preview_cache is not None and not self._preview_cache.isNull():
            return self._preview_cache
        # 仅解码首帧作预览，不触发全帧加载
        try:
            with open(self.file_path, "rb") as fh:
                raw = fh.read()
            ba = QByteArray(raw)
            buf = QBuffer(ba)
            if buf.open(QIODevice.OpenModeFlag.ReadOnly):
                try:
                    reader = QImageReader(buf)
                    img = reader.read()
                    if not img.isNull():
                        self._preview_cache = QPixmap.fromImage(img)
                        return self._preview_cache
                finally:
                    buf.close()
        except OSError:
            pass
        return None

    def content_rect(self, frame: Optional[QPixmap] = None) -> QRect:
        buf = self._cache.get(self.file_path, self._last_buffer_height or _ANIM_MAX_DECODE_HEIGHT)
        if buf is not None:
            if frame is None or (
                frame.width() == buf.images[0].width() and frame.height() == buf.images[0].height()
            ):
                return buf.content_rect
        return super().content_rect(frame)

    def anchor_x_ratio(self, frame: Optional[QPixmap] = None) -> float:
        if self.metadata.anchor_x_norm is not None:
            return max(0.0, min(1.0, float(self.metadata.anchor_x_norm)))
        buf = self._cache.get(self.file_path, self._last_buffer_height or _ANIM_MAX_DECODE_HEIGHT)
        if buf is not None:
            return buf.anchor_x_norm
        return super().anchor_x_ratio(frame)


def parse_clip_metadata(raw: dict[str, Any], base: Optional[ClipMetadata] = None) -> ClipMetadata:
    """将 manifest JSON 条目安全合并为 ClipMetadata。"""
    meta = base or ClipMetadata()
    if not isinstance(raw, dict):
        return meta

    def _opt_float(key: str, default: float) -> float:
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        return default

    anchor_val = raw.get("anchor_x", raw.get("anchor_x_norm"))
    anchor_x = (
        max(0.0, min(1.0, float(anchor_val)))
        if isinstance(anchor_val, (int, float)) and not isinstance(anchor_val, bool)
        else meta.anchor_x_norm
    )

    offset = raw.get("render_offset")
    off_x = meta.offset_x
    off_y = meta.offset_y
    if isinstance(offset, (list, tuple)) and len(offset) >= 2:
        if isinstance(offset[0], (int, float)):
            off_x = float(offset[0])
        if isinstance(offset[1], (int, float)):
            off_y = float(offset[1])
    off_x = _opt_float("offset_x", off_x)
    off_y = _opt_float("offset_y", off_y)

    loop_val = raw.get("loop")
    loop = bool(loop_val) if isinstance(loop_val, bool) else meta.loop

    facing_raw = raw.get("source_facing")
    facing = meta.source_facing
    if isinstance(facing_raw, str):
        facing = -1 if facing_raw.strip().lower() == "left" else 1
    elif isinstance(facing_raw, (int, float)) and not isinstance(facing_raw, bool):
        facing = -1 if facing_raw < 0 else 1

    enter_file = str(raw.get("enter_file", meta.enter_file) or "").strip()
    exit_file = str(raw.get("exit_file", meta.exit_file) or "").strip()

    return replace(
        meta,
        render_scale=max(0.25, min(2.0, _opt_float("render_scale", meta.render_scale))),
        offset_x=max(-0.5, min(0.5, off_x)),
        offset_y=max(-0.5, min(0.5, off_y)),
        anchor_x_norm=anchor_x,
        fps=max(1.0, min(60.0, _opt_float("fps", meta.fps))),
        duration_sec=max(0.0, _opt_float("duration_sec", meta.duration_sec)),
        loop=loop,
        move_start_ratio=max(0.0, min(1.0, _opt_float("move_start_ratio", meta.move_start_ratio))),
        move_stop_ratio=max(0.0, min(1.0, _opt_float("move_stop_ratio", meta.move_stop_ratio))),
        source_facing=facing,
        enter_file=enter_file,
        exit_file=exit_file,
    )


def load_manifest_entries(manifest_path: str) -> dict[str, dict[str, Any]]:
    """读取可选动作清单 manifest.json，文件不存在或格式异常时静默返回空字典。"""
    if not os.path.isfile(manifest_path):
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    clips = data.get("clips", data)
    if not isinstance(clips, dict):
        return {}
    return {
        str(k).strip().lower(): v
        for k, v in clips.items()
        if isinstance(k, str) and isinstance(v, dict)
    }
