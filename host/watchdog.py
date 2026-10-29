# -*- coding: utf-8 -*-
"""宿主服务看门狗。

service.py 挂了没人知道：插件那边只会一直读到 ok=False，然后她就开始
「我点不开」。这个进程每 30 秒问一次 /health，连续两次没回应就重新拉起。

只管两件事：
1. 8791 没了 → 跑 start_service.py
2. AstrBot 的 6185 没了 → docker restart astrbot

不做的事：杀进程。宿主的 Python 一旦被强杀，Edge 的子进程可能留在那儿，
再启动就会撞 profile 锁（SingletonLock），比现在更难收拾。
所以进程还在、端口也在、只是 HTTP 不答的情况，这里只报警不动手。
"""
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import config  # noqa: E402

LOG = os.path.join(BASE, "watchdog.log")
PID_FILE = os.path.join(BASE, "watchdog.pid")
CHECK_EVERY = 30          # 检查间隔（秒）
FAILS_TO_RESTART = 2      # 连续几次没回应才动手，避免一次抖动就重启
RESTART_COOLDOWN = 180    # 两次拉起之间至少隔这么久
DETACHED = 0x00000008
NEW_GROUP = 0x00000200


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
            with open(LOG, encoding="utf-8", errors="ignore") as f:
                tail = f.read()[-200_000:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.write(tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def alive(url: str, timeout: float = 4.0) -> bool:
    req = urllib.request.Request(url, headers={"X-Token": config.TOKEN})
    # 本机地址必须绕开系统代理：机器上开着 Clash(127.0.0.1:7890) 时，
    # urllib 会把 127.0.0.1 也丢给代理，代理回 502，这里就以为服务死了，
    # 于是每 180 秒拉一份新的，最后好几份服务抢同一个 Edge profile。
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def port_open(port: int) -> bool:
    import socket

    s = socket.socket()
    s.settimeout(1.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def py_for_service() -> str:
    """挑一个能跑 service.py 的解释器。

    看门狗自己可能挂在无窗口的 pythonw 下，那个环境没装 playwright，
    用它拉起服务等于白拉。service.py 启动时会把自己的解释器写进 runtime.json，
    这里照抄；实在没有才退回 sys.executable。
    """
    env = (os.environ.get("FEIBI_PYTHON") or "").strip()
    if env and os.path.isfile(env):
        return env
    meta = os.path.join(BASE, "runtime.json")
    if os.path.isfile(meta):
        try:
            import json

            with open(meta, encoding="utf-8") as f:
                p = (json.load(f) or {}).get("py") or ""
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    return sys.executable


def spawn_service() -> None:
    """拉起 service.py。start_service.py 自己会先探一次健康，不会起两份。"""
    f = open(os.path.join(BASE, "service.log"), "a", encoding="utf-8")
    subprocess.Popen(
        [py_for_service(), "start_service.py"],
        cwd=BASE,
        stdout=f,
        stderr=subprocess.STDOUT,
        creationflags=DETACHED | NEW_GROUP,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def only_one() -> bool:
    """同一个看门狗只许跑一个。"""
    try:
        if os.path.exists(PID_FILE):
            old = int(open(PID_FILE, encoding="utf-8").read().strip() or 0)
            if old and old != os.getpid():
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {old}", "/FO", "CSV"],
                    capture_output=True,
                    text=True,
                    encoding="gbk",
                    errors="ignore",
                    timeout=15,
                )
                if str(old) in (r.stdout or ""):
                    return False
        open(PID_FILE, "w", encoding="utf-8").write(str(os.getpid()))
        return True
    except Exception:
        return True


def main() -> None:
    if not only_one():
        print("已有看门狗在跑")
        return
    log(f"watchdog 启动 pid={os.getpid()} port={config.PORT}")
    svc_fail = 0
    ast_fail = 0
    last_spawn = 0.0
    # 冷启动宽限：服务要起 Edge、加载模型，几十秒内端口是不通的。
    # 没这段宽限，看门狗一睁眼就判定死亡，再拉一份，两份互抢 profile 全崩。
    time.sleep(CHECK_EVERY * 2)

    while True:
        # ---- 宿主服务 ----
        if alive(f"http://127.0.0.1:{config.PORT}/health"):
            if svc_fail:
                log("宿主服务恢复")
            svc_fail = 0
        else:
            svc_fail += 1
            if port_open(config.PORT):
                # 端口在、HTTP 不答：进程卡住了。不杀，只记。
                log(f"8791 端口在但 /health 无回应（第 {svc_fail} 次），先不动手")
            elif (
                svc_fail >= FAILS_TO_RESTART
                and time.time() - last_spawn > RESTART_COOLDOWN
            ):
                log(f"宿主服务掉了（连续 {svc_fail} 次），重新拉起")
                try:
                    spawn_service()
                    last_spawn = time.time()
                except Exception as e:
                    log(f"拉起失败：{type(e).__name__}: {e}")

        # ---- AstrBot ----
        if port_open(6185):
            ast_fail = 0
        else:
            ast_fail += 1
            if ast_fail >= FAILS_TO_RESTART and time.time() - last_spawn > RESTART_COOLDOWN:
                log("AstrBot 6185 没开，重启容器")
                try:
                    subprocess.run(
                        ["docker", "restart", "astrbot"],
                        capture_output=True,
                        timeout=180,
                    )
                    last_spawn = time.time()
                except Exception as e:
                    log(f"重启容器失败：{type(e).__name__}: {e}")

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
