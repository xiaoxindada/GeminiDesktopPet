# Gemini 娘 桌面萌宠

基于 [Gender Swap AI · Gemini 娘角色档案](https://genderswapai.com/zh-CN/ai-gijinka/gemini) 设定的 Windows 桌面宠物，支持 **Q 版（默认） / 标准立绘版** 双形态切换。

## 运行效果预览

![Gemini 娘双形态互动、快捷栏与骨骼漫步/贴边预览](preview/images/preview-pet-modes.png)

| 角色档案与设定 | Q 版表情动作图鉴 |
| :---: | :---: |
| ![角色档案与设定](preview/images/preview-profile.png) | ![Q版表情动作图鉴](preview/images/preview-gallery.png) |

| Antigravity 剩余额度看板 |
| :---: |
| ![Antigravity 剩余额度看板](preview/images/preview-quota.png) |

<details>
<summary>展开查看：偏好设置</summary>

<p align="center">
  <img src="preview/images/preview-settings.png" alt="偏好设置" width="380" />
</p>

</details>

## 核心功能

- **双形态与自然行走**：内置多种 Q 版动作表情与标准版全比例三视图立绘；自由漫步时启用自然骨骼步态动画，边缘停靠时自动贴边偷看。
- **暗夜星光深色主题**：主菜单、弹窗与设置界面统一采用深紫星光透明风格。
- **应用联动启停**：支持跟随 `Antigravity` 与 `Gemini` 桌面端自动启停与待机切换。
- **拟态物理互动**：摸头呼噜增加好感、拖曳甩动反应、反重力悬浮、屏幕边缘贴靠与自由漫步。
- **Antigravity 额度看板**：双击桌宠、双击托盘图标或通过右键 / 托盘菜单中的 **📊 查看反重力额度** 一键呼出悬浮看板，展示各模型组（`5 小时` / `一周`）剩余配额百分比、重置时间与账号套餐；桌宠会同步切到查询动作并在出结果后播报（Q 版与标准立绘均支持），另支持低余量警示、悬停倒计时提示、智能避让贴靠、自由拖拽、手动刷新与一键复制摘要。
- **角色档案与 AI 对话**：内置 **角色档案图鉴** 与 **AI 对话气泡**（默认关闭，可从右键或托盘菜单打开）；支持离线互动，也可在设置中配置个人 Gemini API Key，通过 Google 官方接口开启实时对话。

## 配置与清理

- **用户配置目录**：应用设置、自定义角色档案与运行日志统一保存在 `%USERPROFILE%\.GeminiDesktopPet`（个人 API Key 等信息仅保存在本地，请勿对外分享配置文件）。
- **一键清理工具**：发布包内附带 `CleanConfig.exe`（源码运行可执行 `python clean_config.py`），可停止运行中的进程并清理本地配置目录与用户启动快捷方式（支持 `-y` / `--yes` / `/y` 免确认静默清理）。

## 问题反馈

如遇到异常闪退、联动未生效、额度读取失败或更新报错等问题，可整理信息后前往 [GitHub Issues](https://github.com/xiaoxindada/GeminiDesktopPet/issues) 反馈：

1. **开启运行日志**（默认关闭）：
   - 在 **偏好设置 → 运行日志与问题排查**，或系统托盘 / 桌宠右键菜单中开启运行日志并复现一次问题。
2. **脱敏并提交描述**：
   - 可在设置窗口打开日志目录查看当天日志，让你的 AI 助手协助提炼问题现象、复现步骤与关键报错摘要后写入 Issue 描述；
   - **提交日志片段或截图前，请务必先检查并脱敏个人用户名路径、账号信息及凭据等敏感内容**（排查完成后可随时关闭日志开关）。

## 开发与打包

- 本地开发环境搭建、IDE 配置与独立可执行文件打包说明请查看 [BUILD.md](BUILD.md)。

> 角色设定、素材来源与开源协议详见 [CREDITS.md](CREDITS.md) 与 [LICENSE](LICENSE)。
