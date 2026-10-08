# vendor_demos 厂家辅助程序说明

本目录只归档小优当前代码引用或恢复时可能需要核对的辅助程序，不包含厂家 SDK、ROS 配置或 `walker-*` 组件。

- `robo_demo_menu_capture`：麦克风采集辅助，正式语音链相关。
- `xiaoyou_expression`：ExpressionBridge 可执行程序，负责非阻塞触发表情。
- `xiaoyou_speaker`：播放辅助程序；当前主链记录以 `paplay` 为准，继续保留必要性待确认。
- `action_list.md`：动作名称参考，使用前必须与真机固件能力核对。

这些文件运行在机器人环境，依赖厂家 runtime/ROS。输入为音频或动作请求，输出为 PCM、表情动作或播放效果。不得修改或替换厂家组件；本项目只保存自己的桥接程序。采集和旧表情链已跑通，新版表情与口型仍需真机复测。
