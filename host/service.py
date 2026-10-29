# -*- coding: utf-8 -*-
"""菲比刷抖音 —— 宿主侧本地服务。

容器里的 AstrBot 插件通过 http://host.docker.internal:8791 调这里，
真正的 Edge 浏览器在这台机器上跑（必须同一个进程用 Playwright，否则跨线程会报错）。

启动：python service.py
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from config import ASTRBOT_DATA, DATA_CTR, LOG_PATH, PORT, TOKEN
from engine import COVER_DIR, SHOT_DIR, DouyinEngine
import stickers

LOG = LOG_PATH

# 容器里的 /AstrBot/data 就是宿主这个目录，插件传容器路径进来，这里换回宿主路径
DATA_HOST = ASTRBOT_DATA


def host_path(p: str) -> str:
    p = (p or "").replace("\\", "/").strip()
    if p.startswith(DATA_CTR):
        p = DATA_HOST + p[len(DATA_CTR):]
    return p

_engine: DouyinEngine | None = None
_lock = threading.Lock()
# 最后一次"正经操作"的时间。私信轮询要避开它，否则刚切到推荐流刷视频，
# 下一轮轮询又把页面拉回 /chat，两件事互相打断。
_last_activity = 0.0
# 视频类操作（刷/看/点赞）会把页面切走，这段时间私信轮询一律让路。
# 只靠 _last_activity 的 45 秒窗口不够：一次 share_full 七十秒，中途就被抢回来了。
_busy_until = 0.0


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_engine() -> DouyinEngine:
    global _engine
    if _engine is None:
        _engine = DouyinEngine(headless=False)
        _engine.start()
        log("浏览器已启动")
    return _engine


class Handler(BaseHTTPRequestHandler):
    server_version = "feibi-douyin/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---------- 工具 ----------
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if self.headers.get("X-Token") == TOKEN:
            return True
        self._json({"ok": False, "error": "bad token"}, 401)
        return False

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            self._json({"ok": True, "pid": id(_engine) if _engine else 0, "time": time.time()})
            return
        if u.path == "/shot":
            p = SHOT_DIR + r"\last.png"
            try:
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._json({"ok": False}, 404)
            return
        if u.path == "/cover":
            idx = int(q.get("i", ["0"])[0])
            name = (q.get("name", [""])[0] or "").strip()
            if name:
                # 只放行纯文件名，防止路径穿越
                if not name.replace(".", "").isalnum():
                    self._json({"ok": False}, 400)
                    return
                p = os.path.join(COVER_DIR, name)
            else:
                p = os.path.join(COVER_DIR, f"{idx}.jpg")
            try:
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._json({"ok": False}, 404)
            return
        if not self._auth():
            return
        global _last_activity, _busy_until
        if u.path != "/dm_read":
            _last_activity = time.time()
        if u.path in ("/watch", "/share_full", "/pick_link", "/react", "/share_to_dm", "/dm_img", "/comment_stickers", "/share_panel", "/share_card"):
            _busy_until = time.time() + 150
        try:
            if u.path == "/report":
                with _lock:
                    self._json(get_engine().report())
                return
            if u.path == "/watch":
                scrolls = int(q.get("scrolls", ["2"])[0])
                picks = int(q.get("picks", ["2"])[0])
                t0 = time.time()
                with _lock:
                    res = get_engine().session(scrolls=scrolls, picks=picks)
                res["cost_s"] = round(time.time() - t0, 1)
                log(f"watch -> scanned={res.get('scanned')} picked={len(res.get('picked') or [])} {res['cost_s']}s")
                self._json(res)
                return
            if u.path == "/share":
                with _lock:
                    res = get_engine().read_share()
                log(f"share -> ok={res.get('ok')} from={res.get('from')} id={res.get('aweme_id')}")
                self._json(res)
                return
            if u.path == "/share_full":
                t0 = time.time()
                with _lock:
                    res = get_engine().share_full()
                res["cost_s"] = round(time.time() - t0, 1)
                log(
                    f"share_full -> ok={res.get('ok')} id={res.get('aweme_id')} "
                    f"asr={len(res.get('transcript') or '')}字 note={res.get('asr_note') or '-'} "
                    f"{res['cost_s']}s"
                )
                self._json(res)
                return
            if u.path == "/peek":
                with _lock:
                    res = get_engine().peek()
                self._json(res)
                return
            if u.path == "/pick_link":
                with _lock:
                    res = get_engine().pick_link()
                log(f"pick_link -> ok={res.get('ok')} id={res.get('aweme_id')} title={(res.get('title') or '')[:30]}")
                self._json(res)
                return
            if u.path == "/share_to_dm":
                text = (q.get("text", [""])[0] or "").strip()
                with _lock:
                    eng = get_engine()
                    pick = eng.pick_link()
                    if not pick.get("ok"):
                        res = pick
                    else:
                        body = f"{text} {pick['url']}".strip()
                        sent = eng.send_dm(body)
                        res = {**pick, **sent, "body": body}
                log(f"share_to_dm -> ok={res.get('ok')} id={res.get('aweme_id')} err={res.get('error') or '-'}")
                self._json(res)
                return
            if u.path == "/share_card":
                aid = (q.get("aweme_id", [""])[0] or "").strip()
                peer = (q.get("peer", [""])[0] or "").strip()
                t0 = time.time()
                with _lock:
                    res = get_engine().share_card(aid, peer)
                res.setdefault("cost_s", round(time.time() - t0, 1))
                log(
                    f"share_card -> ok={res.get('ok')} id={aid} peer={res.get('peer')} "
                    f"stage={res.get('stage')} {res['cost_s']}s err={res.get('error') or '-'}"
                )
                self._json(res)
                return
            if u.path == "/share_panel":
                aid = (q.get("aweme_id", [""])[0] or "").strip()
                with _lock:
                    res = get_engine().share_panel_probe(aid)
                names = [i.get("t") for i in (res.get("items") or [])]
                log(f"share_panel -> ok={res.get('ok')} items={names[:14]}")
                self._json(res)
                return
            if u.path == "/dm_probe":
                with _lock:
                    res = get_engine().dm_probe()
                log("dm_probe ok")
                self._json(res)
                return
            if u.path == "/dm":
                text = (q.get("text", [""])[0] or "").strip()
                with _lock:
                    res = get_engine().send_dm(text)
                log(f"dm -> ok={res.get('ok')} len={len(text)} err={res.get('error') or '-'}")
                self._json(res)
                return
            if u.path == "/dm_read":
                skip = (q.get("skip_if_busy", ["1"])[0] or "1") == "1"
                if skip and (time.time() < _busy_until or time.time() - _last_activity < 45):
                    self._json({"ok": False, "skip": True, "error": "刚有别的操作，先不抢页面"})
                    return
                limit = int(q.get("limit", ["15"])[0])
                with _lock:
                    res = get_engine().read_dm(limit=limit)
                log(f"dm_read -> n={res.get('n')} peer={res.get('peer')}")
                self._json(res)
                return
            if u.path == "/sticker_stats":
                self._json(stickers.stats())
                return
            if u.path == "/sticker_pick":
                mood = (q.get("mood", [""])[0] or "").strip()
                it = stickers.pick(mood)
                if not it:
                    self._json({"ok": False, "error": "库里没有"})
                    return
                self._json({
                    "ok": True,
                    **it,
                    "host_path": os.path.join(stickers.STICKER_DIR, it["file"]),
                    "ctr_path": f"{DATA_CTR}/plugin_data/feibijiubi/stickers/{it['file']}",
                })
                return
            if u.path == "/sticker_add_url":
                url = (q.get("url", [""])[0] or "").strip()
                src = (q.get("src", ["douyin"])[0] or "douyin").strip()
                res = stickers.add_url(url, src=src)
                log(f"sticker_add_url -> ok={res.get('ok')} mood={res.get('mood') or '-'} err={res.get('error') or '-'}")
                self._json(res)
                return
            if u.path == "/dm_img":
                p = host_path((q.get("path", [""])[0] or "").strip())
                with _lock:
                    res = get_engine().send_dm_image(p)
                log(f"dm_img -> ok={res.get('ok')} err={res.get('error') or '-'}")
                self._json(res)
                return
            if u.path == "/goto":
                url = (q.get("url", [""])[0] or "").strip()
                _busy_until = time.time() + 150
                with _lock:
                    ok = get_engine()._goto(url)
                self._json({"ok": bool(ok), "url": url})
                return
            if u.path == "/comment_stickers":
                limit = int(q.get("limit", ["3"])[0])
                scrolls = int(q.get("scrolls", ["3"])[0])
                with _lock:
                    res = get_engine().grab_comment_stickers(limit=limit, scrolls=scrolls)
                log(f"comment_stickers -> n={res.get('n')} found={res.get('found')}")
                self._json(res)
                return
            if u.path == "/react":
                like = (q.get("like", ["1"])[0] or "1") == "1"
                collect = (q.get("collect", ["0"])[0] or "0") == "1"
                with _lock:
                    res = get_engine().react_video(like=like, collect=collect)
                log(
                    f"react -> ok={res.get('ok')} like={like} collect={collect} "
                    f"err={res.get('error') or '-'}"
                )
                self._json(res)
                return
            if u.path == "/snap":
                with _lock:
                    p = os.path.join(SHOT_DIR, "last.png")
                    get_engine().page.screenshot(path=p)
                self._json({"ok": True, "path": p})
                return
            if u.path == "/detail":
                with _lock:
                    self._json(get_engine().detail())
                return
            if u.path == "/login":
                with _lock:
                    eng = get_engine()
                    ok = eng._goto("https://www.douyin.com/jingxuan")
                self._json({"ok": ok, "hint": "浏览器已打开，扫码登录即可"})
                return
        except Exception as e:
            log("ERR " + traceback.format_exc()[-800:])
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        global _engine, _busy_until
        if not self._auth():
            return
        try:
            if self.path.startswith("/restart"):
                global _engine
                with _lock:
                    if _engine:
                        _engine.close()
                    _engine = None
                    get_engine()
                self._json({"ok": True})
                return
            if self.path.startswith("/sticker_add"):
                q = parse_qs(urlparse(self.path).query)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ext = (q.get("ext", [".jpg"])[0] or ".jpg").strip()
                src = (q.get("src", ["qq"])[0] or "qq").strip()
                res = stickers.add_bytes(raw, ext, src=src)
                log(
                    f"sticker_add -> ok={res.get('ok')} dup={res.get('dup')} "
                    f"mood={res.get('mood') or '-'} err={res.get('error') or '-'}"
                )
                self._json(res)
                return
            if self.path.startswith("/eval"):
                # 调试通道：body 就是一段 JS，返回它的值。选器靠它反推，不靠猜。
                n = int(self.headers.get("Content-Length") or 0)
                code = self.rfile.read(n).decode("utf-8") if n else ""
                if not code.strip():
                    self._json({"ok": False, "error": "空 js"}, 400)
                    return
                # 手搓脚本在动页面，私信轮询先让开，否则查到一半被拉回 /chat
                _busy_until = time.time() + 120
                with _lock:
                    val = get_engine().eval_js(code)
                self._json({"ok": True, "value": val})
                return
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": False, "error": "not found"}, 404)


def _ensure_watchdog() -> None:
    """服务启动时顺手确认看门狗活着，没有就起一个。

    看门狗必须活在服务之外才有意义，但没人会记得开第二个窗口。
    服务本身是长期进程，由它拉起的孩子能活下来；pid 文件里那个还在就不重复起。
    """
    import subprocess
    import sys

    base = os.path.dirname(os.path.abspath(__file__))
    pidf = os.path.join(base, "watchdog.pid")
    try:
        if os.path.isfile(pidf):
            old = int(open(pidf, encoding="utf-8").read().strip() or 0)
            if old:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {old}", "/FO", "CSV"],
                    capture_output=True,
                    text=True,
                    encoding="gbk",      # 中文系统下 tasklist 是 GBK，按 UTF-8 解会在读线程里炸
                    errors="ignore",
                    timeout=15,
                )
                if str(old) in (r.stdout or ""):
                    log(f"watchdog 已在跑 pid={old}")
                    return
    except Exception:
        pass
    # 必须用 pythonw：有窗口的 python.exe 会占住一个控制台
    for cand in (
        os.path.join(sys.base_prefix, "pythonw.exe"),
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
    ):
        if os.path.isfile(cand):
            break
    else:
        cand = sys.executable
    try:
        subprocess.Popen(
            [cand, os.path.join(base, "watchdog.py")],
            cwd=base,
            creationflags=0x00000008 | 0x00000200,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"watchdog 已拉起 {os.path.basename(cand)}")
    except Exception:
        log("watchdog 拉起失败 " + traceback.format_exc()[-300:])


def main() -> None:
    log(f"listening 0.0.0.0:{PORT}")
    # 记下自己是用哪个解释器起的。看门狗可能跑在别的 Python 下（无窗口版），
    # 拉起服务时必须用这个——装了 playwright / requests 的才是它。
    try:
        import json as _json
        import sys as _sys

        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.json"),
                  "w", encoding="utf-8") as f:
            _json.dump({"py": _sys.executable, "pid": os.getpid(), "port": PORT}, f)
    except Exception:
        pass
    get_engine()  # 预热，避免第一条命令等 20 秒
    HTTP_SERVER = HTTPServer(("0.0.0.0", PORT), Handler)
    log("ready")
    # 拉看门狗必须放到 ready 之后：以前放在预热之前，看门狗一起来就探 /health，
    # 那时端口还没 listen，它判定"服务掉了"又拉起第二份，两份抢同一个 Edge
    # profile 和同一个端口，最后全挂。
    # 而且必须裹 try：拉起失败（比如 tasklist 输出编码不对）不能把主进程带走。
    try:
        _ensure_watchdog()
    except Exception:
        log("watchdog 处理失败 " + traceback.format_exc()[-300:])
    HTTP_SERVER.serve_forever()


if __name__ == "__main__":
    main()
