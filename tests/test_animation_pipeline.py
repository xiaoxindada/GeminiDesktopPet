"""核心物理曲线、透明动态 WebP 片段、归一化重心与 A/B 交叉淡化测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QImageWriter, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from models.pet_settings import DockSide, PetSettings, PetStyleMode
from models.pet_state import PetState
from services.action_clip import (
    AnimatedWebpClip,
    StaticSpriteClip,
    analyze_pixmap_metrics,
    analyze_qimage_metrics,
    prime_pixmap_metrics,
)
from services.gemini_chat_service import GeminiChatService
from services.pet_physics import (
    DragTrailEstimator,
    ParticleSystem,
    PetMotionEngine,
    PoseMotionInput,
    SquashSpring,
    ThrowBodyState,
    TransformDamper,
    TransformSnapshot,
    WalkLocomotionController,
    WalkLocomotionState,
    compute_chibi_walk_kinematics,
    compute_std_walk_kinematics,
    landing_squash_depth,
    spring_step_1d,
    squash_scale_xy,
    step_throw_physics,
)
from services.sound_service import SoundService
from services.sprite_manager import SpriteManager
from services.stage_renderer import (
    StageCrossfader,
    VisualRenderSlot,
    compute_local_sprite_rect,
)
from services.system_monitor_service import SystemMonitorService
from views.pet_window import PetWindow


def _write_animated_webp(path: Path, frame_colors: list[tuple[QColor, int]], size: int = 48) -> None:
    """使用 Qt 内置 lossless VP8L 构造标准多帧透明动态 WebP，零外部依赖。"""
    def _encode_vp8l(color: QColor) -> bytes:
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        painter.fillRect(size // 4, size // 6, size // 2, int(size * 0.75), color)
        painter.end()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        writer = QImageWriter(buf, b"webp")
        writer.setQuality(100)
        assert writer.write(img)
        raw = bytes(buf.data())
        buf.close()
        return raw[12:]

    def _pad(tag: bytes, payload: bytes) -> bytes:
        return tag + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")

    def _u24(v: int) -> bytes:
        return struct.pack("<I", v)[:3]

    anmf_chunks = []
    for color, dur_ms in frame_colors:
        chunk = _encode_vp8l(color)
        hdr = _u24(0) + _u24(0) + _u24(size - 1) + _u24(size - 1) + _u24(dur_ms) + bytes([0x03])
        anmf_chunks.append(_pad(b"ANMF", hdr + chunk))

    vp8x = struct.pack("<B", 0x12) + b"\x00\x00\x00" + _u24(size - 1) + _u24(size - 1)
    anim = struct.pack("<IH", 0, 0)
    body = b"WEBP" + _pad(b"VP8X", vp8x) + _pad(b"ANIM", anim) + b"".join(anmf_chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def _write_solid_png(path: Path, size: tuple[int, int], fill_rect: tuple[int, int, int, int], color: QColor) -> None:
    img = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    painter = QPainter(img)
    painter.fillRect(*fill_rect, color)
    painter.end()
    assert img.save(str(path), "PNG")


class TestPetPhysics(unittest.TestCase):
    def test_squash_curve_compresses_then_overshoots_and_returns(self) -> None:
        sx0, sy0 = squash_scale_xy(0.0, 0.24)
        self.assertEqual((sx0, sy0), (1.0, 1.0))

        # 下压最低点：纵向变矮、横向舒展
        sx_mid, sy_mid = squash_scale_xy(0.42, 0.24)
        self.assertLess(sy_mid, 0.80)
        self.assertGreater(sx_mid, 1.12)

        # 回弹过冲段：纵向略超过 1.0 带来果冻感
        sx_reb, sy_reb = squash_scale_xy(0.76, 0.24)
        self.assertGreater(sy_reb, 1.0)
        self.assertLess(sx_reb, 1.0)

        sx1, sy1 = squash_scale_xy(1.0, 0.24)
        self.assertEqual((sx1, sy1), (1.0, 1.0))
        self.assertGreater(landing_squash_depth(1400.0), landing_squash_depth(200.0))

        # 校验挤压曲线全程 C1 光滑：相邻步进二阶差分无剧烈速度断层
        samples = [squash_scale_xy(i / 60.0, 0.20)[1] for i in range(61)]
        first_diffs = [samples[i + 1] - samples[i] for i in range(len(samples) - 1)]
        max_second_diff = max(abs(first_diffs[i + 1] - first_diffs[i]) for i in range(len(first_diffs) - 1))
        self.assertLess(max_second_diff, 0.015)

    def test_squash_spring_absorbs_double_impact_without_snap_jump(self) -> None:
        spring = SquashSpring()
        spring.trigger_stretch(0.06)
        prev_sy = 1.0
        for _ in range(12):
            _, sy = spring.step(1.0 / 60.0)
            self.assertLess(abs(sy - prev_sy), 0.05)
            prev_sy = sy

        # 尚未静止时再次触地注入下压冲量，位移必须连续过渡而非瞬时跳回 1.0
        spring.trigger_squash(0.16)
        min_sy = 1.0
        max_sy = 1.0
        for _ in range(45):
            _, sy = spring.step(1.0 / 60.0)
            self.assertLess(abs(sy - prev_sy), 0.08)
            min_sy = min(min_sy, sy)
            max_sy = max(max_sy, sy)
            prev_sy = sy
        self.assertLess(min_sy, 0.90)
        self.assertGreater(max_sy, 1.01)
        self.assertFalse(spring.is_active)

    def test_motion_engine_unifies_walk_rig_poke_springs_and_particles(self) -> None:
        engine = PetMotionEngine()
        engine.push_drag_velocity_x(1000.0)
        self.assertGreater(engine.drag_tilt_vx, 0.0)
        engine.decay_drag_tilt(0.05)
        self.assertGreater(engine.drag_tilt_vx, 0.0)
        self.assertLess(engine.drag_tilt_vx, 280.0)

        engine.trigger_poke(0.12, -1.0)
        engine.set_subpixel_offset(0.35, -0.25)
        engine.damper.step_walk_blend(0.5, walking=True)
        snap = engine.evaluate(
            PoseMotionInput(
                anim_time=0.4,
                dt=1.0 / 60.0,
                scale=1.0,
                docked_side=DockSide.NONE,
                current_state=PetState.RUNNING,
                override_pose=None,
                is_dragging=False,
                is_sync_working_locked=False,
                is_walking=True,
                walk_dir=1,
                mouse_down=False,
                is_falling=False,
                throw_vx=0.0,
                is_sleeping=False,
                is_dozing=False,
                anti_gravity_mode=False,
                is_animated_clip=False,
                use_chibi_walk_rig=True,
                chibi_rig_complete=True,
            )
        )
        self.assertEqual(snap.facing_sign, -1.0)
        self.assertNotEqual(snap.angle, 0.0)
        self.assertAlmostEqual(snap.trans_x, 0.35, delta=0.05)
        self.assertAlmostEqual(snap.trans_y, -0.25, delta=0.05)
        self.assertNotEqual(engine.chibi_kinematics["leg_l_rot"], 0.0)

        ps = ParticleSystem(max_particles=8)
        ps.spawn_star_burst(100.0, 100.0, 4)
        self.assertEqual(len(ps.particles), 4)
        ps.step(0.5)
        self.assertEqual(len(ps.particles), 4)
        ps.step(1.0)
        self.assertEqual(len(ps.particles), 0)

    def test_overdamped_spring_converges_without_overshoot(self) -> None:
        pos, vel = 0.0, 0.0
        for _ in range(45):
            pos, vel = spring_step_1d(vel, pos, 200.0, 1.0 / 60.0)
            self.assertLessEqual(pos, 200.5)
        self.assertAlmostEqual(pos, 200.0, delta=0.5)
        self.assertEqual(vel, 0.0)

    def test_drag_trail_gentle_pause_vs_flick_throw(self) -> None:
        est = DragTrailEstimator()
        t0 = 10.0
        for i in range(6):
            est.add_sample(100.0 + i * 30.0, 200.0 - i * 10.0, t0 + i * 0.016)
        vx, vy = est.estimate_release_velocity(t0 + 5 * 0.016 + 0.01)
        self.assertGreater(vx, 800.0)
        self.assertLess(vy, 0.0)

        # 松手前停顿超过阈值视为温柔放下
        vx_still, vy_still = est.estimate_release_velocity(t0 + 5 * 0.016 + 0.30)
        self.assertEqual((vx_still, vy_still), (0.0, 0.0))

        # 甩动幅度与力度应单调区分：短轻甩 < 中等抛掷 < 大幅大力甩飞；末段刹停与微抖均归零
        def sample_impulse(points: list[tuple[float, float, float]]):
            tr = DragTrailEstimator()
            t = 20.0
            for x, y, dt in points:
                t += dt
                tr.add_sample(x, y, t)
            return tr.estimate_release_impulse(t + 0.01)

        brake_imp = sample_impulse(
            [(200.0 + i * 24.0, 300.0, 0.016) for i in range(9)]
            + [(392.0 + i * 1.2, 300.0, 0.016) for i in range(1, 5)]
        )
        self.assertFalse(brake_imp.is_throw)
        jitter_imp = sample_impulse([(400.0, 300.0, 0.0), (407.0, 298.0, 0.016), (414.0, 295.0, 0.016)])
        self.assertFalse(jitter_imp.is_throw)

        light_imp = sample_impulse([(400.0 + i * 11.0, 360.0 - i * 4.0, 0.016) for i in range(7)])
        med_imp = sample_impulse([(400.0 + i * 22.0, 360.0 - i * 10.0, 0.016) for i in range(9)])
        hard_imp = sample_impulse([(200.0 + i * 45.0, 420.0 - i * 20.0, 0.016) for i in range(11)])
        self.assertTrue(light_imp.is_throw)
        self.assertLess(light_imp.speed, med_imp.speed)
        self.assertLess(med_imp.speed, hard_imp.speed)
        self.assertLess(light_imp.intensity, med_imp.intensity)
        self.assertLess(med_imp.intensity, hard_imp.intensity)
        self.assertLess(light_imp.amplitude_px, hard_imp.amplitude_px)

    def test_throw_physics_wall_bounce_and_settle(self) -> None:
        state = ThrowBodyState(x=20.0, y=100.0, vx=-900.0, vy=-100.0)
        bounced = False
        landed = False
        max_wall_imp = 0.0
        for _ in range(180):
            res = step_throw_physics(
                state,
                1.0 / 60.0,
                min_x=0.0,
                max_x=500.0,
                min_y=0.0,
                floor_y=300.0,
            )
            state = ThrowBodyState(res.x, res.y, res.vx, res.vy)
            bounced = bounced or res.bounced_wall
            max_wall_imp = max(max_wall_imp, res.wall_impact_speed)
            if res.landed_impact_vy > 0.0:
                landed = True
            if res.at_rest:
                break
        self.assertTrue(bounced)
        self.assertGreater(max_wall_imp, 700.0)
        self.assertTrue(landed)
        self.assertAlmostEqual(state.y, 300.0, delta=0.5)
        self.assertEqual(state.vx, 0.0)

        # 力度联动弹跳与惯性反馈：大力下砸与大幅甩动比轻甩弹跳更高且触地压扁→回弹拉伸、滑行后仰
        def run_bounce_stats(vx: float, vy: float, energy: float) -> tuple[int, float, bool]:
            st = ThrowBodyState(x=300.0, y=160.0, vx=vx, vy=vy)
            bounces = 0
            peak_h = 0.0
            in_b = False
            saw_slide = False
            for _ in range(260):
                r = step_throw_physics(
                    st,
                    1.0 / 60.0,
                    min_x=0.0,
                    max_x=1200.0,
                    min_y=0.0,
                    floor_y=400.0,
                    bounce_energy=energy,
                )
                st = ThrowBodyState(r.x, r.y, r.vx, r.vy)
                if r.bounced_floor:
                    bounces += 1
                    in_b = True
                elif in_b:
                    peak_h = max(peak_h, 400.0 - r.y)
                if r.is_sliding_on_floor:
                    saw_slide = True
                if r.at_rest:
                    break
            return (bounces, peak_h, saw_slide)

        gentle_b, _, _ = run_bounce_stats(0.0, 0.0, 0.16)
        self.assertEqual(gentle_b, 0)
        small_b, small_h, small_slide = run_bounce_stats(320.0, -100.0, 0.25)
        self.assertEqual(small_b, 0)
        self.assertEqual(small_h, 0.0)
        self.assertTrue(small_slide)
        med_b, med_h, med_slide = run_bounce_stats(980.0, -320.0, 0.56)
        hard_b, hard_h, hard_slide = run_bounce_stats(1800.0, -620.0, 1.12)
        slam_b, slam_h, _ = run_bounce_stats(0.0, 1450.0, 0.95)
        self.assertGreaterEqual(med_b, 1)
        self.assertLess(med_h, 45.0)
        self.assertGreater(hard_h, med_h * 2.0)
        self.assertGreaterEqual(hard_b, med_b)
        self.assertTrue(med_slide and hard_slide)
        self.assertGreaterEqual(slam_b, 1)
        self.assertGreater(slam_h, 80.0)
        self.assertLess(slam_h, 190.0)

        # 验证次级物理反馈：触地弹跳先压扁再回弹拉伸，地面滑行呈抗制动后仰角
        eng = PetMotionEngine()
        eng.trigger_floor_impact(1200.0, 600.0, bounced=True, intensity=0.8)
        _, sy0 = eng.squash_spring.step(0.0)
        self.assertLess(sy0, 0.96)
        max_sy = sy0
        for _ in range(20):
            _, sy = eng.squash_spring.step(1.0 / 60.0)
            max_sy = max(max_sy, sy)
        self.assertGreater(max_sy, 1.03)

        eng_slide = PetMotionEngine()
        snap_slide = eng_slide.evaluate(
            PoseMotionInput(
                anim_time=1.0,
                dt=0.25,
                scale=1.0,
                docked_side=DockSide.NONE,
                current_state=PetState.IDLE,
                override_pose=None,
                is_dragging=False,
                is_sync_working_locked=False,
                is_walking=False,
                walk_dir=1,
                mouse_down=False,
                is_falling=True,
                throw_vx=650.0,
                throw_vy=0.0,
                is_ground_sliding=True,
                is_sleeping=False,
                is_dozing=False,
                anti_gravity_mode=False,
                is_animated_clip=False,
            )
        )
        self.assertLess(snap_slide.angle, -2.0)

    def test_transform_damper_keeps_facing_sign_separate_from_scale(self) -> None:
        damper = TransformDamper()
        damper.step(facing_sign=1.0, scale_x=1.0, scale_y=1.0, angle=0.0, trans_x=0.0, trans_y=0.0, dt=0.016, snap=True)
        mid = damper.step(facing_sign=-1.0, scale_x=1.05, scale_y=0.95, angle=10.0, trans_x=0.0, trans_y=-16.0, dt=0.016)
        self.assertEqual(mid.facing_sign, -1.0)
        self.assertGreater(mid.scale_x, 0.99)
        self.assertGreater(mid.angle, 0.0)
        self.assertLess(mid.angle, 10.0)

        # dt=0 时（如姿态切换瞬间触发的无时间步刷新）不得硬切跳变，保持当前阻尼姿态等待帧循环缓动
        held = damper.step(facing_sign=1.0, scale_x=1.0, scale_y=1.0, angle=0.0, trans_x=0.0, trans_y=0.0, dt=0.0)
        self.assertEqual(held.facing_sign, 1.0)
        self.assertAlmostEqual(held.angle, mid.angle, delta=1e-6)
        self.assertAlmostEqual(held.trans_y, mid.trans_y, delta=1e-6)

    def test_walk_locomotion_controller_and_grounded_gait(self) -> None:
        ctrl = WalkLocomotionController()
        # 靠近左边界启动时应优先向右开阔区迈步
        chosen = ctrl.start(None, x=20.0, min_x=0.0, max_x=800.0, scale=1.0, pace_factor=1.0)
        self.assertEqual(chosen, 1)
        self.assertEqual(ctrl.state, WalkLocomotionState.WALKING)

        # 巡航加速后应达到协调的自然步行节奏（标准立绘约 90 px/s、约 1.07 周期/秒）且相位与速度正相关
        x = 120.0
        phase_total = 0.0
        prev_phase = ctrl.stride_phase
        for _ in range(60):  # 1.0s @ 60fps
            res = ctrl.step(1.0 / 60.0, x=x, min_x=0.0, max_x=800.0, scale=1.0, is_standard=True)
            x = res.x
            delta_p = (res.stride_phase - prev_phase) % (2.0 * math.pi)
            phase_total += delta_p
            prev_phase = res.stride_phase
        self.assertGreater(res.velocity_x, 80.0)
        self.assertLess(res.velocity_x, 105.0)
        self.assertGreater(phase_total / (2.0 * math.pi), 0.90)
        self.assertLess(phase_total / (2.0 * math.pi), 1.25)

        # 接近右边界时进入柔和减速转身，而非 1 帧硬翻转
        turned = False
        saw_turning = False
        x = 760.0
        for _ in range(75):
            res = ctrl.step(1.0 / 60.0, x=x, min_x=0.0, max_x=800.0, scale=1.0, is_standard=True)
            x = res.x
            if ctrl.state == WalkLocomotionState.TURNING:
                saw_turning = True
            if res.direction == -1 and res.velocity_x < -15.0:
                turned = True
                break
        self.assertTrue(saw_turning)
        self.assertTrue(turned)

        # 请求优雅收步应平滑减速并在归正后触发 finished_stop
        ctrl.request_stop()
        stopped = False
        for _ in range(55):
            res = ctrl.step(1.0 / 60.0, x=x, min_x=0.0, max_x=800.0, scale=1.0, is_standard=True)
            x = res.x
            if res.finished_stop:
                stopped = True
                break
        self.assertTrue(stopped)
        self.assertFalse(ctrl.is_active)

        # 标准立绘 C∞ 平滑步态：摆动期柔和折膝、对侧摆臂与平滑无折角质心起伏
        k_contact = compute_std_walk_kinematics(0.0, 1.0, 1.0, 1.0)
        k_pass = compute_std_walk_kinematics(math.pi * 0.42, 1.0, 1.0, 1.0)
        self.assertGreater(k_contact["front_hip"], 14.0)
        self.assertLess(k_contact["front_arm_rot"], -10.0)  # 左腿前迈时左臂向后摆
        self.assertGreater(k_contact["back_arm_rot"], 15.0)  # 右臂向前探
        self.assertLess(k_pass["back_knee"], -30.0)
        # 尾巴应随步态周期柔和摆动
        std_tail_vals = [
            compute_std_walk_kinematics(2.0 * math.pi * i / 16.0, 1.0, 1.0, 1.0)["tail_rot"]
            for i in range(16)
        ]
        self.assertGreater(max(std_tail_vals) - min(std_tail_vals), 12.0)
        # 连续采样整周期验证 hip_ty 无突跳（消除“噔噔感”）
        prev_ty = compute_std_walk_kinematics(0.0, 1.0, 1.0, 1.0)["hip_ty"]
        max_step_ty = 0.0
        for i in range(1, 65):
            cur_ty = compute_std_walk_kinematics(2.0 * math.pi * i / 64.0, 1.0, 1.0, 1.0)["hip_ty"]
            max_step_ty = max(max_step_ty, abs(cur_ty - prev_ty))
            prev_ty = cur_ty
        self.assertLess(max_step_ty, 0.4)

        # Q 版步态：前后半步均能对称打开并带柔和起伏、骨盆位移、对侧摆臂与猫尾摆动
        k_c1 = compute_chibi_walk_kinematics(math.pi * 0.5, 1.0, 1.0, 1.0)
        k_c2 = compute_chibi_walk_kinematics(math.pi * 1.5, 1.0, 1.0, 1.0)
        self.assertGreater(k_c1["leg_l_rot"], 10.0)
        self.assertGreater(k_c2["leg_r_rot"], 20.0)
        self.assertLess(k_c2["leg_r_tx"], -7.0)
        self.assertLess(k_c1["arm_l_rot"], -15.0)
        self.assertGreater(k_c1["arm_r_rot"], 15.0)
        chibi_tail_vals = [
            compute_chibi_walk_kinematics(2.0 * math.pi * i / 16.0, 1.0, 1.0, 1.0)["tail_rot"]
            for i in range(16)
        ]
        self.assertGreater(max(chibi_tail_vals) - min(chibi_tail_vals), 16.0)


class TestActionClipAndStagePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_visual_anchor_centers_on_body_despite_side_prop(self) -> None:
        # 左侧厚实躯干 + 右侧细长外伸道具：重心应偏向躯干（< 0.45）而非包围盒中点 0.5
        img = QImage(120, 120, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        painter.fillRect(10, 20, 40, 95, QColor(120, 90, 220, 255))
        painter.fillRect(50, 50, 65, 6, QColor(250, 200, 90, 255))
        painter.end()
        metrics = analyze_pixmap_metrics(QPixmap.fromImage(img))
        self.assertLess(metrics.anchor_x_norm, 0.45)

    def test_animated_webp_clip_and_manifest_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Assets"
            sprites_dir = root / "Sprites"
            std_dir = root / "Standard"
            anim_sprites = root / "Animations" / "Sprites"
            sprites_dir.mkdir(parents=True)
            std_dir.mkdir(parents=True)
            anim_sprites.mkdir(parents=True)

            _write_solid_png(sprites_dir / "idle.png", (64, 64), (16, 8, 32, 52), QColor(100, 100, 255, 255))
            _write_solid_png(sprites_dir / "happy.png", (64, 64), (16, 8, 32, 52), QColor(255, 180, 100, 255))
            _write_solid_png(std_dir / "standee.png", (64, 100), (16, 8, 32, 88), QColor(140, 100, 255, 255))

            _write_animated_webp(
                anim_sprites / "running.webp",
                [
                    (QColor(255, 40, 40, 220), 60),
                    (QColor(40, 220, 60, 220), 60),
                    (QColor(40, 80, 255, 220), 60),
                ],
                size=48,
            )
            (sprites_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "clips": {
                            "running": {
                                "render_scale": 0.95,
                                "move_start_ratio": 0.2,
                                "move_stop_ratio": 0.8,
                                "source_facing": "right",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            mgr = SpriteManager(assets_root=str(root))
            try:
                mgr.preload_all()
                self.assertTrue(mgr.has_animated_clip(PetState.RUNNING, PetStyleMode.CHIBI))
                self.assertFalse(mgr.has_animated_clip(PetState.IDLE, PetStyleMode.CHIBI))

                run_clip = mgr.get_clip(PetState.RUNNING, PetStyleMode.CHIBI)
                self.assertIsInstance(run_clip, AnimatedWebpClip)
                assert isinstance(run_clip, AnimatedWebpClip)
                self.assertAlmostEqual(run_clip.metadata.render_scale, 0.95)
                self.assertEqual(run_clip.metadata.source_facing, 1)
                self.assertAlmostEqual(run_clip.metadata.move_start_ratio, 0.2)
                self.assertTrue(run_clip.ensure_loaded_sync())

                f0 = run_clip.frame_at(0.01)
                f1 = run_clip.frame_at(0.08)
                f2 = run_clip.frame_at(0.14)
                self.assertIsNotNone(f0)
                self.assertIsNotNone(f1)
                self.assertIsNotNone(f2)
                assert f0 is not None and f1 is not None and f2 is not None
                # 校验真实透明底与逐帧颜色切换
                c0 = f0.toImage().pixelColor(f0.width() // 2, f0.height() // 2)
                c1 = f1.toImage().pixelColor(f1.width() // 2, f1.height() // 2)
                c2 = f2.toImage().pixelColor(f2.width() // 2, f2.height() // 2)
                self.assertEqual(f0.toImage().pixelColor(0, 0).alpha(), 0)
                self.assertGreater(c0.red(), 200)
                self.assertGreater(c1.green(), 180)
                self.assertGreater(c2.blue(), 200)

                # 静态图保持原有 QPixmap 引用语义
                idle_clip = mgr.get_clip(PetState.IDLE, PetStyleMode.CHIBI)
                self.assertIsInstance(idle_clip, StaticSpriteClip)
                self.assertIs(idle_clip.preview_pixmap(), mgr.get(PetState.IDLE, PetStyleMode.CHIBI))

                # PetWindow 遇到动态 running.webp 时应优先播放动态片段而非刚体 WalkRig
                settings = PetSettings(auto_walk=True, chat_sync_enabled=False, show_speech_bubble=False)
                service = Mock(settings_path=Path(tmp) / "settings.json")
                pet = PetWindow(
                    settings,
                    service,
                    mgr,
                    SoundService(),
                    SystemMonitorService(),
                    GeminiChatService(),
                )
                try:
                    pet._is_walking = True
                    pet._current_state = PetState.RUNNING
                    pet._override_pose = None
                    pet._refresh_sprite_visual()
                    self.assertFalse(pet._use_chibi_walk_rig)
                    self.assertIs(pet._active_clip, run_clip)

                    # 渲染首帧并切换姿态，确认触发 A/B 交叉淡化且离屏绘制无异常
                    canvas = QPixmap(pet.width(), pet.height())
                    canvas.fill(QColor(0, 0, 0, 0))
                    p = QPainter(canvas)
                    pet._paint_stage(p)
                    p.end()

                    pet._is_walking = False
                    pet._current_state = PetState.HAPPY
                    pet._refresh_sprite_visual()
                    self.assertTrue(pet._crossfader.is_active)

                    p2 = QPainter(canvas)
                    pet._paint_stage(p2)
                    p2.end()
                finally:
                    pet.close()
            finally:
                mgr.dispose()

    def test_crossfader_maintains_high_combined_alpha_and_skips_style_switch(self) -> None:
        fader = StageCrossfader(duration_sec=0.16)
        slot_a = VisualRenderSlot(
            slot_key="chibi:idle",
            clip=None,
            pixmap=None,
            use_chibi_rig=False,
            use_std_rig=False,
            sprite_rect=QRectF(0, 0, 100, 100),
            transform=TransformSnapshot(),
            chibi_kinematics={},
            std_kinematics={},
            docked_side=DockSide.NONE,
            style_mode=PetStyleMode.CHIBI,
        )
        fader.notify_transition(
            new_key="chibi:idle",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=None,
        )
        fader.notify_transition(
            new_key="chibi:happy",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=slot_a,
        )
        self.assertTrue(fader.is_active)
        fader.advance(0.08)
        out_a, in_a = fader.opacities()
        combined = out_a + in_a * (1.0 - out_a)
        self.assertGreaterEqual(combined, 0.90)
        # 中段旧图必须快速衰减且新图主导清晰，避免浓重双重曝光残影
        self.assertLessEqual(out_a, 0.30)
        self.assertGreaterEqual(in_a, 0.90)

        # 站姿↔蹲姿等大幅轮廓高度变化或跨骨骼切换应直接清槽，避免悬空/双头残影
        fader.notify_transition(
            new_key="chibi:sulk",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=slot_a,
            new_sprite_rect=QRectF(0, 32, 100, 68),
        )
        self.assertFalse(fader.is_active)
        fader.notify_transition(
            new_key="rig:chibi",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=slot_a,
            new_use_rig=True,
        )
        self.assertFalse(fader.is_active)

        # 切换画风、窗口尺寸或拖拽状态变化时直接清空淡化，避免错位重影
        fader.notify_transition(
            new_key="standard:happy",
            new_style=PetStyleMode.STANDARD,
            new_dock=DockSide.NONE,
            previous_slot=slot_a,
        )
        self.assertFalse(fader.is_active)

        slot_drag = VisualRenderSlot(
            slot_key="chibi:float",
            clip=None,
            pixmap=None,
            use_chibi_rig=False,
            use_std_rig=False,
            sprite_rect=QRectF(90, 200, 180, 240),
            transform=TransformSnapshot(),
            chibi_kinematics={},
            std_kinematics={},
            docked_side=DockSide.NONE,
            style_mode=PetStyleMode.CHIBI,
            stage_size=(380, 440),
            is_dragging=True,
        )
        fader.notify_transition(
            new_key="chibi:float",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=None,
            stage_size=(380, 440),
            is_dragging=True,
        )
        fader.notify_transition(
            new_key="chibi:annoyed",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=slot_drag,
            stage_size=(380, 440),
            is_dragging=True,
        )
        self.assertTrue(fader.is_active)
        fader.translate_outgoing(0.0, -20.0)
        assert fader.outgoing is not None
        self.assertAlmostEqual(fader.outgoing.sprite_rect.y(), 180.0, delta=1e-4)

        # 拖拽松手切常态时不保留旧淡出槽
        fader.notify_transition(
            new_key="chibi:idle",
            new_style=PetStyleMode.CHIBI,
            new_dock=DockSide.NONE,
            previous_slot=slot_drag,
            stage_size=(380, 440),
            is_dragging=False,
        )
        self.assertFalse(fader.is_active)

    def test_drag_pose_switch_anchor_and_lost_grab_recovery(self) -> None:
        mgr = SpriteManager()
        try:
            mgr.preload_all()
            with tempfile.TemporaryDirectory() as tmp:
                settings = PetSettings(
                    style_mode=PetStyleMode.STANDARD,
                    auto_walk=False,
                    gravity_enabled=False,
                    chat_sync_enabled=False,
                    show_speech_bubble=False,
                )
                service = Mock(settings_path=Path(tmp) / "settings.json")
                pet = PetWindow(
                    settings,
                    service,
                    mgr,
                    SoundService(),
                    SystemMonitorService(),
                    GeminiChatService(),
                )
                try:
                    pet.move(300, 220)
                    pet._override_pose = PetState.SULK
                    pet._refresh_sprite_visual()
                    sulk_anchor_global_y = pet.y() + pet._get_local_drag_anchor().y()

                    # 蹲姿 SULK 起拖切到站姿 FLOAT 时，窗口应补偿纵向高度差以保证角色紧贴鼠标握点
                    pet._mouse_down = True
                    pet._mouse_pressed_physically = True
                    pet._drag_start_global = QPointF(490.0, 500.0)
                    pet._last_move_global = QPointF(490.0, 500.0)
                    pet._win_start_left = float(pet.x())
                    pet._win_start_top = float(pet.y())
                    pet._start_drag_at_global_pos(QPointF(498.0, 500.0))
                    float_anchor_global_y = pet.y() + pet._get_local_drag_anchor().y()
                    self.assertAlmostEqual(float_anchor_global_y, sulk_anchor_global_y, delta=2.0)

                    # 拖拽中切到 ANNOYED 时，横向躯干重心均为舞台中线，窗口不得发生多余横跳
                    x_before_annoyed = pet.x()
                    pet._override_pose = PetState.ANNOYED
                    pet._refresh_sprite_visual()
                    self.assertEqual(pet.x(), x_before_annoyed)

                    # 模拟气泡/焦点变化导致漏收 mouseReleaseEvent：物理按键已松开时看门狗须安全收尾拖拽
                    pet._is_primary_mouse_button_held = lambda: False  # type: ignore[method-assign]
                    pet._on_stage_mouse_grab_lost()
                    self.assertFalse(pet._mouse_down)
                    self.assertFalse(pet._is_dragging)
                    self.assertIsNot(pet._stage_canvas.mouseGrabber(), pet._stage_canvas)

                    # 贴边向左拖出时单次补偿窗口坐标，不重复叠加偏移
                    pet._settings.docked_side = DockSide.LEFT
                    pet._override_pose = None
                    pet._current_state = PetState.EDGE_LEFT
                    pet._refresh_sprite_visual()
                    pet.move(0, 200)
                    old_center_global_x = pet.x() + pet._get_local_sprite_rect().center().x()
                    pet._mouse_down = True
                    pet._drag_start_global = QPointF(60.0, 420.0)
                    pet._last_move_global = QPointF(60.0, 420.0)
                    pet._win_start_left = float(pet.x())
                    pet._win_start_top = float(pet.y())
                    pet._start_drag_at_global_pos(QPointF(72.0, 420.0))
                    new_anchor_global_x = pet.x() + pet._get_local_drag_anchor().x()
                    self.assertAlmostEqual(new_anchor_global_x, old_center_global_x + 12.0, delta=2.0)
                finally:
                    pet.close()
        finally:
            mgr.dispose()

    def test_compute_local_sprite_rect_applies_dock_slide(self) -> None:
        pm = QPixmap(100, 200)
        pm.fill(QColor(100, 120, 255, 255))
        r_start = compute_local_sprite_rect(
            stage_w=380.0,
            stage_h=440.0,
            sprite_h=240.0,
            docked_side=DockSide.LEFT,
            style_mode=PetStyleMode.CHIBI,
            use_std_walk_rig=False,
            use_chibi_walk_rig=False,
            std_rig_size=(100.0, 300.0),
            chibi_rig_size=(200.0, 240.0),
            active_clip=None,
            active_pixmap=pm,
            dock_progress=0.0,
        )
        r_end = compute_local_sprite_rect(
            stage_w=380.0,
            stage_h=440.0,
            sprite_h=240.0,
            docked_side=DockSide.LEFT,
            style_mode=PetStyleMode.CHIBI,
            use_std_walk_rig=False,
            use_chibi_walk_rig=False,
            std_rig_size=(100.0, 300.0),
            chibi_rig_size=(200.0, 240.0),
            active_clip=None,
            active_pixmap=pm,
            dock_progress=1.0,
        )
        self.assertLess(r_start.left(), 0.0)
        self.assertAlmostEqual(r_end.left(), 0.0, delta=0.1)

    def test_normal_drag_does_not_trigger_bristle_while_sustained_shake_progresses(self) -> None:
        mgr = SpriteManager()
        try:
            mgr.preload_all()
            with tempfile.TemporaryDirectory() as tmp:
                settings = PetSettings(
                    auto_walk=False,
                    gravity_enabled=False,
                    particles_enabled=False,
                    chat_sync_enabled=False,
                    show_speech_bubble=False,
                )
                service = Mock(settings_path=Path(tmp) / "settings.json")
                pet = PetWindow(
                    settings,
                    service,
                    mgr,
                    SoundService(),
                    SystemMonitorService(),
                    GeminiChatService(),
                )
                pet._frame_timer.stop()
                pet._ai_timer.stop()
                pet._idle_speech_timer.stop()
                pet.move(400, 300)
                clock = [100.0]

                def make_mouse(et: QEvent.Type, gpos: QPointF, btn: Qt.MouseButton) -> QMouseEvent:
                    button = (
                        btn
                        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
                        else Qt.MouseButton.NoButton
                    )
                    buttons = btn if et != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
                    return QMouseEvent(
                        et,
                        QPointF(100.0, 100.0),
                        gpos,
                        button,
                        buttons,
                        Qt.KeyboardModifier.NoModifier,
                    )

                def step_ticks(duration_sec: float, dt: float = 0.016) -> None:
                    steps = max(1, int(round(duration_sec / dt)))
                    for _ in range(steps):
                        clock[0] += dt
                        if (
                            pet._override_pose is not None
                            and pet._override_until_utc != datetime.max.replace(tzinfo=timezone.utc)
                        ):
                            pet._override_until_utc -= timedelta(seconds=dt)
                        pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=dt)
                        pet._frame_timer_tick()

                def drive_drag(
                    points: list[tuple[float, float, float]],
                    *,
                    release: bool = True,
                    stop_at_stage: int = 0,
                ) -> int:
                    max_stage = 0
                    p0 = QPointF(points[0][0], points[0][1])
                    pet._on_stage_mouse_press(
                        make_mouse(QEvent.Type.MouseButtonPress, p0, Qt.MouseButton.LeftButton)
                    )
                    last_tick = clock[0]
                    last_pt = p0
                    for x, y, dt in points[1:]:
                        clock[0] += dt
                        last_pt = QPointF(x, y)
                        pet._on_stage_mouse_move(
                            make_mouse(QEvent.Type.MouseMove, last_pt, Qt.MouseButton.LeftButton)
                        )
                        max_stage = max(max_stage, pet._shake_stage)
                        while clock[0] - last_tick >= 0.016:
                            last_tick += 0.016
                            if (
                                pet._override_pose is not None
                                and pet._override_until_utc != datetime.max.replace(tzinfo=timezone.utc)
                            ):
                                pet._override_until_utc -= timedelta(seconds=0.016)
                            pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.016)
                            pet._frame_timer_tick()
                            max_stage = max(max_stage, pet._shake_stage)
                        if stop_at_stage and max_stage >= stop_at_stage:
                            break
                    if release:
                        pet._on_stage_mouse_release(
                            make_mouse(QEvent.Type.MouseButtonRelease, last_pt, Qt.MouseButton.LeftButton)
                        )
                    return max_stage

                try:
                    with patch("views.pet_window.time.monotonic", side_effect=lambda: clock[0]):
                        # 1. 常见正常拖拽轨迹（含波浪横拖、弧线 U 弯、绕窗拖动、过冲微调与轻抛蓄力）绝不误触炸毛流程
                        normal_paths = [
                            [(400.0 + i * 18.0, 300.0 + 30.0 * math.sin(i * 0.65), 0.016) for i in range(55)],
                            [
                                (500.0 + 90.0 * math.cos(math.pi * i / 24.0), 350.0 + 90.0 * math.sin(math.pi * i / 24.0), 0.016)
                                for i in range(25)
                            ],
                            (
                                [(400.0 + i * 13.0, 200.0, 0.016) for i in range(31)]
                                + [(790.0, 200.0 + i * 13.0, 0.016) for i in range(1, 24)]
                                + [(790.0 - i * 13.0, 499.0, 0.016) for i in range(1, 31)]
                                + [(400.0, 499.0 - i * 13.0, 0.016) for i in range(1, 24)]
                            ),
                            (
                                [(300.0 + i * 20.0, 300.0, 0.016) for i in range(26)]
                                + [(800.0 - i * 15.0, 300.0, 0.016) for i in range(1, 11)]
                                + [(650.0 + i * 10.0, 300.0, 0.016) for i in range(1, 9)]
                                + [(730.0 - i * 6.0, 300.0, 0.016) for i in range(1, 6)]
                            ),
                            (
                                [(500.0 - i * 16.0, 300.0, 0.016) for i in range(6)]
                                + [(420.0 + i * 25.0, 300.0, 0.016) for i in range(1, 13)]
                            ),
                        ]
                        for path in normal_paths:
                            clock[0] += 0.5
                            max_stage = drive_drag(path, release=True)
                            self.assertEqual(max_stage, 0)
                            self.assertIsNone(pet._override_pose)
                            self.assertFalse(pet._is_sulk)

                        # 2. 触发烦恼后立即停手静置：不得借残余能量自动升级鼓嘴/炸毛，应平滑回落到 FLOAT
                        shake_pts = [
                            (500.0 + 100.0 * math.sin(2.0 * math.pi * 2.5 * (i * 0.016)), 300.0, 0.016)
                            for i in range(190)
                        ]
                        clock[0] += 0.5
                        stage_reached = drive_drag(shake_pts, release=False, stop_at_stage=1)
                        self.assertEqual(stage_reached, 1)
                        self.assertEqual(pet._override_pose, PetState.ANNOYED)
                        step_ticks(1.2)
                        self.assertEqual(pet._shake_stage, 0)
                        self.assertEqual(pet._override_pose, PetState.FLOAT)
                        pet._on_stage_mouse_release(
                            make_mouse(QEvent.Type.MouseButtonRelease, pet._last_move_global, Qt.MouseButton.LeftButton)
                        )

                        # 3. 持续高速往返摇晃按 烦恼 -> 鼓嘴 -> 炸毛 顺阶触发，炸毛松手进入眩晕并衔接生气
                        clock[0] += 0.5
                        stage_reached = drive_drag(shake_pts, release=False)
                        self.assertEqual(stage_reached, 3)
                        self.assertEqual(pet._override_pose, PetState.REDHOT)
                        pet._on_stage_mouse_release(
                            make_mouse(QEvent.Type.MouseButtonRelease, pet._last_move_global, Qt.MouseButton.LeftButton)
                        )
                        self.assertEqual(pet._override_pose, PetState.DIZZY)
                        self.assertTrue(pet._is_sulk)
                        self.assertTrue(pet._pending_sulk_after_pose)

                        # 清理眩晕/生气状态后校验持续快速画圈同样能顺阶触发炸毛，且停晃 2 秒恢复 FLOAT
                        pet._override_pose = None
                        pet._override_locked = False
                        pet._is_sulk = False
                        pet._pending_sulk_after_pose = False
                        spin_pts = [
                            (
                                500.0 + 75.0 * math.cos(2.0 * math.pi * 1.8 * (i * 0.016)),
                                350.0 + 75.0 * math.sin(2.0 * math.pi * 1.8 * (i * 0.016)),
                                0.016,
                            )
                            for i in range(190)
                        ]
                        clock[0] += 0.5
                        stage_reached = drive_drag(spin_pts, release=False)
                        self.assertEqual(stage_reached, 3)
                        self.assertEqual(pet._override_pose, PetState.REDHOT)
                        step_ticks(2.2)
                        self.assertEqual(pet._override_pose, PetState.FLOAT)
                        self.assertEqual(pet._shake_stage, 0)
                        pet._on_stage_mouse_release(
                            make_mouse(QEvent.Type.MouseButtonRelease, pet._last_move_global, Qt.MouseButton.LeftButton)
                        )
                        self.assertIsNone(pet._override_pose)
                        self.assertFalse(pet._is_sulk)
                finally:
                    pet.close()
        finally:
            mgr.dispose()

    def test_throw_bounces_at_visible_sprite_screen_edges(self) -> None:
        mgr = SpriteManager()
        try:
            mgr.preload_all()
            with tempfile.TemporaryDirectory() as tmp:
                fixed_area = QRect(0, 0, 1920, 1080)
                for style in (PetStyleMode.CHIBI, PetStyleMode.STANDARD):
                    settings = PetSettings(
                        style_mode=style,
                        auto_walk=False,
                        gravity_enabled=True,
                        edge_dock_enabled=True,
                        particles_enabled=False,
                        chat_sync_enabled=False,
                        show_speech_bubble=False,
                    )
                    service = Mock(settings_path=Path(tmp) / f"settings_{style.value}.json")
                    pet = PetWindow(
                        settings,
                        service,
                        mgr,
                        SoundService(),
                        SystemMonitorService(),
                        GeminiChatService(),
                    )
                    pet._frame_timer.stop()
                    pet._ai_timer.stop()
                    pet._idle_speech_timer.stop()
                    pet._get_work_area = lambda _cx, _cy, area=fixed_area: area  # type: ignore[method-assign]
                    try:
                        pet._refresh_sprite_visual()
                        sprite_rect = pet._get_local_sprite_rect()
                        self.assertGreaterEqual(sprite_rect.top(), 180.0)

                        # 1. 向上抛掷时不得在窗口顶边 y=0（角色头顶仍距屏顶 ~200px）处提前反弹，
                        #    必须飞至角色可见顶边贴近屏幕上边 area.top() 才反向回弹。
                        pet.move(800, 120)
                        pet._clamp_and_apply_gravity_after_release(
                            refresh_resting=True,
                            release_vel=(0.0, -1450.0),
                        )
                        bounced_top_vis_y: float | None = None
                        min_vis_top = float("inf")
                        for _ in range(90):
                            prev_vy = pet._fall_velocity_y
                            pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.016)
                            pet._frame_timer_tick()
                            vis_top = pet.y() + sprite_rect.top()
                            min_vis_top = min(min_vis_top, vis_top)
                            if prev_vy < 0.0 and pet._fall_velocity_y > 0.0:
                                bounced_top_vis_y = vis_top
                                break
                        self.assertIsNotNone(bounced_top_vis_y)
                        self.assertAlmostEqual(min_vis_top, float(fixed_area.top()), delta=2.0)
                        assert bounced_top_vis_y is not None
                        self.assertAlmostEqual(bounced_top_vis_y, float(fixed_area.top()), delta=16.0)

                        # 2. 向左/向右抛掷须在可见贴图左右边缘触达屏幕边缘时才回弹
                        pet.move(260, 200)
                        pet._clamp_and_apply_gravity_after_release(
                            refresh_resting=True,
                            release_vel=(-1500.0, -200.0),
                        )
                        min_vis_left = float("inf")
                        bounced_left = False
                        for _ in range(90):
                            prev_vx = pet._throw_vx
                            pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.016)
                            pet._frame_timer_tick()
                            min_vis_left = min(min_vis_left, pet.x() + sprite_rect.left())
                            if prev_vx < 0.0 and pet._throw_vx > 0.0:
                                bounced_left = True
                                break
                        self.assertTrue(bounced_left)
                        self.assertAlmostEqual(min_vis_left, float(fixed_area.left()), delta=2.0)

                        pet.move(1400, 200)
                        pet._clamp_and_apply_gravity_after_release(
                            refresh_resting=True,
                            release_vel=(1500.0, -200.0),
                        )
                        max_vis_right = float("-inf")
                        bounced_right = False
                        for _ in range(90):
                            prev_vx = pet._throw_vx
                            pet._last_tick_utc = datetime.now(timezone.utc) - timedelta(seconds=0.016)
                            pet._frame_timer_tick()
                            max_vis_right = max(max_vis_right, pet.x() + sprite_rect.right())
                            if prev_vx > 0.0 and pet._throw_vx < 0.0:
                                bounced_right = True
                                break
                        self.assertTrue(bounced_right)
                        self.assertAlmostEqual(max_vis_right, float(fixed_area.right() + 1), delta=2.0)

                        # 3. 紧邻左侧边但向右甩出时不得误吸回左侧贴边，应保持正常抛掷飞行
                        pet.move(int(round(fixed_area.left() - sprite_rect.left() + 10.0)), 260)
                        pet._try_edge_dock_or_gravity_drop(release_vel=(1100.0, -200.0))
                        self.assertEqual(pet._settings.docked_side, DockSide.NONE)
                        self.assertTrue(pet._is_falling)
                        self.assertGreater(pet._throw_vx, 500.0)
                    finally:
                        pet.close()
        finally:
            mgr.dispose()

    def test_background_warm_render_cache_predecodes_and_primes_metrics(self) -> None:
        # 验证后台线程预解码高分辨率标准立绘后会生成紧致图像、预填重心缓存并预缩图
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Assets"
            sprites_dir = root / "Sprites"
            std_dir = root / "Standard"
            sprites_dir.mkdir(parents=True)
            std_dir.mkdir(parents=True)

            _write_solid_png(sprites_dir / "idle.png", (64, 64), (16, 8, 32, 52), QColor(100, 100, 255, 255))
            _write_solid_png(std_dir / "standee.png", (200, 320), (40, 20, 100, 280), QColor(140, 100, 255, 255))
            _write_solid_png(std_dir / "surprised.png", (480, 1200), (120, 80, 200, 1040), QColor(255, 160, 120, 255))

            mgr = SpriteManager(assets_root=str(root))
            try:
                mgr.preload_all()
                mgr.warm_render_cache(PetStyleMode.STANDARD, sprite_height_px=300.0, dpr=1.5)
                thread = mgr._preload_thread
                self.assertIsNotNone(thread)
                assert thread is not None
                thread.join(timeout=3.0)
                self.assertFalse(thread.is_alive())
                self.assertIn("surprised", mgr._predecoded_standard)

                # 主线程首次获取 surprised 应直接消费后台预解码结果，且紧致高度受上限约束
                clip = mgr.get_clip(PetState.SURPRISED, PetStyleMode.STANDARD)
                self.assertIsNotNone(clip)
                assert clip is not None
                pm = clip.preview_pixmap()
                self.assertIsNotNone(pm)
                assert pm is not None
                self.assertLessEqual(pm.height(), 900)
                self.assertNotIn("surprised", mgr._predecoded_standard)

                # analyze_qimage_metrics + prime_pixmap_metrics 应与主线程分析一致
                img = QImage(80, 80, QImage.Format.Format_ARGB32)
                img.fill(QColor(0, 0, 0, 0))
                painter = QPainter(img)
                painter.fillRect(10, 10, 30, 60, QColor(90, 160, 250, 255))
                painter.end()
                expected = analyze_qimage_metrics(img)
                probe_pm = QPixmap.fromImage(img)
                prime_pixmap_metrics(probe_pm, expected)
                self.assertEqual(analyze_pixmap_metrics(probe_pm), expected)
            finally:
                mgr.dispose()


if __name__ == "__main__":
    unittest.main()
