"""动作判定自测：把 _decide 的 prompt 原样打给模型，看它挑不挑得对。

只测判定，不执行——执行要开浏览器，另一件事。
"""
import json
import os
import re
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
from vision import _api_key  # noqa: E402

MAIN = os.path.join(ROOT, "feibijiubi", "main.py")
src = open(MAIN, encoding="utf-8").read()
i = src.find("ACT_HELP = ")
j = src.find('"""', src.find('"""', i) + 3)
ACT_HELP = src[i:j + 3].split('"""')[1].lstrip("\\\n")

SYSTEM = (
    "你在判断他这句话是不是在给你派活。只判断，不执行。\n\n"
    "你能干的活儿（act 只能从这里面挑一个）：\n"
    f"{ACT_HELP}\n"
    "判断规则：\n"
    "- 看的是「他有没有让我做事」，不是「他问我会不会」。\n"
    "- 「你会刷抖音吗」是在问能力，算 none，但回答要照实说会。\n"
    "- 「分享个视频」「来个视频看看」这种光秃秃的也算 share_video。\n"
    "- 「给我发张图」「发个表情」算 sticker。\n"
    "- 他要是自己在那说、在吐槽、在提问，一律 none。\n"
    "- 确实在派活但你不想干，就用 refuse，理由必须是你自己的\n"
    "  （懒得动、刚刷完、凭什么使唤我、现在不想看）。\n"
    "- 不管选哪个，都不许写「做不到」「不会」「没权限」「发不了」，那些是假的。\n\n"
    '只输出一行 JSON，不要解释：{"act":"动作名","why":"一句人话"}'
)

YES = {
    "给我分享个视频": "share_video",
    "刷会儿抖音去": "browse",
    "发个说说": "qzone",
    "给我发张表情包": "sticker",
    "去抖音回我": "say_dm",
    "看下我发你那视频": "watch_last",
    "这条给我点个赞": "like",
    "收藏一下这条": "collect",
    "去评论区捡点表情": "grab",
    # 这不是派活，但她真去看反而是对的——比就着标题瞎聊强。单独放，不算错。
}
SOFT = {"这条视频讲了啥": ("watch_last", "none")}
NO = ["今天好累啊", "你会刷抖音吗", "你喜欢哪件衣服"]


def ask(text):
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"他在QQ说：{text}"},
            ],
            "temperature": 0.1,
            "max_tokens": 200,
            "thinking": {"type": "disabled"},
        },
        timeout=40,
    )
    raw = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*?\}", raw, re.S)
    return json.loads(m.group(0)) if m else {"act": "?", "raw": raw}


bad = 0
for s, want in YES.items():
    d = ask(s)
    ok = d.get("act") == want
    bad += 0 if ok else 1
    print(("OK  " if ok else "错  ") + f"{s:<14} -> {d.get('act')} (要 {want}) {d.get('why') or ''}")
for s in NO:
    d = ask(s)
    ok = d.get("act") == "none"
    bad += 0 if ok else 1
    print(("OK  " if ok else "错  ") + f"{s:<14} -> {d.get('act')} (要 none) {d.get('why') or ''}")

print("bad =", bad)
for s, want in SOFT.items():
    d = ask(s)
    print(f"软判 {s:<14} -> {d.get('act')} (接受 {want})")
