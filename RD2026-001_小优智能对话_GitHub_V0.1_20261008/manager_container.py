#!/usr/bin/env python3
"""Internal panel that owns the XiaoYou voice child process inside one container."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import atexit, html, json, os, signal, subprocess, threading, time
from urllib.parse import parse_qs
from src.character_config import load_character, list_characters, select_character
from ubtech.gaze_manager import NativeGazeManager

ROOT = Path(__file__).resolve().parent
CHARACTERS_DIR = ROOT / "characters"
PORT = int(os.getenv("XIAOYOU_MANAGER_PORT", "8770"))
PID_FILE = Path("/tmp/xiaoyou_voice.pid")
LOG_FILE = Path("/tmp/xiaoyou_voice.log")
LOCK = threading.Lock()
VOICE = None
VISION_MONITOR = None
GAZE = NativeGazeManager(str(LOG_FILE))
SERVICE_WAITING = False

def alive(pid):
    try: os.kill(pid, 0); return True
    except (ProcessLookupError, PermissionError): return False

def current_pid():
    global VOICE
    if VOICE is not None and VOICE.poll() is None: return VOICE.pid
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if alive(pid) else None
    except (FileNotFoundError, ValueError): return None

def restore_native():
    subprocess.run(["docker", "start", "walker-behavior.behavior-1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    services = [
        ("/audio/sense/set_record_audio_switch", "audio_msgs/srv/SetRecordAudioSwitch", '{"record_enable":true}'),
        ("/system/control/wakeup_enable_set", "uworld_state_msgs/srv/SetBool", '{"data":true}'),
        ("/system/control/wakeup_followup_set", "uworld_state_msgs/srv/SetBool", '{"data":true}'),
    ]
    def restore_one(item):
        service, kind, body = item
        script = ("source /opt/walker/setup.bash; timeout 6 rosa service call "
                  + service + " " + kind + " '" + body + "'")
        try:
            subprocess.run(["docker", "exec", "walker-audio.audio-1", "bash", "-lc", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(restore_one, services))

def interrupt_vendor_audio():
    """Best-effort immediate interruption before changing mode or restoring audio."""
    calls = [
        ("walker-registry.registry-1", "/audio/stream/coze/interrupt_action_audio",
         "coze_msgs/srv/InterruptActionAudio", "{}"),
        ("walker-audio.audio-1", "/audio/stream/coze/leave_room",
         "sys_msgs/srv/Trigger", "{}"),
    ]
    for container, service, kind, body in calls:
        script = ("source /opt/walker/setup.bash; timeout 4 rosa service call --no-daemon "
                  + service + " " + kind + " '" + body + "'")
        try:
            subprocess.run(["docker", "exec", container, "bash", "-lc", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=6, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

def set_visual_follow(enabled: bool) -> None:
    """Enable vendor perception/head-follow services without voice behavior."""
    # mode 0 is vendor multi-face tracking.  Using mode 1 with face_id=-1
    # acknowledges the request but leaves the controller without a target.
    track_mode = 0 if enabled else 0
    motion_mode = 3 if enabled else 1
    calls = [
        ("walker-perception.perception-1",
         "/vision/vision_perception_controller/face_tracking_mode_command",
         "vision_msgs/srv/FaceTrackingModeCommand",
         '{"track_mode":' + str(track_mode) + ',"face_id":-1}'),
        ("walker-motion.motion-1", "/mc/set_uworld_motion_mode",
         "uworld_motion_msgs/srv/SetMotionMode",
         '{"motion_mode":{"mode_type":' + str(motion_mode) + '}}'),
    ]
    results = []
    # The vendor perception controller can acknowledge FaceTracking while the
    # global vision gate is still off. Enable it explicitly before selecting
    # tracking/head-follow mode. This service is hosted by walker-audio.
    vision_script = ("source /opt/walker/setup.bash; timeout 8 rosa service call --no-daemon "
                     "/system/control/vision_enable_set uworld_state_msgs/srv/SetBool '"
                     + ('{"data":true}' if enabled else '{"data":false}') + "'")
    try:
        vision_result = subprocess.run(
            ["docker", "exec", "walker-audio.audio-1", "bash", "-lc", vision_script],
            capture_output=True, text=True, timeout=12, check=False)
        vision_output = vision_result.stdout + vision_result.stderr
        vision_ok = ("success=True" in vision_output or "status=0" in vision_output)
    except (OSError, subprocess.TimeoutExpired):
        vision_ok = False
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write("VISION_GLOBAL: " + ("OK" if vision_ok else "FAILED") + "\n")
    # A busy ROS graph can make this acknowledgement time out even though
    # the controller accepts the subsequent FaceTracking command. Do not
    # block the whole voice mode on this best-effort gate; the result remains
    # visible in the log for diagnosis.
    for container, service, kind, body in calls:
        script = ("source /opt/walker/entrypoint.sh; timeout 8 "
                  "/opt/rosa/rosa_cli/bin/rosa service call --no-daemon "
                  + service + " " + kind + " '" + body + "'")
        try:
            result = subprocess.run(["docker", "exec", container, "bash", "-lc", script],
                                    capture_output=True, text=True, timeout=12, check=False)
            output = result.stdout + result.stderr
            ok = ("code=0" in output or "Motion mode set to" in output or "success=True" in output)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        results.append(ok)
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(("VISION_FACE_TRACKING" if "perception" in container else "VISION_HEAD_MODE")
                      + ": " + ("OK" if ok else "FAILED") + "\n")
    if enabled and not all(results):
        # Visual follow is an optional capability.  The independent
        # host_motion_player/View Follower may already be running; a busy
        # vendor ROS graph must not prevent the XiaoYou voice path from
        # starting after factory audio isolation has succeeded.
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write("VISION_FOLLOW_WARNING: 接口未全部确认，语音链路继续启动\n")

def start_vision_monitor():
    """Keep a live, inspectable target stream and classify left/center/right."""
    global VISION_MONITOR
    if VISION_MONITOR is not None and VISION_MONITOR.poll() is None:
        return
    script = r'''source /opt/walker/entrypoint.sh
exec /opt/rosa/rosa_cli/bin/rosa topic echo --no-daemon /vision/vision_perception_controller/faces_tracking
'''
    out = LOG_FILE.open("ab", buffering=0)
    VISION_MONITOR = subprocess.Popen(
        ["docker", "exec", "walker-perception.perception-1", "bash", "-lc", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    def pump():
        import re
        for raw in iter(VISION_MONITOR.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            m = re.search(r"yaw_degree['\"]?\s*[:=]\s*(-?\d+(?:\.\d+)?)", line)
            if m:
                yaw = float(m.group(1))
                side = "左" if yaw < -8 else ("右" if yaw > 8 else "中")
                out.write((f"VISION_TARGET: {side} yaw={yaw:.1f}\n").encode())
            elif "faces_tracking" in line and "[]" in line:
                out.write(b"VISION_TARGET: NONE\n")
    threading.Thread(target=pump, daemon=True).start()

def disable_vendor_behavior():
    # Visual tracking is hosted by the native behavior container. Ensure the
    # controller exists, then mute its conversation/audio inputs below.
    subprocess.run(["docker", "start", "walker-behavior.behavior-1"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=15, check=False)
    def call(container, service, kind, body, timeout=7, daemon=False):
        mode = "" if daemon else "--no-daemon "
        script = ("source /opt/walker/setup.bash; timeout 6 rosa service call "
                  + mode
                  + service + " " + kind + " '" + body + "'")
        result = subprocess.run(["docker", "exec", container, "bash", "-lc", script],
                                capture_output=True, text=True, timeout=timeout, check=False)
        output = result.stdout + result.stderr
        return any(marker in output for marker in ("code=0", "error_code=0", "success=True")), output

    def call_coze_status(timeout=2.0):
        """Run one bounded status query and clean the entire process group.

        The inner timeout is essential: docker exec can return while the ROSA
        client inside the audio container is still blocked.  The outer
        process-group cleanup is a second line of defence for docker/bash
        wrappers.  A new query must never start until this function returns.
        """
        script = ("source /opt/walker/setup.bash; "
                  "timeout --kill-after=0.5s 6s rosa service call "
                  "/audio/stream/coze/get_status "
                  "coze_msgs/srv/GetStatus '{}'" )
        proc = subprocess.Popen(
            ["docker", "exec", "walker-audio.audio-1", "bash", "-lc", script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout + 1.0)
            # The inner timeout has already reaped ROSA; record the invariant
            # explicitly so logs distinguish a cleaned timeout from a leak.
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write("QUERY_PROCESS_CLEANED\n")
            return output or "", proc.returncode
        except subprocess.TimeoutExpired:
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write("QUERY_TIMEOUT outer\n")
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2.0)
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write("QUERY_PROCESS_CLEANED\n")
            return "", -9

    steps = [
        ("退出 Coze", "walker-audio.audio-1", "/audio/stream/coze/leave_room", "sys_msgs/srv/Trigger", "{}"),
        ("打断厂家播报", "walker-registry.registry-1", "/audio/stream/coze/interrupt_action_audio", "coze_msgs/srv/InterruptActionAudio", "{}"),
        ("关闭厂家唤醒", "walker-audio.audio-1", "/system/control/wakeup_enable_set", "uworld_state_msgs/srv/SetBool", '{"data":false}'),
        ("关闭厂家连续对话", "walker-audio.audio-1", "/system/control/wakeup_followup_set", "uworld_state_msgs/srv/SetBool", '{"data":false}'),
        ("关闭厂家录音", "walker-audio.audio-1", "/audio/sense/set_record_audio_switch", "audio_msgs/srv/SetRecordAudioSwitch", '{"record_enable":false}'),
    ]
    def disable_one(step):
        label, container, service, kind, body = step
        # ROS service discovery is occasionally busy immediately after the
        # vendor audio container starts. Retry the safety switches before
        # refusing Xiaoyou mode; never treat a failed shutdown as success.
        for attempt in range(3):
            try:
                ok, _ = call(container, service, kind, body, timeout=10)
                if not ok:
                    ok, _ = call(container, service, kind, body, timeout=10, daemon=True)
                if ok:
                    return label, "OK"
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(1.0)
        return label, "UNAVAILABLE"
    # ROS service discovery on this robot serializes under load. Concurrent
    # calls routinely time out, so keep the three required checks sequential.
    active = [disable_one(step) for step in (steps[0], steps[1], steps[-1])]
    results = [active[0], active[1], disable_one(steps[2]),
               disable_one(steps[3]), active[2]]
    if any(label == "关闭厂家录音" and result != "OK" for label, result in results):
        label, result = disable_one(steps[-1])
        results = [(name, result if name == label else value) for name, value in results]
    with LOG_FILE.open("a", encoding="utf-8") as log:
        for label, result in results:
            log.write(f"FACTORY_{label}: {result}\n")
    required_labels = {"退出 Coze", "打断厂家播报", "关闭厂家唤醒",
                       "关闭厂家连续对话", "关闭厂家录音"}
    if any(label in required_labels and result != "OK" for label, result in results):
        failed = ", ".join(f"{label}={result}" for label, result in results
                           if label in required_labels and result != "OK")
        raise RuntimeError("厂家音频隔离未完成：" + failed)
    if any(label == "关闭厂家录音" and result != "OK" for label, result in results):
        raise RuntimeError("厂家原生录音未确认关闭，小优模式禁止启动")
    # View Follower is owned by the independent host_motion_player process.
    # Do not initialize the vision ROS graph on the audio handoff path: a busy
    # perception service must not delay the historical XiaoYou startup chain.
    status_output = ""
    # leave_room is asynchronous on some firmware versions.  A successful
    # request only means it was accepted; wait for the authoritative status
    # service to report mode=1 before claiming the microphone.
    # The audio service briefly becomes unavailable while leave_room is
    # completing; give it a short settle period before polling.
    time.sleep(1.0)
    wait_started = time.monotonic()
    query_started = time.monotonic()
    try:
        # Historical chain performs one authoritative check only.  The query
        # itself is still hard-bounded and its complete process group is
        # reaped before failure is returned.
        status_output, _ = call_coze_status(timeout=6.0)
        if "mode=" in status_output:
            mode = status_output.split("mode=", 1)[1].split()[0].rstrip(")},")
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write(f"COZE_WAIT attempt=1 elapsed_total_ms={round((time.monotonic()-wait_started)*1000)} query_ms={round((time.monotonic()-query_started)*1000)} result=mode={mode}\n")
        else:
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write(f"COZE_STATUS_TIMEOUT attempt=1 elapsed_total_ms={round((time.monotonic()-wait_started)*1000)} query_ms={round((time.monotonic()-query_started)*1000)}\n")
    except (OSError, subprocess.TimeoutExpired):
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"COZE_STATUS_TIMEOUT attempt=1 elapsed_total_ms={round((time.monotonic()-wait_started)*1000)} query_ms={round((time.monotonic()-query_started)*1000)}\n")
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write("FACTORY_COZE_MODE: " + ("1 IDLE" if "mode=1" in status_output else "NOT_IDLE") + "\n")
    if "mode=1" not in status_output:
        raise RuntimeError("厂家 Coze 未处于 mode=1 空闲，小优模式禁止启动")

    # Historical chain handles the vendor behavior container only after the
    # authoritative mode=1 safety check.  This remains a best-effort action
    # on the vendor container; no vendor files or services are modified.
    subprocess.run(["docker", "kill", "walker-behavior.behavior-1"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=3, check=False)
    state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                            "walker-behavior.behavior-1"], capture_output=True,
                           text=True, timeout=3)
    if state.stdout.strip() not in ("true", "false"):
        raise RuntimeError("厂家行为容器状态无法确认，小优模式禁止启动")


def start_voice():
    global VOICE
    with LOCK:
        pid = current_pid()
        if pid:
            behavior = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                                       "walker-behavior.behavior-1"], capture_output=True, text=True, timeout=3)
            if behavior.stdout.strip() == "true":
                disable_vendor_behavior()
            return f"小优模式已经运行，PID={pid}"
        LOG_FILE.write_text("", encoding="utf-8")
        disable_vendor_behavior()
        log = LOG_FILE.open("ab", buffering=0)
        VOICE = subprocess.Popen(["python3", str(ROOT / "robot_voice.py")], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        PID_FILE.write_text(str(VOICE.pid))
        # Gaze is an optional parallel capability; it must never block voice startup.
        threading.Thread(target=GAZE.enable, name="native-gaze", daemon=True).start()
        return f"小优模式正在启动，PID={VOICE.pid}"

def switch_character(character_id: str):
    """Switch role data atomically; hot-restart only our child when active."""
    global VOICE
    with LOCK:
        current = load_character(CHARACTERS_DIR)
        if current.character_id == character_id:
            return current
        selected = select_character(CHARACTERS_DIR, character_id)
        pid = current_pid()
        if pid:
            try: os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            deadline = time.time() + 5
            while alive(pid) and time.time() < deadline: time.sleep(.1)
            if alive(pid):
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            PID_FILE.unlink(missing_ok=True)
            log = LOG_FILE.open("ab", buffering=0)
            log.write(("CHARACTER_SWITCH: " + selected.character_id + "\n").encode("utf-8"))
            VOICE = subprocess.Popen(["python3", str(ROOT / "robot_voice.py")], cwd=ROOT,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            PID_FILE.write_text(str(VOICE.pid))
        return selected

def stop_voice():
    global VOICE, VISION_MONITOR
    with LOCK:
        # Interrupt robot-side audio before terminating the child process so a
        # queued TTS/Coze sentence cannot continue after the web Stop button.
        interrupt_vendor_audio()
        try:
            GAZE.disable()
        except Exception:
            pass
        try:
            set_visual_follow(False)
        except Exception:
            pass
        if VISION_MONITOR is not None:
            try:
                VISION_MONITOR.kill()
            except ProcessLookupError:
                pass
            VISION_MONITOR = None
        pid = current_pid()
        if pid:
            try: os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            deadline = time.time() + 3
            while alive(pid) and time.time() < deadline: time.sleep(.2)
            if alive(pid):
                try: os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        VOICE = None
        PID_FILE.unlink(missing_ok=True)
        restore_native()
        return "已退出小优模式并恢复厂家原生唤醒"

def status():
    running = current_pid() is not None
    try: logs = LOG_FILE.read_text(errors="replace")[-12000:]
    except FileNotFoundError: logs = "暂无小优语音日志"
    return running, logs

def mode_state(running, logs):
    if SERVICE_WAITING:
        return "厂家语音服务初始化中"
    if not running:
        return "厂家原生模式"
    mic_lines = [line for line in logs.splitlines() if line.startswith("MIC_READY ")]
    if mic_lines:
        path = Path(mic_lines[-1].removeprefix("MIC_READY ").strip())
        try:
            if time.time() - path.stat().st_mtime > 5:
                return "麦克风中断，正在自动重连"
        except OSError:
            return "麦克风中断，正在自动重连"
    last_mic = logs.rfind("MIC_READY ")
    if logs.rfind("XIAOYOU_READY") < last_mic:
        return "正在播放欢迎词" if logs.rfind("XIAOYOU_INTRO") > last_mic else "正在启动麦克风"
    if last_mic < 0:
        return "正在启动麦克风"
    return "小优模式运行中"

def shutdown(_signum=None, _frame=None): stop_voice()

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            if self.path == "/start": start_voice()
            elif self.path == "/stop": stop_voice()
            elif self.path == "/character":
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                character_id = (form.get("character_id") or [""])[0]
                switch_character(character_id)
            else: self.send_error(404); return
        except Exception as exc:
            with LOG_FILE.open("a", encoding="utf-8") as log:
                log.write("MODE_SWITCH_ERROR: " + str(exc) + "\n")
            self.send_error(503, str(exc)); return
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()
    def do_GET(self):
        if self.path == "/logs":
            _, logs = status()
            data = logs.encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        running, logs = status()
        state = mode_state(running, logs)
        character = load_character(CHARACTERS_DIR)
        if self.path == "/state":
            data = json.dumps({"state": state, "gaze_state": GAZE.state,
                               "face_id": GAZE.face_id,
                               "character_id": character.character_id,
                               "character_name": character.name}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        color = "#16a34a" if state == "小优模式运行中" else "#eab308" if running else "#64748b"
        cards = "".join(
            '<button class="role%s" type="submit" name="character_id" value="%s"><i>%s</i><b>%s</b><small>%s</small></button>' %
            (" active" if item.character_id == character.character_id else "",
             html.escape(item.character_id),
             html.escape(str(item.data.get("icon", "✦"))),
             html.escape(item.name),
             html.escape(str(item.data.get("response_style", {}).get("tone", "自然交流"))))
            for item in list_characters(CHARACTERS_DIR)
            if item.character_id in {"libai", "liqingzhao", "teacher", "companion", "sichuan"})
        body = f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>小优多 IP 控制台</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#080d22;color:#f5f3ff;font-family:"Noto Sans SC","Microsoft YaHei",sans-serif}}.hero{{min-height:220px;padding:34px 6%;display:flex;align-items:center;justify-content:space-between;background:linear-gradient(90deg,#0b102b 0%,#16143b 52%,#241947 100%);border-bottom:1px solid #252b58}}.brand h1{{font-size:46px;margin:0 0 8px;letter-spacing:4px}}.brand p{{font-size:17px;color:#c3c5e6;margin:0}}.online{{display:flex;align-items:center;gap:22px}}.status{{min-width:300px;padding:20px 28px;background:#171e42;border:1px solid #3a4071;border-radius:18px}}.status strong{{display:block;font-size:24px;color:{color};margin-bottom:8px}}.status small{{color:#b5b9df}}.actions{{display:flex;gap:16px}}button{{border:0;border-radius:14px;color:#fff;cursor:pointer}}.actions button{{padding:18px 34px;font-size:20px;box-shadow:0 8px 20px #0003}}.start{{background:linear-gradient(135deg,#c18bdd,#8d75bc)}}.stop{{background:linear-gradient(135deg,#4563a6,#293d78)}}.nav{{height:76px;display:flex;align-items:center;gap:44px;padding:0 6%;background:#0e1430;border-bottom:1px solid #252b58;color:#c9ccef;font-size:18px}}.nav .active{{height:76px;display:flex;align-items:center;border-bottom:3px solid #e79aff;color:#fff}}.main{{padding:34px 6%;max-width:1500px;margin:auto}}.heading{{display:flex;align-items:baseline;gap:22px;margin-bottom:20px}}.heading h2{{font-size:28px;margin:0}}.heading span{{color:#9298be}}.roles{{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}}.role{{min-height:145px;padding:20px;background:linear-gradient(135deg,#37295a,#151b45);border:1px solid #454477;display:flex;flex-direction:column;justify-content:flex-end;align-items:flex-start;text-align:left;position:relative}}.role.active{{border:2px solid #efa7ff;box-shadow:0 0 24px #c18bdd55}}.role b{{font-size:21px}}.role small{{color:#c3c5e7;margin-top:6px}}.role i{{position:absolute;right:18px;top:16px;font-size:34px}}.logcard{{margin-top:30px;background:#11183a;border:1px solid #303766;border-radius:18px;padding:22px}}.logcard h2{{margin:0 0 14px;font-size:24px}}pre{{margin:0;min-height:150px;max-height:330px;overflow:auto;white-space:pre-wrap;background:#050816;border-radius:12px;padding:18px;color:#d8dcff;font:13px/1.65 ui-monospace,monospace}}.footer{{padding:20px 6%;display:flex;gap:30px;color:#aeb4db;border-top:1px solid #252b58}}@media(max-width:900px){{.hero{{gap:20px;align-items:flex-start;flex-direction:column}}.roles{{grid-template-columns:repeat(2,1fr)}}.online{{flex-wrap:wrap}}}}@media(max-width:560px){{.roles{{grid-template-columns:1fr}}.brand h1{{font-size:34px}}}}
</style>
<header class="hero"><div class="brand"><h1>小优多 IP 控制台</h1><p>当前 IP：<strong id="role_name">{html.escape(character.name)}</strong> · {html.escape(str(character.data.get('identity', '数字人格')))}</p></div><div class="online"><div class="status"><strong id="mode_state">● {state}</strong><small>角色配置独立 · 可直接切换</small></div><div class="actions"><form method="post" action="/start"><button class="start">▶&nbsp; 启动对话</button></form><form method="post" action="/stop"><button class="stop">■&nbsp; 关闭对话</button></form></div></div></header>
<nav class="nav"><span class="active">☰　对话模式</span><span>▥　机器人状态</span><span>⚙　系统设置</span><span>◷　对话记录</span><span style="margin-left:auto">⚙　让每一次对话，都更有温度</span></nav>
<main class="main"><div class="heading"><h2>选择 IP</h2><span>运行中切换会自动播放新角色开场白</span></div><form class="roles" method="post" action="/character">{cards}</form><section class="logcard"><h2>对话记录（实时日志）</h2><pre id="logs">{html.escape(logs)}</pre></section></main><footer class="footer"><span>◉ 语音识别</span><span>◉ 独立 TTS</span><span>◉ 固定 Demo 直出</span><span>◉ 统一高音量</span></footer>
<script>const e=document.getElementById('logs'),s=document.getElementById('mode_state'),n=document.getElementById('role_name');function refresh(){{fetch('/logs').then(r=>r.text()).then(t=>{{const atBottom=e.scrollHeight-e.scrollTop-e.clientHeight<30;e.textContent=t;if(atBottom)e.scrollTop=e.scrollHeight;}});fetch('/state').then(r=>r.json()).then(v=>{{s.textContent='● '+v.state;n.textContent=v.character_name;s.style.color=v.state==='小优模式运行中'?'#16a34a':v.state==='厂家原生模式'?'#64748b':'#eab308'}})}};e.scrollTop=e.scrollHeight;setInterval(refresh,1000);</script>'''
        data = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *_args): pass

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown); atexit.register(shutdown)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
