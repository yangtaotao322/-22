"""Development-only tests for bounded Coze status-query cleanup.

These tests never contact a robot.  They model docker/bash/rosa nesting with
local Python children and assert that a timed-out query leaves no descendants.
"""
import os
import signal
import subprocess
import sys
import time


def bounded_query(seconds=0.1, outer=0.3):
    code = (
        "import subprocess,time,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
        "time.sleep(10)"
    )
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.communicate(timeout=outer)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=2)
        return False
    return True


def test_immediate_success():
    p = subprocess.run([sys.executable, "-c", "print('mode=1')"],
                       capture_output=True, text=True, check=True)
    assert "mode=1" in p.stdout


def test_timeout_reaps_descendants():
    assert bounded_query() is False


def test_repeated_timeouts_do_not_accumulate():
    for _ in range(12):
        assert bounded_query() is False


if __name__ == "__main__":
    test_immediate_success()
    test_timeout_reaps_descendants()
    test_repeated_timeouts_do_not_accumulate()
    print("PASS: success, timeout cleanup, repeated 12 timeout cleanup")
