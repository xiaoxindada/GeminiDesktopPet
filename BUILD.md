# 开发与构建指南

## 1. 环境准备

项目基于 [pyproject.toml](pyproject.toml) 管理依赖（Python `>=3.11`，推荐 `uv` + `.venv`）：

```shell
# 安装运行、打包（PyInstaller）与测试依赖
uv sync --extra build --extra dev

# 若不使用 uv，可使用标准 venv + pip 安装：
# python -m venv .venv
# .venv\Scripts\activate
# pip install -e ".[build,dev]"
```

> 若执行 `uv sync` 后出现 `ImportError: DLL load failed while importing QtCore`，通常是因为旧环境中卸载重装包时删除了共享的 `PySide6/__init__.py` 等文件，执行以下命令重装修复：
>
> ```shell
> uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6 --extra build --extra dev
> ```

## 2. IDE 使用

- **VS Code**：项目已内置 [.vscode/settings.json](.vscode/settings.json)、[.vscode/launch.json](.vscode/launch.json)、[.vscode/tasks.json](.vscode/tasks.json) 与 [.vscode/extensions.json](.vscode/extensions.json)。打开根目录后选择解释器 `.venv\Scripts\python.exe`，按 `F5` 可直接调试启动，`Ctrl+Shift+B` 可执行一键发布构建。
- **PyCharm**：打开根目录后将解释器设为 `.venv\Scripts\python.exe`，并将根目录标记为 `Sources Root`；主运行脚本设为 `main.py`，测试运行器选择 `Unittest`（目录 `tests`）。

## 3. 构建与测试命令

```shell
# 启动 / 优雅关闭 / 配置清理
python main.py
python main.py --close-pet --stop-all
python clean_config.py

# Windows 一键打包（基于 GeminiDesktopPet.spec）
BuildRelease.bat --no-pause
```

- **打包输出**：生成目录 `out\GeminiDesktopPet\` 并打包为 `out\GeminiDesktopPet-v<版本>-win-x64.zip`。
- **版本号**：统一维护在 [models/app_version.py](models/app_version.py) 的 `APP_VERSION`。
