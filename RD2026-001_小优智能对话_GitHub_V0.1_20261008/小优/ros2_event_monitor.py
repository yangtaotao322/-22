"""ROS 2 listener for UBTECH wake and direction events."""
import json
import threading
from collections import deque


TOPICS = {
    "main_wakeup_word": ("/robo/audio/subscribe/main_wakeup_word", 1),
    "wakeup_event": ("/robo/audio/subscribe/wakeup_event", 10),
    "wakeup_state": ("/robo/audio/subscribe/wakeup_state", 1),
    "doa_event": ("/robo/audio/subscribe/doa_event", 10),
}


def parse_event(topic, raw):
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON event on {topic}") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        raise ValueError(f"invalid event envelope on {topic}")
    return envelope


class UbtechEventMonitor:
    def __init__(self, callback=None):
        import rclpy
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String

        self.rclpy = rclpy
        self.callback = callback
        self.events = deque(maxlen=1000)
        self._owns_rclpy = not rclpy.ok()
        if self._owns_rclpy:
            rclpy.init(args=None)
        self.node = rclpy.create_node("ubtech_perception_monitor")
        self.subscriptions = []
        for name, (topic, depth) in TOPICS.items():
            qos = QoSProfile(depth=depth, reliability=ReliabilityPolicy.RELIABLE)
            sub = self.node.create_subscription(String, topic, self._make_callback(name, topic), qos)
            self.subscriptions.append(sub)
        self.thread = None

    def _make_callback(self, name, topic):
        def receive(message):
            try:
                envelope = parse_event(topic, message.data)
                event = {"name": name, "topic": topic, "envelope": envelope}
            except ValueError as exc:
                event = {"name": name, "topic": topic, "error": str(exc), "raw": message.data[:200]}
            self.events.append(event)
            if self.callback:
                self.callback(event)
        return receive

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.rclpy.spin, args=(self.node,), daemon=True)
        self.thread.start()

    def close(self):
        if self.node:
            self.node.destroy_node(); self.node = None
        if self.thread:
            self.thread.join(timeout=2)
        if self._owns_rclpy and self.rclpy.ok():
            self.rclpy.shutdown()
