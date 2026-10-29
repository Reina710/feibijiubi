"""画面理解：从视频里抽几帧，交给 deepseek-flash 描述。

为什么不直接把图塞给主模型：AstrBot 那边 provider 的 modalities 是空的，
图片会被丢掉（实测 config 里 `"modalities": []`）。这里自己调 API，
输出是纯文字，拼进事实块最稳，也不动主链路。

模型选型踩过的坑：
- deepseek-chat 现在已被官方路由到 V4.1 Flash，也能吃图；
- 但第一次测 max_tokens=400 时，400 全被 reasoning 吃掉，content 是空串。
  所以这里给足 2000，并且 reasoning_content 只当兜底、不当正文。
"""
import base64
import json
import os
import shutil
import subprocess

from config import CONFIG_JSON

CFG = CONFIG_JSON
MODEL = os.environ.get("FEIBI_VISION_MODEL", "deepseek-flash")
API = "https://api.deepseek.com/chat/completions"
MAX_FRAMES = 4

PROMPT = (
    "这几张是同一个短视频按时间顺序的截图。用中文回答：\n"
    "1) 画面里是谁、在做什么、什么场景（说到人物外形和动作）；\n"
    "2) 画面上出现的文字，逐条照抄（字幕、标题、水印都算）；\n"
    "3) 这是什么类型的视频（真人 / 动画 / AI生成 / 游戏录屏）。\n"
    "只写看到的，看不清就说看不清，不推测剧情。"
)


def _find_key(obj):
    """配置里的 key 是列表（['sk-xxx']），不是字符串，只认 str 会返回 None。"""
    if isinstance(obj, str):
        return obj if obj.startswith("sk-") and len(obj) > 20 else None
    if isinstance(obj, dict):
        if "key" in obj:
            got = _find_key(obj["key"])
            if got:
                return got
        for v in obj.values():
            got = _find_key(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v)
            if got:
                return got
    return None


def _api_key():
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    try:
        cfg = json.loads(open(CFG, encoding="utf-8-sig").read())
    except Exception:
        return None
    return _find_key(cfg)


def extract_frames(mp4, out_dir, n=MAX_FRAMES):
    """按总时长均匀抽 n 帧。视频短就少抽几帧，别拿重复画面凑数。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return [], "宿主机没装 ffmpeg"
    os.makedirs(out_dir, exist_ok=True)
    duration = 0.0
    try:
        p = subprocess.run(
            [exe, "-i", mp4, "-hide_banner"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
        import re

        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr or "")
        if m:
            duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass

    # 跳过开头 0.5s 和结尾 1s，避开黑场和片尾
    usable = max(duration - 1.5, 1.0)
    n = max(1, min(n, int(usable // 2) or 1))
    frames = []
    for i in range(n):
        t = 0.5 + usable * (i + 0.5) / n
        out = os.path.join(out_dir, f"f_{i:02d}.jpg")
        try:
            subprocess.run(
                [exe, "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{t:.2f}", "-i", mp4,
                 "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", out],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            continue
        if os.path.exists(out) and os.path.getsize(out) > 3000:
            frames.append(out)
    return frames, ""


def describe(mp4):
    """返回 {'ok': bool, 'text': str, 'note': str}。失败不抛异常，让上层照常走 ASR。"""
    key = _api_key()
    if not key:
        return {"ok": False, "text": "", "note": "没读到 deepseek key"}

    out_dir = os.path.join(os.path.dirname(mp4), "frames")
    frames, err = extract_frames(mp4, out_dir)
    if not frames:
        return {"ok": False, "text": "", "note": err or "抽帧失败"}

    content = [{"type": "text", "text": PROMPT}]
    for f in frames:
        try:
            with open(f, "rb") as fh:
                b = base64.b64encode(fh.read()).decode()
        except Exception:
            continue
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b}"},
        })

    try:
        import requests

        r = requests.post(
            API,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 800,
                "temperature": 0.2,
                # 不关思考的话 4 帧要 11s，其中 1600 tokens 全花在 reasoning 上；
                # 关掉后 2~4s，质量没掉（实测同一批帧描述的关键点一样全）。
                "thinking": {"type": "disabled"},
            },
            timeout=150,
        )
    except Exception as e:
        return {"ok": False, "text": "", "note": f"{type(e).__name__}: {e}"}

    if r.status_code != 200:
        return {"ok": False, "text": "", "note": f"HTTP {r.status_code}: {r.text[:120]}"}

    try:
        msg = r.json()["choices"][0]["message"]
    except Exception:
        return {"ok": False, "text": "", "note": "返回格式不对"}

    text = (msg.get("content") or "").strip()
    if not text:
        # reasoning 还没写完就被截断时才会走到这，拿来兜底总比空着强
        text = (msg.get("reasoning_content") or "").strip()
    if not text:
        return {"ok": False, "text": "", "note": "模型没输出"}
    return {"ok": True, "text": text[:1500], "note": ""}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        sys.exit("用法: python vision.py <视频文件>")
    print(json.dumps(describe(sys.argv[1]), ensure_ascii=False, indent=1))
