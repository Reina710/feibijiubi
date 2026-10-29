# -*- coding: utf-8 -*-
"""AstrBot 数据库看门狗。

背景：data_v4.db 是 Windows 目录 bind mount 进容器的，SQLite 走 WAL。
只要本机有进程从外面碰过这个文件（哪怕只是读），容器里那批 aiosqlite
连接就会集体报 `unable to open database file`，然后**一直坏到重启**。
这个脚本每隔几分钟看一眼日志，发现这类报错成串就把容器重启掉。

不改数据、不动配置，唯一动作是 `docker restart astrbot`。
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "dbguard.log"
STATE = HERE / "_dbguard_state.txt"
CONTAINER = "astrbot"
WINDOW = "8m"      # 看最近多久的日志
THRESHOLD = 3      # 报错行数达到多少判定为坏了
COOLDOWN = 600     # 重启后 10 分钟内不再动手（旧报错还在窗口里，否则会连环重启）


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def main() -> int:
    if STATE.exists():
        try:
            last = float(STATE.read_text(encoding="utf-8").strip())
            if time.time() - last < COOLDOWN:
                return 0
        except Exception:
            pass

    p = subprocess.run(
        ["docker", "logs", "--since", WINDOW, CONTAINER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        log(f"读日志失败: {(p.stderr or '').strip()[:200]}")
        return 0

    text = (p.stdout or "") + (p.stderr or "")
    n = text.count("unable to open database file")
    if n < THRESHOLD:
        return 0

    log(f"检测到 {n} 条数据库报错，重启容器")
    STATE.write_text(str(time.time()), encoding="utf-8")
    r = subprocess.run(
        ["docker", "restart", CONTAINER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode == 0:
        log("重启完成，等 12s 看是否还在报错")
        time.sleep(12)
        p2 = subprocess.run(
            ["docker", "logs", "--since", "30s", CONTAINER],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        t2 = (p2.stdout or "") + (p2.stderr or "")
        log("重启后 30s 内报错数: %d" % t2.count("unable to open database file"))
    else:
        log(f"重启失败: {(r.stderr or '').strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
