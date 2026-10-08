# 当前源码快照说明

这是本轮从开发机现有工程提取的“小优智能对话模式”归档副本，保持原目录结构，便于审计 import、配置加载和部署关系。原始来源为：

`/home/zzl/Documents/Codex/小优优必选机器人ARM64交付包_V0.3.0/app/`

归档不修改原项目，不包含 `.env`、密钥、密码、token、license、机器人鉴权材料或厂家 `walker-*` 文件。

## 入口与数据流

`manager_container.py`（8770/模式管理） → `robot_voice.py`（语音循环） → 麦克风 PCM/VAD/ASR → `src/dialogue_core.py`（固定问答或知识+Qwen） → `src/qwen_tts.py` → 增益/限幅 → `paplay`/机器人扬声器。

`src/character_config.py` 负责五角色包加载和隔离；`ubtech/` 封装机器人接口；`vendor_demos/` 保存当前需要的厂家侧辅助可执行文件；`tests/` 与 `tools/` 分别用于验证和研发辅助。

## 当前状态

稳定主链及五角色已有真机记录；本快照是归档副本，不能仅凭目录存在认定可独立部署。镜像构建、运行环境和 `.env` 仍需按稳定快照恢复资料核对。

## 核心模块用途索引

| 核心模块 | 主要代码 | 用途说明 |
|---|---|---|
| Manager | `manager_container.py` | 8770、启动停止、厂家能力隔离、Coze 安全门、角色切换和状态管理；详见 `../api/README.md` |
| Voice Runtime | `robot_voice.py` | PCM、VAD、ASR、对话、TTS、播放和非阻塞表情主循环；详见 `../audio/README.md` |
| ASR | `robot_voice.py`、`ubtech/voice_input.py` | 将机器人麦克风 utterance 转成文本；详见 `../asr/README.md` |
| Qwen/LLM | `src/qwen_client.py`、`src/dialogue_core.py` | 在未命中固定回答时组合 Persona、知识和短期历史生成回答；详见 `../llm/README.md` |
| TTS | `src/qwen_tts.py`、`robot_voice.py` | 将回答转换为 PCM，经增益和限幅后完整播放；详见 `../tts/README.md` |
| Character | `src/character_config.py`、`characters/` | 加载、校验和切换五个隔离角色包；详见 `../characters/README.md` |
| Knowledge | `src/knowledge_base.py`、角色 `knowledge.json` | 提供角色轻量本地知识检索；详见 `src/README.md` 和 `../llm/README.md` |
| UBTECH Adapter | `ubtech/ubtech_adapter.py`、`voice_input.py` | 隔离上层对话逻辑与厂家 SDK/ROS2/ROSA；详见 `ubtech/README.md` 和 `../robot/README.md` |
| Expression | `robot_voice.py::ExpressionBridge`、`vendor_demos/xiaoyou_expression` | 回答期间非阻塞触发离散表情动作；详见 `vendor_demos/README.md` |
| Gaze | `ubtech/gaze_manager.py` | 管理 Face Tracking 和 VIEW_FOLLOW，可选且不作为语音硬依赖；详见 `ubtech/README.md` |
| Docker | `Dockerfile.complete` | 构建小优自有 ARM64 容器；旧 compose/脚本位于待确认区，详见 `../docker/README.md` |
| Tests | `tests/`、`test_coze_query_cleanup.py` | 验证角色、适配、Mock、知识和 Coze 查询清理；详见 `tests/README.md` 和 `../tests/README.md` |
