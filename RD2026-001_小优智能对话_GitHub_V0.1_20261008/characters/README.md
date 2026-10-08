# characters 角色包

正式角色为 `libai`、`liqingzhao`、`teacher`、`companion`、`sichuan`。`test_character` 只用于单元测试。`current_character.json` 记录当前选择，启动时由 `character_config.py` 读取。

每个正式角色独立保存 `character.json`、`persona_prompt.md`、`fixed_scenarios.json`、`knowledge.json`、`expressions.json` 和 `actions.json`，从而隔离 Persona、开场白、固定答案、知识、TTS、表情和动作。输入为角色 ID/用户问题，输出为角色配置和回答策略；由 manager、DialogueCore 和语音主链共同使用。

五角色本地测试与真机切换已有证据，串台检查已通过。新增角色应复制目录契约并补测试，不修改其他角色内容。
