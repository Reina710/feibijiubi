# -*- coding: utf-8 -*-
"""改完代码要重载时，手动重启宿主服务。

    python restart_service.py

不能直接 taskkill 完就走：新进程由本脚本 spawn 出来，跟着本脚本的控制台一起活；
真有窗口的机器上没问题，但如果你是在别的进程里起的，记得让看门狗兜一下——
服务掉线 60 秒后它会自己拉（watchdog.py）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(BASE, "runtime.json")
meta = json.load(open(META, encoding="utf-8"))
PY = meta["py"]
OLD = int(meta.get("pid") or 0)
PORT = int(meta.get("port") or 8791)

# 本机地址绕开系统代理：开着 Clash 时 127.0.0.1 也会被丢给 7890，回 502
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

print("old pid:", OLD, "py:", PY)

if OLD:
    try:
        subprocess.run(["taskkill", "/PID", str(OLD), "/T", "/F"],
                       capture_output=True, timeout=30)
        print("killed")
    except Exception as e:
        print("kill fail", e)

# 等端口真的释放，否则新进程会撞 "address already in use"
for _ in range(20):
    try:
        OP.open(f"http://127.0.0.1:{PORT}/health", timeout=2)
        time.sleep(1.5)
    except Exception:
        break

LOG = os.path.join(BASE, "service.log")
f = open(LOG, "a", encoding="utf-8")
f.write("\n--- restart_service ---\n")
f.flush()
p = subprocess.Popen(
    [PY, "service.py"], cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
    creationflags=0x00000008 | 0x00000200,
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
)
print("spawned", p.pid)

for i in range(40):
    time.sleep(2)
    try:
        with OP.open(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            print("health ok after", (i + 1) * 2, "s:", r.read().decode())
            sys.exit(0)
    except Exception:
        pass
print("NOT UP")
print(open(LOG, encoding="utf-8", errors="ignore").read()[-2500:])
