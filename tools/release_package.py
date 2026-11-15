"""发布构建辅助模块：统一管理版本号解析、产物目录校验、变更摘要与 ZIP 打包。"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sys
from typing import Iterable, Optional
import zipfile

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from models.app_version import AppVersion
from tools.git_summary import generate_git_summary

REQUIRED_RELEASE_DOCS = ("README.md", "CREDITS.md", "LICENSE")
REQUIRED_BUILD_FILES = (*REQUIRED_RELEASE_DOCS, "main.py", "clean_config.py")
EXPECTED_STAGE_ENTRIES = (
    "CREDITS.md",
    "CleanConfig.exe",
    "GeminiDesktopPet.exe",
    "LICENSE",
    "README.md",
    "_internal",
)


def build_pyinstaller_spec_content(repo_root: Path) -> str:
    """生成用于双 EXE 打包的 PyInstaller .spec 内容，绑定仓库根目录。"""
    resolved_root = repr(str(repo_root.resolve()))
    return f"""# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT_DIR = {resolved_root}
ICON_PATH = os.path.join(ROOT_DIR, "Assets", "AppIcon.ico")

main_hiddenimports = collect_submodules("google.genai")
gui_excludes = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtNetwork",
]

a_main = Analysis(
    [os.path.join(ROOT_DIR, "main.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[(os.path.join(ROOT_DIR, "Assets"), "Assets")],
    hiddenimports=main_hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=gui_excludes,
    noarchive=False,
    optimize=0,
)
pyz_main = PYZ(a_main.pure)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    exclude_binaries=True,
    name="GeminiDesktopPet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_PATH],
)

# CleanConfig.exe 仅包含无 GUI 的清理入口与服务模块，与主程序共用同一份 _internal 依赖
a_clean = Analysis(
    [os.path.join(ROOT_DIR, "clean_config.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PySide6",
        "shiboken6",
        "google",
        "PIL",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)
pyz_clean = PYZ(a_clean.pure)

exe_clean = EXE(
    pyz_clean,
    a_clean.scripts,
    [],
    exclude_binaries=True,
    name="CleanConfig",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_PATH],
)

coll = COLLECT(
    exe_main,
    exe_clean,
    a_main.binaries,
    a_main.datas,
    a_clean.binaries,
    a_clean.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GeminiDesktopPet",
)
"""


def write_pyinstaller_spec(repo_root: Path, output_path: Path) -> Path:
    """将动态生成的 PyInstaller .spec 写入构建临时目录。"""
    content = build_pyinstaller_spec_content(repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def resolve_release_version(repo_root: Path, output_path: Path) -> str:
    """校验构建前置文件并将当前应用语义版本写入指定文件。"""
    for filename in REQUIRED_BUILD_FILES:
        if not (repo_root / filename).is_file():
            raise FileNotFoundError(f"Missing required release file: {filename}")

    version = AppVersion.current()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(version, encoding="ascii")
    return version


def finalize_stage_dir(repo_root: Path, stage_dir: Path) -> list[str]:
    """复制发布文档到打包目录并校验顶层文件布局，防止残留多余文件。"""
    if not stage_dir.is_dir():
        raise FileNotFoundError(f"Stage directory does not exist: {stage_dir}")

    for filename in REQUIRED_RELEASE_DOCS:
        src = repo_root / filename
        if not src.is_file():
            raise FileNotFoundError(f"Missing required release doc: {filename}")
        shutil.copy2(src, stage_dir / filename)

    entries = sorted(p.name for p in stage_dir.iterdir())
    expected = sorted(EXPECTED_STAGE_ENTRIES)
    if entries != expected:
        raise RuntimeError(f"Unexpected top-level entries: {entries} (expected {expected})")
    return entries


def create_release_archive(
    repo_root: Path,
    stage_dir: Path,
    version: str,
    summary_path: Path,
    zip_path: Path,
) -> tuple[Path, int, str]:
    """生成 Git 变更摘要，创建发布 ZIP 压缩包并返回路径、字节数与 SHA-256。"""
    if not stage_dir.is_dir():
        raise FileNotFoundError(f"Stage directory does not exist: {stage_dir}")

    generate_git_summary(repo_root, version, summary_path)

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(stage_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(stage_dir).as_posix())

    with zip_path.open("rb") as fp:
        digest = hashlib.file_digest(fp, "sha256").hexdigest()
    size_bytes = zip_path.stat().st_size
    return zip_path, size_bytes, digest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GeminiDesktopPet 发布打包辅助工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_ver = subparsers.add_parser("resolve-version", help="校验前置文件并写出当前版本号")
    p_ver.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    p_ver.add_argument("--output", type=Path, required=True)

    p_spec = subparsers.add_parser("generate-spec", help="生成 PyInstaller .spec 至构建目录")
    p_spec.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    p_spec.add_argument("--output", type=Path, required=True)

    p_stage = subparsers.add_parser("finalize-stage", help="复制发布文档并校验输出目录结构")
    p_stage.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    p_stage.add_argument("--stage-dir", type=Path, required=True)

    p_arch = subparsers.add_parser("create-archive", help="生成变更摘要并打包发布 ZIP")
    p_arch.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    p_arch.add_argument("--stage-dir", type=Path, required=True)
    p_arch.add_argument("--version", required=True)
    p_arch.add_argument("--summary-path", type=Path, required=True)
    p_arch.add_argument("--zip-path", type=Path, required=True)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "resolve-version":
            resolve_release_version(args.repo_root, args.output)
        elif args.command == "generate-spec":
            write_pyinstaller_spec(args.repo_root, args.output)
        elif args.command == "finalize-stage":
            finalize_stage_dir(args.repo_root, args.stage_dir)
        elif args.command == "create-archive":
            _, size_bytes, digest = create_release_archive(
                args.repo_root,
                args.stage_dir,
                args.version,
                args.summary_path,
                args.zip_path,
            )
            print(f"ZIP Size : {size_bytes:,} bytes")
            print(f"SHA-256  : {digest}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
