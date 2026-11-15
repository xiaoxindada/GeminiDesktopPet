"""纯 Python Git 变更摘要生成器（用于 Release 包与 GitHub Release Notes）。"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from typing import Iterable, Optional

_CN_RELEASE_COMMIT_RE = re.compile(
    r"^(?:发布|发版|版本发布)\s*(?:版本\s*)?v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.\-]+)?(?:\s*版本)?$"
)
_EN_RELEASE_COMMIT_RE = re.compile(
    r"^(?:(?:chore|build|ci|docs)(?:\([^)]+\))?\s*:\s*)?"
    r"(?:release|bump(?:\s+version)?|version\s+bump)\s*(?:to\s+)?v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.\-]+)?$",
    re.IGNORECASE,
)


def _run_git(repo_root: Path, git_args: list[str]) -> tuple[int, list[str]]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "-c", "i18n.logOutputEncoding=UTF-8", "-c", "core.quotepath=false", *git_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, proc.stdout.splitlines()
    except OSError:
        return 1, []


def is_release_version_commit(subject: str) -> bool:
    """过滤纯版本号推进或发版标记提交，使变更日志仅保留真实功能改动。"""
    cleaned = subject.strip()
    if not cleaned:
        return False
    return bool(_CN_RELEASE_COMMIT_RE.match(cleaned) or _EN_RELEASE_COMMIT_RE.match(cleaned))


def generate_git_summary(repo_root: Path, version: str, output_path: Path) -> Path:
    """采集上一个 Tag 到 HEAD 的提交主题并生成 UTF-8 Markdown 摘要。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_ver = version.strip().lstrip("vV")
    current_tag = f"v{normalized_ver}"

    rc, inside_lines = _run_git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or "".join(inside_lines).strip() != "true":
        fallback = "\n".join(
            [
                f"## 变更日志 ({current_tag})",
                "",
                f"- 版本：`{current_tag}`",
                "- 更新内容：当前目录不是 Git 仓库，无法采集变更列表。",
                "",
            ]
        )
        output_path.write_text(fallback, encoding="utf-8")
        return output_path

    _, tag_lines = _run_git(repo_root, ["tag", "--merged", "HEAD", "--sort=-v:refname"])
    merged_tags = [line.strip() for line in tag_lines if line.strip()]
    prev_tag = next((tag for tag in merged_tags if tag != current_tag), None)

    if prev_tag:
        range_args = [f"{prev_tag}..HEAD"]
        range_desc = f"`{prev_tag}...{current_tag}`"
    elif current_tag in merged_tags:
        cnt_rc, cnt_lines = _run_git(repo_root, ["rev-list", "--count", f"{current_tag}..HEAD"])
        try:
            count_after = int(cnt_lines[0].strip()) if cnt_rc == 0 and cnt_lines else 0
        except ValueError:
            count_after = 0
        if count_after > 0:
            range_args = [f"{current_tag}..HEAD"]
            range_desc = f"自 `{current_tag}` 起的新增变更"
        else:
            range_args = ["-n", "30", "HEAD"]
            range_desc = "最近 30 条更新内容"
    else:
        range_args = ["-n", "30", "HEAD"]
        range_desc = "最近 30 条更新内容"

    log_rc, log_lines = _run_git(repo_root, ["log", *range_args, "--no-merges", "--pretty=format:%s"])
    if log_rc != 0 or not log_lines:
        _, log_lines = _run_git(repo_root, ["log", *range_args, "--pretty=format:%s"])

    commit_lines: list[str] = []
    for raw in log_lines:
        subject = raw.strip()
        if not subject or is_release_version_commit(subject):
            continue
        commit_lines.append(f"- {subject}")

    md_lines = [
        f"## 变更日志 ({current_tag})",
        "",
        f"- 版本：`{current_tag}`",
        f"- 对比范围：{range_desc}",
        f"- 变更条目：{len(commit_lines)} 条",
        "",
    ]
    if not commit_lines:
        md_lines.append("- 暂无新增变更记录。")
    else:
        md_lines.append("### 更新内容")
        md_lines.extend(commit_lines)

    output_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Git 提交摘要 Markdown")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    generate_git_summary(args.repo_root, args.version, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
