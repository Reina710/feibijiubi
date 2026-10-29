# -*- coding: utf-8 -*-
"""表情包库：存图、打标、按情绪挑一张。

目录跟 AstrBot 容器共享（bind mount），所以插件那边能直接用同一个 index.json，
发图时只需要把宿主机路径换成容器内路径。

设计取舍：
- 不给图打「多标签」，只打一个情绪名——人设里的表情标记本来就只有一个，
  多标签在挑选时反而要算权重，收益不值。
- 挑图按 used 升序 + 同分随机，避免连着三次发同一张。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time

import requests

from config import STICKER_DIR
from vision import _api_key

INDEX = os.path.join(STICKER_DIR, "index.json")

# 和人设里的表情名一一对应，别自己加
MOODS = (
    "撇嘴 发呆 得意 害羞 睡 大哭 调皮 呲牙 微笑 难过 酷 抓狂 偷笑 "
    "可爱 白眼 疑问 晕 鼓掌 坏笑 委屈 快哭了 亲亲 可怜 抱拳 挥手 激动 憨笑 赞 爱心"
).split()

MAX_BYTES = 4 * 1024 * 1024
OK_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

LABEL_PROMPT = (
    "这是一张聊天表情包。只回一个 JSON，不要解释：\n"
    '{"mood":"<情绪名>","desc":"<12字内画面描述>","usable":true/false}\n'
    "情绪名只能从这个表里选一个，最贴的：\n"
    + "、".join(MOODS)
    + "\nusable=false 的情况：截图、长图、纯文字图、风景照、真人自拍、看不清的。"
)


def _load() -> dict:
    try:
        with open(INDEX, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def _save(d: dict) -> None:
    os.makedirs(STICKER_DIR, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)  # 原子换，容器那边不会读到半个文件


def _sig(raw: bytes) -> str:
    return hashlib.sha1(raw).hexdigest()[:16]


def add_bytes(raw: bytes, ext: str = ".jpg", src: str = "qq", label: bool = True) -> dict:
    """收一张图进库。重复图直接返回旧的。"""
    if len(raw) < 600:
        return {"ok": False, "error": "图太小"}
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": "图太大"}
    sid = _sig(raw)
    d = _load()
    for it in d["items"]:
        if it.get("id") == sid:
            return {"ok": True, "dup": True, **it}

    os.makedirs(STICKER_DIR, exist_ok=True)
    ext = ext.lower() if ext.lower() in OK_EXT else ".jpg"
    name = sid + ext
    path = os.path.join(STICKER_DIR, name)
    with open(path, "wb") as f:
        f.write(raw)

    item = {
        "id": sid,
        "file": name,
        "src": src,
        "at": int(time.time()),
        "used": 0,
        "mood": "",
        "desc": "",
    }
    if label:
        got = label_file(path)
        if not got.get("usable", True):
            try:
                os.remove(path)
            except Exception:
                pass
            return {"ok": False, "error": "不适合当表情包", "desc": got.get("desc", "")}
        item["mood"] = got.get("mood") or ""
        item["desc"] = got.get("desc") or ""

    d["items"].append(item)
    _save(d)
    return {"ok": True, "dup": False, **item, "total": len(d["items"])}


def add_file(path: str, src: str = "qq", label: bool = True) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    return add_bytes(raw, os.path.splitext(path)[1], src=src, label=label)


def add_url(url: str, src: str = "douyin", label: bool = True) -> dict:
    try:
        r = requests.get(url, timeout=25, headers={"Referer": "https://www.douyin.com/"})
        if r.status_code != 200 or len(r.content) < 600:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    ext = ".gif" if r.content[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
    return add_bytes(r.content, ext, src=src, label=label)


def label_file(path: str) -> dict:
    """让 flash 选一个情绪名。失败就返回 usable=True + 空 mood，别把图丢了。"""
    key = _api_key()
    if not key:
        return {"usable": True, "mood": "", "desc": ""}
    try:
        with open(path, "rb") as f:
            b = base64.b64encode(f.read()).decode()
    except Exception:
        return {"usable": True, "mood": "", "desc": ""}

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": LABEL_PROMPT},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}},
                        ],
                    }
                ],
                "max_tokens": 400,
                "temperature": 0.1,
                "thinking": {"type": "disabled"},
            },
            timeout=60,
        )
        txt = r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return {"usable": True, "mood": "", "desc": ""}

    txt = txt.strip("`").strip()
    if txt.startswith("json"):
        txt = txt[4:].strip()
    try:
        obj = json.loads(txt)
    except Exception:
        return {"usable": True, "mood": "", "desc": txt[:20]}
    mood = (obj.get("mood") or "").strip()
    if mood not in MOODS:
        mood = ""
    return {
        "usable": bool(obj.get("usable", True)),
        "mood": mood,
        "desc": (obj.get("desc") or "")[:20],
    }


def pick(mood: str = "", used_bump: bool = True) -> dict | None:
    """按情绪挑一张。没打上标的图当通用备用，只在该情绪没图时用。"""
    d = _load()
    items = [it for it in d["items"] if it.get("file")]
    if not items:
        return None
    pool = [it for it in items if it.get("mood") == mood] if mood else items
    if not pool and mood:
        pool = [it for it in items if not it.get("mood")]
    if not pool:
        return None
    pool.sort(key=lambda it: (int(it.get("used") or 0), -int(it.get("at") or 0)))
    top = pool[: max(1, min(4, len(pool)))]
    it = random.choice(top)
    if used_bump:
        it["used"] = int(it.get("used") or 0) + 1
        _save(d)
    return dict(it)


def stats() -> dict:
    d = _load()
    cnt: dict[str, int] = {}
    for it in d["items"]:
        k = it.get("mood") or "未分类"
        cnt[k] = cnt.get(k, 0) + 1
    return {"total": len(d["items"]), "by_mood": cnt, "dir": STICKER_DIR}


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
