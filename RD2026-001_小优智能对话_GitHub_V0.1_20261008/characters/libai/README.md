# 李白角色包

正式角色 ID：`libai`。保留已跑通的李白 Persona、开场白、403 条知识、20 首诗、固定 Showcase、TTS、表情与动作策略。六类文件分别负责主配置、Persona、固定问答、知识、表情和动作；由 `character_config.py` 加载，供 manager、DialogueCore 和语音链使用。状态：已验证；不得被其他角色迁移覆盖。已知问题主要属于 ASR/网络等公共链路。
