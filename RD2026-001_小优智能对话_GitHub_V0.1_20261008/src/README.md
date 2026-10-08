# src 核心业务模块

本目录运行在机器人 Docker 内，负责角色配置、对话编排、知识检索、Qwen、TTS、Web 辅助和模式/动作配置。

| 文件 | 职责 | 输入 | 输出 | 当前状态 |
|---|---|---|---|---|
| `character_config.py` | 发现、校验、加载和切换角色包 | 角色 ID/目录 | 角色配置对象 | 已验证 |
| `dialogue_core.py` | 固定问答优先、知识检索、短期历史和 LLM 编排 | 用户文本/角色 | 回答文本 | 已验证 |
| `knowledge_base.py` | 轻量 JSON 知识加载和检索 | 问题/知识库 | 上下文片段 | 已验证 |
| `qwen_client.py` | Qwen 调用 | Prompt/历史/知识 | 自由回答 | 已验证 |
| `qwen_tts.py` | TTS 调用 | 文本/角色指令 | PCM | 已验证 |
| `live_web.py` | Web 页面/状态辅助 | HTTP 状态 | 页面内容 | 已跑通 |
| `modes.py` | 模式定义与辅助 | 模式状态 | 控制参数 | 已跑通 |
| `motions.py` | 动作配置加载 | 动作 ID | 动作定义 | 研究中 |

调用方主要是 `manager_container.py` 和 `robot_voice.py`；下游是 DashScope、角色 JSON 和机器人适配层。配置位于 `.env`（未归档）、`config/` 与 `characters/`。本地自动化测试已覆盖核心非硬件逻辑；硬件效果以真机记录为准。已知问题见 `12_问题清单/问题清单.md`。
