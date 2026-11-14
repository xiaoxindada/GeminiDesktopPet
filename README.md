# Gemini 娘 桌面萌宠

基于 [Gender Swap AI · Gemini 娘角色档案](https://genderswapai.com/zh-CN/ai-gijinka/gemini) 设定的 Windows 原生透明桌面宠物，支持 **Q 版（默认） / 标准立绘版** 双形态切换。

## 运行效果预览

![Gemini 娘双形态互动、快捷栏与骨骼漫步/贴边预览](preview/images/preview-pet-modes.png)

| 角色档案与设定 | Q 版表情动作图鉴 |
| :---: | :---: |
| ![角色档案与设定](preview/images/preview-profile.png) | ![Q版表情动作图鉴](preview/images/preview-gallery.png) |

<details>
<summary>展开查看：偏好设置与 VBS 应用联动界面</summary>

<p align="center">
  <img src="preview/images/preview-settings.png" alt="偏好设置与 VBS 应用联动" width="380" />
</p>

</details>

## 核心功能

- **双形态与自然行走**：内置 55 种 Q 版动作表情与标准版全比例三视图立绘；自由漫步时启用自然骨骼步态动画，边缘停靠时自动贴边偷看。
- **暗夜星光深色主题**：菜单、弹窗与设置界面统一采用深紫星光透明风格。
- **应用联动启停**：支持跟随 `Antigravity` 与 `Gemini` 桌面 App 自动启动与关闭。
- **拟态物理互动**：摸头呼噜增加好感、拖曳甩动反应、反重力悬浮、屏幕边缘贴靠与自由漫步。
- **Antigravity 额度看板**：双击宠物或通过右键 / 托盘菜单查看当前账号剩余额度与重置时间。
- **角色档案与 AI 对话**：内置 **角色档案图鉴** 与 **AI 对话气泡**（气泡默认关闭，可从右键或托盘菜单打开；支持离线回复及接入 Gemini API Key）。

## 提交问题描述

使用过程中如果遇到异常闪退、应用联动未生效、额度读取失败或更新报错等问题，请按以下步骤采集信息并提交反馈：

1. **开启运行日志开关**（默认关闭）：
   - 打开 **偏好设置 → 运行日志与问题排查**，勾选 **开启运行日志**；也可以在系统托盘图标或桌宠右键菜单中直接勾选 **记录运行日志（排错用）**。
2. **复现问题并找到运行日志**：
   - 保持开关开启并完整复现一次问题。日志生成在 `Logs` 文件夹：
   - `<主程序目录>\Logs\YYYY-MM-DD.log`（可在设置窗口点击 **📂 打开日志目录** 一键打开）。
   - 提交 [GitHub Issues](https://github.com/xiaoxindada/GeminiDesktopPet/issues) 时，建议先让**你的其他 AI 助手**读取当天的 `.log` 文件帮你做一次问题整理（梳理出问题现象、复现步骤、环境信息与关键报错摘要）并写入**问题描述**；
   - 或者直接在提交问题时附上简短说明，或者上传 `.log` 日志文件（提交前请先检查脱敏个人隐私信息），方便更快定位与修复（排查结束后可随时关闭运行日志开关）。

```shell
# 使用 dotnet CLI 或 Visual Studio MSBuild 构建 Release
dotnet build GeminiDesktopPet.slnx -c Release

# 运行或打包发布独立 ZIP（输出至 publish）
.\BuildRelease.bat
```

> 角色设定、素材来源与开源协议详见 [CREDITS.md](CREDITS.md) 与 [LICENSE](LICENSE)。
