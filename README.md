# Gemini 娘 桌面萌宠

基于 [Gender Swap AI · Gemini 娘角色档案](https://genderswapai.com/zh-CN/ai-gijinka/gemini) 设定的桌面宠物，支持 **Q 版（默认） / 标准立绘版** 双形态切换。

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

- **双形态桌宠**：Q 版与标准立绘一键切换，自由漫步、贴边停靠。
- **桌面互动**：摸头、拖拽、反重力悬浮等拟态反馈，附带好感度。
- **应用联动**：跟随 Antigravity 与 Gemini 桌面端自动启停。
- **暗夜星光主题**：菜单、弹窗与设置统一为深紫星光风格。
- **反重力额度看板**：一键呼出额度与重置时间，低余量提醒，可复制摘要。
- **角色档案与 AI 对话**：内置角色图鉴与可选的实时对话，支持离线互动。
- **反重力对话同步**：开启后 Antigravity 的新消息会逐条显示在桌宠气泡里，可随时停止。

## 配置与清理

- **配置目录**：设置、角色档案与日志保存在 `%USERPROFILE%\.GeminiDesktopPet`；个人 API Key 仅存本地，请勿分享配置文件。
- **一键清理**：发布包附带 `CleanConfig.exe`；源码运行可用 `python clean_config.py`，加 `-y` 免确认。

## 问题反馈

遇到闪退、联动失效、额度读取失败或更新报错，请到 [GitHub Issues](https://github.com/xiaoxindada/GeminiDesktopPet/issues) 反馈：

1. 在 **偏好设置 → 运行日志与问题排查** 或右键菜单开启运行日志，复现一次问题。
2. 提交前先脱敏个人路径、账号与凭据等敏感信息。

## 开发与打包

本地开发、IDE 配置与打包详见 [BUILD.md](BUILD.md)。

> 角色设定、素材来源与开源协议见 [CREDITS.md](CREDITS.md) 与 [LICENSE](LICENSE)。
