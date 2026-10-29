# -*- coding: utf-8 -*-
"""把 service.py 脱离父进程启动（PowerShell 的 Start-Process 在这个沙箱里创建不出来）。"""
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOG = os.path.join(BASE, "service.log")

DETACHED = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 先看是不是已经在跑。本机地址绕开系统代理，否则 Clash 会回 502 让人误判"没跑"
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with _op.open("http://127.0.0.1:8791/health", timeout=3) as r:
        print("ALREADY RUNNING:", r.read().decode())
        sys.exit(0)
except Exception:
    pass

f = open(LOG, "a", encoding="utf-8")
p = subprocess.Popen(
    [PY, "service.py"],
    cwd=BASE,
    stdout=f,
    stderr=subprocess.STDOUT,
    creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP,
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
)
print("SPAWNED", p.pid)

for i in range(30):
    time.sleep(2)
    try:
        with _op.open("http://127.0.0.1:8791/health", timeout=3) as r:
            print("HEALTH OK after", (i + 1) * 2, "s:", r.read().decode())
            break
    except Exception as e:
        print("wait", i, type(e).__name__)
else:
    print("NOT UP")
    print(open(LOG, encoding="utf-8", errors="ignore").read()[-2000:])
