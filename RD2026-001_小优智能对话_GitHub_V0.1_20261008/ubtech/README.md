# ubtech 适配层

本目录将小优上层逻辑与优必选机器人 SDK、ROS2/ROSA service 和音频接口隔离。

- `ubtech_adapter.py`：正式机器人能力适配入口。
- `voice_input.py`：麦克风/录音服务接入。
- `audio_contract.py`：PCM 参数和格式契约。
- `gaze_manager.py`：视线跟随管理，不作为语音主链硬依赖。
- `ros2_event_monitor.py`：ROS2 事件监听。
- `mock_adapter.py`、`mock_scenario.py`：开发电脑无真机调试。

输入为上层动作、音频或状态请求；输出为厂家调用、PCM、事件或 Mock 结果。正式模块运行在机器人 Docker，Mock 可在开发电脑运行。依赖厂家 SDK/ROS2/ROSA；厂家文件不在本归档中。语音所需适配已验证；视觉、表情扩展为已跑通或待真机复测，详见研发日志第 4 册。
