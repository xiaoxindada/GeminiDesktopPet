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
REQUIRED_BUILD_FILES = (*REQUIRED_RELEASE_DOCS, "GeminiDesktopPet.spec")
EXPECTED_STAGE_ENTRIES = (
    "CREDITS.md",
    "CleanConfig.exe",
    "GeminiDesktopPet.exe",
    "LICENSE",
    "README.md",
    "_internal",
)


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
