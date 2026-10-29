# -*- coding: utf-8 -*-
"""两个方向的跨平台意图判定自测：QQ→抖音 / 抖音→QQ。"""
import re
import sys

QQ_DM_SEND = re.compile(
    r"(抖音|那边|私信).{0,4}(回我|回你|回一句|回个话|回条|回一下|回消息)"
    r"|(回我|回一句|回消息|回一条).{0,6}(抖音|那边|私信)"
    r"|去抖音(说|发|回)"
)
ABILITY_DENY = re.compile(r"回不了|发不出去|发不了|没法回|只能发视频|连抖音都回")

DM_QQ_SEND = re.compile(
    r"qq.{0,6}(回我|回你|回一句|回个|回条|回一下|说话|说一句|发我|聊)"
    r"|(去|到|来|上)\s*qq.{0,4}(回|说|发|聊|找)"
    r"|(回|说|发|聊).{0,4}qq",
    re.I,
)
DM_QQ_DENY = re.compile(
    r"qq.{0,8}(回不了|发不出去|发不了|说不了|上不去|去不了)"
    r"|(回不了|发不出去|发不了).{0,8}qq",
    re.I,
)

bad = 0
print("=== 抖音私信里：他叫去 QQ ===")
YES = ["你去qq回我", "qq上回我", "去QQ说", "回qq消息", "你QQ回我一句", "来qq聊"]
NO = ["在qq跟你说", "qq空间发了没", "抖音这次是你发的吗", "发啥视频", "你抖音视频看了没"]
for s in YES:
    hit = bool(DM_QQ_SEND.search(s))
    print(("OK  " if hit else "漏了") + "  " + s)
    bad += 0 if hit else 1
for s in NO:
    hit = bool(DM_QQ_SEND.search(s))
    print(("误抓" if hit else "OK  ") + "  " + s)
    bad += 1 if hit else 0

print("=== 她嘴硬说 QQ 回不了 ===")
for s in ["菲比啾比（我QQ回不了）", "菲八（QQ那边发不出去）", "菲比比（回不了，qq上不去）",
          "菲比啾比（能发，别听我瞎说）"]:
    print(("拦 " if DM_QQ_DENY.search(s) else "放 ") + s)

print("=== 原有 QQ→抖音 回归 ===")
for s in ["你在抖音回我", "那你抖音回我一句", "抖音这次是你发的吗", "发啥视频"]:
    print(("命中" if QQ_DM_SEND.search(s) else "不抓") + "  " + s)

print("bad =", bad)
