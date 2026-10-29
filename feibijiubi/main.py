# -*- coding: utf-8 -*-
"""菲比·刷抖音。

真的开浏览器刷：宿主侧跑着 host/service.py，由本机 Edge 打开抖音精选，
滚动几屏、按兴趣挑几条、把封面和标题拿回来。这个插件只负责两件事：

1. 把「刷到的东西」翻译成她的口气说出去（带封面图）；
2. 把这次刷到的东西记进上下文，后面他问「你刚看啥呢」她能接上。

不在这里写任何抓取逻辑——浏览器在容器外面，插件只管 HTTP 调。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

DATA_DIR = Path(__file__).resolve().parents[2] / "plugin_data/feibijiubi"
STATE_FILE = DATA_DIR / "last.json"
SHARE_STATE = DATA_DIR / "share_state.json"
DM_STATE = DATA_DIR / "dm_state.json"
ROUTINE_STATE = DATA_DIR / "routine_state.json"
DB_FILE = Path(__file__).resolve().parents[2] / "data_v4.db"

# 抖音私信轮询：她得能自己看见他发的消息，而不是等他回 QQ 再转述
DM_POLL = 15          # 每轮间隔（读一次约 5 秒）
DM_QUIET = 8          # 回完这条后至少隔这么久再回下一条，防连发

# 他跟她在 QQ 里约好的作息：每天 19:00-20:00 自己刷一次抖音。
# 约定写在聊天记录里不算数，得有真任务跑，不然就是"答应了但没做"。
ROUTINE_HOUR = 19
ROUTINE_CHECK = 300   # 5 分钟看一次，整个小时窗口内补跑

# 主动甩视频的节奏：白天到睡前，隔一阵发一条，一天别超过几条
SHARE_GAP = 2.2 * 3600     # 两条之间至少隔这么久
SHARE_CHECK = 900          # 每 15 分钟看一次
SHARE_DAY_MAX = 3          # 每天最多主动分享几条
SHARE_FROM_HOUR = 10       # 10:00 之前不发
SHARE_TO_HOUR = 23         # 23:00 之后不发
TZ = ZoneInfo("Asia/Shanghai")

# 宿主侧服务。容器里走 host.docker.internal，跑在同一台机器上时退回 127.0.0.1。
SERVICE_URLS = [
    "http://host.docker.internal:8791",
    "http://127.0.0.1:8791",
]
TOKEN = os.environ.get("FEIBI_TOKEN", "feibi-dy-2026")
TIMEOUT = 150

# 只有真的在问「能不能看到内容」才去开浏览器。
# 「抖音」这个词太泛：他问「抖音这次是你发的吗」也被抓去看视频，
# 结果先喊一句「等下，我点开看看」，再答一个不相干的问题——左右脑互博就是这么来的。
SEE_HINT = re.compile(r"看|视频|链接|分享|刷到|打不开")
# 归属问句：问的是「这条是不是你发的」，不是「里面讲了什么」
OWNER_HINT = re.compile(r"你发的|是不是你|是你发|你发我|谁发的|你有抖音|你抖音")
# 「我看了」是他自己看完来汇报，不是叫她去看，别又开一遍浏览器
SELF_WATCHED = re.compile(r"我看了|我看过了|我看完了|我已经看了")
# 「再看一次抖音」是要再刷推荐流，不是回头再看那条旧分享。
# 之前只按 SEE_HINT 判，一律走读分享：缓存里还是上次那条，她能说的只有「嗯」。
WATCH_HINT = re.compile(
    r"再刷|刷会|刷一会儿|刷一刷|刷一下|刷两|刷点|再看.{0,4}抖音|看会抖音|看会儿抖音|再逛"
)

# 「发条说说」「发个空间」= 要真去 QQ 空间发，不是嘴上答应。
# 走 NapCat 的扩展接口 send_qzone_msg（不是标准 OneBot 11 能力）。
QZONE_HINT = re.compile(r"发.{0,4}(说说|动态)|(说说|动态).{0,4}(发|写)|发.{0,3}空间|空间发")

# 等浏览器时先垫一句。固定同一句连着出现，看着像复读机
WAIT_LINES = [
    "菲比啾比（等下，我刷会儿）",
    "菲比啾比（等会，我划两下）",
    "菲比啾比（行，我看看）",
    "菲比啾比（这就刷）",
]
PEEK_LINES = [
    "菲比啾比（等下，我点开看看）",
    "菲比啾比（等会，我看眼那个）",
    "菲比啾比（让我点开）",
    "菲比啾比（行，我看看）",
]

# 私信里被催着看视频。之前私信轮询没有这个分支，她只能照聊天记录编，
# 编出来就是「催什么」——事实一句没有，火气全是自己加的。
DM_VIEW_HINT = re.compile(
    r"看完|看没看|看了吗|看了没|去.?看|赶紧看|快看|点开|自己看|你去看|看下|看一眼|"
    r"看.{0,4}视频|视频.{0,3}(看|呢|没)|最后一个|最后那条|最新|刚发|新发"
)
# 他在纠错：说的不是刚才那条。命中就把缓存作废，重新回去读最新那张卡片。
# 「不是，我说最后一个」以前没命中任何规则，她只能顺着上下文把旧视频再讲一遍。
DM_FRESH_HINT = re.compile(
    r"不是|最后一个|最后那条|最新|刚发|新发|没看|重看|再看一遍|再发我|哪条"
)
# 私信里垫场要短，不能像 QQ 那样一整句旁白甩过去
DM_WAIT_LINES = [
    "菲比（等下，我点开看看）",
    "菲比（行，我这就看）",
    "菲比（知道啦，我看）",
]

# 她能干的活儿，两边共用一份。写在 prompt 里让模型照着挑动作名，
# 也写进人设让她知道自己手上有这些东西——不然被问「你会不会」就开始编不会。
ACT_HELP = """\
share_video  挑一条抖音视频甩给他看（私信或 QQ，在哪说就发哪）
watch_last   点开他最后发来的那个分享视频，真看完
browse       去抖音推荐流随便刷几条，回来跟他讲
like         给眼前这条点个赞
collect      收藏眼前这条
sticker      发一张表情包
qzone        在 QQ 空间发一条说说
grab         去评论区捡几张表情包
say_dm       去抖音私信跟他说一句
say_qq       去 QQ 跟他说一句
none         这句话不是在派活
refuse       确实在派活，但你现在不想干"""

# 干活前她自己那句垫场该说什么。按动作给场景，别一律「我看看」。
ACT_SCENE = {
    "share_video": "你要去推荐流挑一条视频甩给他",
    "browse": "你要去推荐流刷一会儿",
    "watch_last": "你要点开他发来的那个视频看完",
    "grab": "你要去视频评论区捡几张表情",
    "qzone": "你要去 QQ 空间写一条说说",
}
# 这些动作要开浏览器、跑几十秒，不能卡在钩子里等
SLOW_ACTS = {"share_video", "browse", "watch_last", "grab", "qzone"}

# 动作判定的粗筛：一个"会派活"的字都没有的句子，不值得多花 1~3 秒问一次模型。
# 宁可漏（漏了就是普通聊天，她照常回），也别让每句「今天好累」都等一次。
ACT_HINT = re.compile(
    r"分享|发|刷|看|点|收藏|捡|说说|空间|视频|表情|图|来一|给我|帮我|替我|去|上|整"
)

# 垫场句里最容易被模型顺手写出来的火气。她要去干活，先甩一句「别催」就变味了。
ADLIB_BAN = re.compile(r"别催|急什么|烦不烦|催什么|关你什么事|哪个啊|哪条啊")
# 事实已给到手还在嘴硬的句子。命中就把回复换成按原话写的台词
DENY_HINT = re.compile(
    r"看不了|看不见|打不开|点不开|没权限|无法访问|没有手机|当我神仙|问我第.遍|再发我|发我一遍"
)

# 「你去抖音回我」= 让那边真收到一条，不是在这边解释自己发不出去。
# 以前没有这个分支：主模型手上没有任何「私信能发」的事实，只能顺着前几轮
# 的「没刷开」往下编，越编越顺——「我点不开」「我回不了」「只能发视频」。
QQ_DM_SEND = re.compile(
    r"(抖音|那边|私信).{0,4}(回我|回你|回一句|回个话|回条|回一下|回消息)"
    r"|(回我|回一句|回消息|回一条).{0,6}(抖音|那边|私信)"
    r"|去抖音(说|发|回)"
)
# 能力自贬。/dm 实测是通的，所以这类句子一出现就是编的，直接换掉。
ABILITY_DENY = re.compile(r"回不了|发不出去|发不了|没法回|只能发视频|连抖音都回")

# 反方向：私信里被叫「去 QQ 回我」。和 QQ→抖音对称，缺哪边她就说哪边发不了。
DM_QQ_SEND = re.compile(
    r"qq.{0,6}(回我|回你|回一句|回个|回条|回一下|说话|说一句|发我|聊)"
    r"|(去|到|来|上)\s*qq.{0,4}(回|说|发|聊|找)"
    r"|(回|说|发|聊).{0,4}qq",
    re.I,
)
# 私信里否认「QQ 能回」。QQ 这条路一直是通的，出现就是编的。
DM_QQ_DENY = re.compile(
    r"qq.{0,8}(回不了|发不出去|发不了|说不了|上不去|去不了)"
    r"|(回不了|发不出去|发不了).{0,8}qq",
    re.I,
)

# 「音（人话）」的最小配对。五个音长在前，否则「菲比」会先吃掉「菲比啾比」
SOUND = r"菲比啾比|菲八八|菲比比|菲八|菲比"
PAIR_RE = re.compile(r"(菲比啾比|菲八八|菲比比|菲八|菲比)\s*[（(]([^（()）]{1,60})[）)]")
EMOJI_RE = re.compile(r"\[[^\]]{1,8}\]")
# 和人设里的表情名一一对应，宿主侧 stickers.py 里是同一份表
STICKER_MOODS = {
    "撇嘴", "发呆", "得意", "害羞", "睡", "大哭", "调皮", "呲牙", "微笑", "难过",
    "酷", "抓狂", "偷笑", "可爱", "白眼", "疑问", "晕", "鼓掌", "坏笑", "委屈",
    "快哭了", "亲亲", "可怜", "抱拳", "挥手", "激动", "憨笑", "赞", "爱心",
}
ANY_SOUND = re.compile(SOUND)


def _enforce_format(text: str) -> str:
    """把回复压回「一个音 + 一个括号」，多出来的旁白和第二个括号全丢。

    人设里写得再清楚，模型偶尔还是会写三行：一句没括号的旁白 + 两个括号。
    这条规则是可判定的（数左括号），所以放在代码里执行，不指望它自觉。
    """
    if not text or not text.strip():
        return text

    # 模型偶尔加粗，**菲比啾比**（…）会让音和括号断开
    text = text.replace("**", "").replace("__", "")

    matches = list(PAIR_RE.finditer(text))
    if not matches:
        # 一个括号都没有 = 他真崩了那种正常说话，别动
        return text.strip()

    # 一般第一段就是她真正要说的那句；只有第一段短得没内容（「在」「嗯」）时
    # 才退而取后面信息最多的一段。之前一律取最长，会把她先说的正事丢掉。
    best = matches[0]
    if len(best.group(2).strip()) <= 4:
        best = max(matches, key=lambda m: len(m.group(2)))
    inner = best.group(2).strip()
    line = f"{best.group(1)}（{inner}）"

    # 同一行后面跟的表情带上
    line_end = text.find("\n", best.end())
    tail = text[best.end(): line_end if line_end != -1 else len(text)]
    emo = EMOJI_RE.search(tail)
    if emo:
        line += emo.group(0)

    # 人设允许的"第二行"：紧跟其后、不含音和括号、十五字以内
    if line_end != -1:
        rest = [ln.strip() for ln in text[line_end + 1:].splitlines() if ln.strip()]
        if rest:
            nxt = rest[0]
            if not ANY_SOUND.search(nxt) and not re.search(r"[（(]", nxt) and len(nxt) <= 15:
                line += "\n" + nxt

    return line


def _call(path: str, timeout: int = TIMEOUT) -> dict:
    """同步调用宿主服务；HTTP 阻塞，调用方负责丢进线程。"""
    last_err = ""
    for base in SERVICE_URLS:
        req = urllib.request.Request(base + path, headers={"X-Token": TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return {"ok": False, "error": last_err or "no route"}


def _post_sticker(raw: bytes, ext: str = ".jpg", src: str = "qq") -> dict:
    """把图片字节交给宿主入库。

    插件在容器里，读得到文件但打标要宿主的 flash，所以统一走 HTTP，
    顺便让宿主做去重和「这图适不适合当表情」的判断。
    """
    last_err = ""
    for base in SERVICE_URLS:
        req = urllib.request.Request(
            f"{base}/sticker_add?ext={urllib.parse.quote(ext)}&src={urllib.parse.quote(src)}",
            data=raw,
            headers={"X-Token": TOKEN, "Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return {"ok": False, "error": last_err or "no route"}


def _fetch_cover(idx: int = 0, name: str = "") -> str:
    """从宿主服务取封面存本地，返回路径。抖音 CDN 有 referer 校验，容器直连会 403。"""
    fname = name or f"cover_{idx}.jpg"
    path = DATA_DIR / fname
    for base in SERVICE_URLS:
        q = f"/cover?name={name}" if name else f"/cover?i={idx}"
        req = urllib.request.Request(base + q, headers={"X-Token": TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF":
                path.write_bytes(data)
                return str(path)
        except Exception:
            continue
    return ""


def _fmt_item(i: dict, idx: int) -> str:
    tags = "、".join(i.get("tags") or []) or "无"
    return (
        f"{idx}. 《{i.get('title') or '(无标题)'}》\n"
        f"   作者 @{i.get('author') or '?'}｜时长 {i.get('duration') or '?'}"
        f"｜点赞 {i.get('likes') or '?'}｜话题 {tags}"
    )


def _owner_block(st: dict | None) -> str:
    """回答「这条是不是你发的」用的事实块。

    她之前回过「我哪来的抖音号」——可私信就是从她号里发的。
    这里只给归属事实，不给视频内容，免得她又跑去"看"一遍。
    """
    title = ((st or {}).get("last_title") or "").strip()
    line = ((st or {}).get("last_line") or "").strip()
    got = ""
    if title or line:
        got = "你最近一次甩给他的是"
        if title:
            got += f"《{title}》"
        if line:
            got += f"，当时说的是「{line}」"
        got += "。"
    return f"""

【关于「抖音里那条是不是你发的」】
你有自己的抖音号，会刷也会往他私信里甩东西。{got}
- 他问是不是你发的、谁发的 → 直接认，别否认自己有号。
- 认的时候可以赖：发完就忘了、懒得说为什么挑这条。
- **这一轮没有新视频要看**：不要喊「我点开看看」，也不要扯视频里讲了什么。"""


def _share_block(share: dict) -> str:
    """把这一轮实际拿到的东西写成事实块。

    措辞必须和人设【链接、视频、语音】那一节对齐：
    出现「原话：」她才算看过；给了画面描述也算看过；只给标题就只许聊标题。
    """
    if not share.get("ok"):
        return f"""

【这一轮没拿到视频内容】
真实原因：{share.get('error') or '未知'}
- 属于最后一档，什么都没给。一句话带过，别自己编原因，也别让他重发。"""

    tr = (share.get("transcript") or "").strip()
    vs = (share.get("visual") or "").strip()
    # whisper 碰到纯 BGM 或 AI 配音，经常只吐「语语?」「啊」这种噪声。
    # 这种长度当原话用，她只能围着两个字瞎猜，还不如老实走画面档。
    if len(re.sub(r"[\s?？。，、!！…·~～\-]+", "", tr)) < 8:
        tr = ""
    parts = ["\n\n【这一轮系统抄给她的东西】"]

    if tr:
        parts += [
            "档位：① 有原话 → 这条视频你看过了。",
            f"标题：{share.get('text') or '(没写)'}",
            f"作者：@{share.get('author') or '?'}",
            f"原话：{tr[:1200]}",
        ]
        if vs:
            parts.append(f"你看到的画面：{vs[:1200]}")
        if share.get("comments"):
            parts.append("热评：" + " / ".join(c[:40] for c in share["comments"][:4]))
        parts.append(
            "- 就着原话或画面里具体的东西回，最好点出其中一句或一个画面。"
            "这一轮不许出现「看不了 / 打不开 / 没权限 / 我又没有手机」，出现即算没看。"
        )
    elif vs:
        # 纯 BGM / 没人说话的视频——以前只能退回标题，现在有画面了
        parts += [
            "档位：② 没听到人声，但你看到了画面 → 这条视频你看过了。",
            f"标题：{share.get('text') or '(没写)'}",
            f"作者：@{share.get('author') or '?'}",
            f"你看到的画面：{vs[:1200]}",
        ]
        note = f"（{share.get('asr_note')}）" if share.get("asr_note") else ""
        parts.append(
            f"- 语音没转出内容{note}，但画面你是真看到了。"
            "就画面里的人、动作、场景或上面的字说，别编画面里没有的东西。"
        )
    else:
        note = f"（{share.get('asr_note') or share.get('vision_note')}）" if (
            share.get("asr_note") or share.get("vision_note")
        ) else ""
        parts += [
            "档位：③ 只有标题，声音和画面都没拿到。",
            f"标题：{share.get('text') or '(没写)'}",
            f"作者：@{share.get('author') or '?'}",
            f"话题：{'、'.join(share.get('tags') or []) or '无'}",
            f"- 声音和画面都没拿到{note}。就标题里的词说，别把标题展开成画面和剧情。",
        ]
    parts.append("- 别复述这些字段，直接回他那句话。")
    return "\n".join(parts)


class _BrainEvent:
    """把一句抖音私信伪装成 AstrBot 事件，交给「记忆大脑」的接口。

    记忆大脑只挂在 on_llm_request 上注入，而私信是定时轮询出来的，手上没有
    事件对象。这里补一个最小的鸭子类型——它一共只用四个方法，够用就行。
    会话标识故意沿用 QQ 那条 umo：同一个人在两边的对话，本来就该是同一条记忆。
    """

    def __init__(
        self, umo: str, text: str, sender: str = "Destiny", sender_id: str = ""
    ) -> None:
        self.unified_msg_origin = umo
        self._text = text
        self._sender = sender
        self._sender_id = sender_id

    def get_message_str(self) -> str:
        return self._text

    def get_group_id(self) -> str:
        return ""

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender


@register("feibijiubi", "feibi", "她真的会刷抖音，然后跟你讲刷到了什么", "1.0.0")
class FeibiDouyin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._persona: str | None = None
        self._auto_at: dict[str, float] = {}
        # 刚写进长期记忆的原文指纹。同一批消息可能因为兜底重试再走一遍
        # _speak_dm，不挡一下就会在记忆里留下两条一样的。
        self._brain_seen: dict[int, float] = {}
        # 正在后台看视频的会话。长任务不能待在 on_llm_request 里占会话锁
        self._watching: set[str] = set()
        # 这一轮已经被派活路由接管的会话。后面几个钩子看到就让路，
        # 否则「给我分享个视频」会被读分享那条一起抓走，她讲的是上一张卡片。
        # 带时间戳，stop_event 把后面钩子全跳过时也不会永远卡住。
        self._act_claim: dict[str, float] = {}
        # umo -> (时间戳, share, 台词)。连问几遍就直接重发，不再重新转写
        self._share_cache: dict[str, tuple[float, dict, str]] = {}
        self._last_adlib: str = ""
        # umo -> (时间戳, 兜底台词)。事实给了她还说"看不了"时顶上
        self._guard: dict[str, tuple[float, str]] = {}
        # 主动甩视频的节奏状态（按 umo 存盘，重启不丢）
        self._share_all: dict[str, dict] = self._load_share_all()
        self._share_task: asyncio.Task | None = None
        # 抖音私信：最后回过哪条，重启不重复回
        self._dm_state: dict = self._load_dm_state()
        self._dm_task: asyncio.Task | None = None
        self._dm_busy = False
        # QQ 这边最近几轮说过什么。私信回复时要能看到，不然两边像两个人
        self._qq_recent: list[dict] = []
        # 表情库里现在真有哪几种情绪。写死 28 个名字没用——
        # 库里只有 5 张，她挑「偷笑」就静默发不出去。
        self._moods: tuple[float, list[str]] = (0.0, [])
        # 约好的作息：19 点自己刷一次抖音，按天记，重启不重刷
        self._routine: dict = self._load_routine()
        self._routine_task: asyncio.Task | None = None
        # 插件加载时直接在事件循环里，顺手把轮询挂上；不在循环里就走首次请求的兜底
        try:
            asyncio.get_running_loop()
            self._ensure_dm_loop()
            self._ensure_routine_loop()
        except RuntimeError:
            pass

    # ---------- 人设 ----------
    def _load_persona(self) -> str:
        if self._persona:
            return self._persona
        pid = "feibi"
        try:
            cfg = self.context.get_config()
            pid = cfg["agent_runner"]["config"]["persona"]["persona_id"] or pid
        except Exception:
            pass
        try:
            conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT system_prompt FROM personas WHERE persona_id=?", (pid,)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT system_prompt FROM personas ORDER BY length(system_prompt) DESC LIMIT 1"
                ).fetchone()
            conn.close()
            self._persona = (row[0] if row else "") or ""
        except Exception:
            logger.exception("[feibijiubi] 读人设失败")
            self._persona = ""
        return self._persona

    # ---------- 状态 ----------
    # ---------- 抖音私信里自己回话 ----------
    def _load_dm_state(self) -> dict:
        try:
            return json.loads(DM_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_dm_state(self) -> None:
        try:
            DM_STATE.write_text(
                json.dumps(self._dm_state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.exception("[feibijiubi] 写私信状态失败")

    def _dm_umo(self) -> str:
        """借一个会话标识去取 provider。私信回复用的是同一套模型和人格。"""
        for k in self._share_all.keys():
            if k:
                return k
        return ""

    def _ensure_dm_loop(self) -> None:
        if self._dm_task and not self._dm_task.done():
            return
        self._dm_task = asyncio.create_task(self._dm_loop())
        logger.info("[feibijiubi] 私信轮询已启动")

    async def _dm_loop(self) -> None:
        """每 15 秒看一眼抖音私信。

        浏览器在宿主那边，一次读要 5 秒左右，所以不能定太密；
        但也不能像 QQ 那样"等他先开口"——私信就是他不来 QQ 时用的。
        """
        await asyncio.sleep(25)  # 让 AstrBot 自己先起完
        while True:
            try:
                await self._dm_tick()
            except Exception:
                logger.exception("[feibijiubi] 私信轮询出错")
            await asyncio.sleep(DM_POLL)

    async def _dm_tick(self) -> None:
        if self._dm_busy:
            return
        self._dm_busy = True
        try:
            res = await asyncio.to_thread(_call, "/dm_read?limit=20", 90)
            if not res.get("ok"):
                return
            msgs = res.get("msgs") or []
            if not msgs:
                return
            last = msgs[-1]
            if last.get("mine"):
                return  # 最后一句是她说的，等着他回
            # 他连发几条时别只回最后一句：上一轮「来qq给我发一条 / 菲巴 / 速度」
            # 只处理了「速度」，那句让他去 QQ 的话被自己刷掉了。
            tail_msgs: list[dict] = []
            for m in reversed(msgs):
                if m.get("mine"):
                    break
                if (m.get("text") or "").strip():
                    tail_msgs.append(m)
            tail_msgs.reverse()
            text = "\n".join((m.get("text") or "").strip() for m in tail_msgs)
            if not text:
                return

            # 最新一条是不是分享卡片？卡片在 DOM 里只剩作者名，
            # 不认出来她就会就着名字聊（「记下了，聂时同局」）。
            # 卡片节点的 class 必带 ShareAweme，cls 本来就在 read_dm 的返回里，
            # 不用再开一次浏览器去探。
            is_share = any(
                m.get("share") or "ShareAweme" in (m.get("cls") or "")
                for m in tail_msgs
            )
            card = ""
            if is_share:
                last_his = tail_msgs[-1]
                card = (last_his.get("card") or last_his.get("text") or "").strip()
                logger.info(f"[feibijiubi] 私信里认到分享卡片：{card[:30]}")

            key = f"{res.get('peer') or ''}|{text}"
            if is_share:
                # 卡片只留作者名。同一个作者发两条，光看文字会当成「已经回过」，
                # 拿最近 6 条的文本当指纹——同一条卡片重复读到不变，换一条就变。
                sig = "/".join((m.get("text") or "")[:20] for m in msgs[-6:])
                key += "|" + sig
            if key == self._dm_state.get("last"):
                return  # 这条已经回过
            if time.time() - float(self._dm_state.get("last_at") or 0) < DM_QUIET:
                return

            # 他刚甩来一个视频：先点开看完再说话，别就着作者名瞎聊。
            if is_share and not DM_QQ_SEND.search(text):
                logger.info("[feibijiubi] 私信收到视频卡片，直接去看")
                if await self._dm_watch_and_reply(
                    msgs, "他刚甩过来一个视频", card=card
                ):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return

            # 他叫她去 QQ 说：真去 QQ 发一句。以前没这条，她只能编「QQ 回不了」。
            if DM_QQ_SEND.search(text):
                logger.info(f"[feibijiubi] 私信里让他去 QQ：{text[:30]}")
                if await self._dm_to_qq(text):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return

            # 他催着看视频：这句必须真去看。手上一个字的事实都没有，
            # 她只能拿语气顶回去——「催什么」就是这么来的。
            if DM_VIEW_HINT.search(text) and not SELF_WATCHED.search(text):
                logger.info(f"[feibijiubi] 私信里被催看，真去看：{text[:30]}")
                # 「不是，我说最后一个」——他在纠错，刚才那份缓存说的是别条
                if DM_FRESH_HINT.search(text) and self._dm_state.get("view"):
                    self._dm_state.pop("view", None)
                    self._save_dm_state()
                    logger.info("[feibijiubi] 他在纠错，丢弃缓存重读最新那张卡片")
                if await self._dm_watch_and_reply(msgs, text):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return

            # 他要是派了个活（发条视频、点个赞、发张图、去 QQ 说），先真干，再开口。
            # 以前私信里只认「看视频」「去 QQ」两类，其余她答应一句就没了下文——
            # 「你给我分享个视频」回了「行行行我找找」，然后什么都没发生。
            d = {"act": "none"}
            if ACT_HINT.search(text):
                d = await self._decide(self._dm_umo(), text, "抖音私信")
            act = d.get("act") or "none"
            extra = ""
            if act in SLOW_ACTS:
                # 慢的挪后台，不然 _dm_busy 一直占着，下一条消息十几秒没人看
                self._dm_state.update({"last": key, "last_at": time.time()})
                self._save_dm_state()
                asyncio.create_task(
                    self._act_bg(
                        self._dm_umo(), act, d.get("why") or "", "dm", text
                    )
                )
                return
            if act == "refuse":
                extra = (
                    f"\n\n【他刚让你干活，你不想干】\n"
                    f"你自己的理由：{d.get('why') or '懒得动'}\n"
                    "照你的脾气回他。可以说不去、可以说等会儿、可以谈条件；\n"
                    "但不许说「不会」「做不到」「没权限」——那些东西你手上都有。"
                )
            elif act not in ("", "none"):
                r = await self._run_act(
                    self._dm_umo(), act, d.get("why") or "", "dm", text
                )
                if r.get("spoken"):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return
                extra = (
                    f"\n\n【他刚让你干活，你已经干了】\n"
                    f"结果：{r.get('fact')}\n"
                    "说结果和态度，别邀功、别说「已完成」。"
                )

            line = await self._speak_dm(self._dm_umo(), msgs, text, extra=extra)
            if not line:
                return
            # 她要是把「QQ 我回不了」写出来了，说明又在凭印象编——直接真去 QQ。
            if DM_QQ_DENY.search(line):
                logger.info(f"[feibijiubi] 她想说 QQ 回不了（{line[:26]}），改真去发")
                if await self._dm_to_qq(text):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return

            # 没真看就编「我点不开/你再发我一遍」——这种话不许发出去，
            # 手上明明有浏览器，推给他重发是最难看的偷懒。
            if DENY_HINT.search(line):
                logger.info(f"[feibijiubi] 她想糊弄（{line[:26]}），改成真去看")
                if await self._dm_watch_and_reply(msgs, text):
                    self._dm_state.update({"last": key, "last_at": time.time()})
                    self._save_dm_state()
                    return
            ok = await self._dm_say_rich(line)
            logger.info(
                f"[feibijiubi] 私信回复 ok={ok} 他={text[:30]} 她={line[:40]}"
            )
            if ok:
                self._dm_remember(
                    [{"who": "他", "text": text}, {"who": "你", "text": line}]
                )
                self._dm_state.update({"last": key, "last_at": time.time()})
                self._save_dm_state()
        finally:
            self._dm_busy = False

    async def _dm_say(self, text: str) -> bool:
        """在抖音私信里发一句纯文字。"""
        try:
            r = await asyncio.to_thread(
                _call, "/dm?text=" + urllib.parse.quote(text), 120
            )
            return bool(r.get("ok"))
        except Exception:
            logger.exception("[feibijiubi] 私信发字失败")
            return False

    async def _live_moods(self) -> list[str]:
        """表情库里真有图的情绪，10 分钟缓存一次。"""
        ts, ms = self._moods
        if ms and time.time() - ts < 600:
            return ms
        try:
            st = await asyncio.to_thread(_call, "/sticker_stats", 10)
            ms = [k for k in (st.get("by_mood") or {}) if k]
            self._moods = (time.time(), list(ms))
        except Exception:
            logger.warning("[feibijiubi] 取表情库情绪失败", exc_info=True)
        return ms

    async def _dm_sticker(self, mood: str) -> bool:
        """在抖音私信里发一张该情绪的图。

        这条链 2026-10-30 实跑过：/dm_img 返回 ok，截图里图确实落在私信里。
        以前只是"代码写了没验"，所以一直不敢在对话里真调。
        """
        if not mood:
            return False
        try:
            it = await asyncio.to_thread(
                _call, "/sticker_pick?mood=" + urllib.parse.quote(mood), 30
            )
            p = (it.get("ctr_path") or "").strip()
            if not it.get("ok") or not p:
                return False
            await asyncio.sleep(0.6)  # 让文字先落地，像「说完补个图」
            r = await asyncio.to_thread(
                _call, "/dm_img?path=" + urllib.parse.quote(p), 200
            )
            ok = bool(r.get("ok"))
            logger.info(
                f"[feibijiubi] 私信发表情 mood={mood} ok={ok} "
                f"err={r.get('error') or '-'}"
            )
            return ok
        except Exception:
            logger.warning("[feibijiubi] 私信发表情失败", exc_info=True)
            return False

    async def _dm_say_rich(self, text: str) -> bool:
        """发一句字；句尾那个 [偷笑] 标记换成真图补过去。

        私信里不认 QQ 表情，标记会原样显示成「[偷笑]」四个字，所以必须先剥掉。
        模型想发图就给它留这一个口子，别的地方一律清干净。
        """
        raw = text or ""
        mood = ""
        for m in EMOJI_RE.finditer(raw):
            if m.group(0)[1:-1] in STICKER_MOODS:
                mood = m.group(0)[1:-1]
                break
        clean = EMOJI_RE.sub("", raw).strip()
        if not clean:
            return await self._dm_sticker(mood) if mood else False
        ok = await self._dm_say(clean)
        if ok and mood:
            asyncio.create_task(self._dm_sticker(mood))
        return ok

    def _dm_remember(self, pairs: list[dict]) -> None:
        """把私信里刚说过的几句存下来。

        私信那边每轮都是新 prompt（contexts=[]），QQ 这边也看不见私信，
        两套上下文互不相通。不记一份，他一问「那边回我」她就只能说
        「我回不了」——不是不会发，是不知道自己发过。
        """
        rec = list(self._dm_state.get("recent") or [])
        rec.extend(pairs)
        self._dm_state["recent"] = rec[-10:]

    async def _go_dm(self, umo: str, ask: str) -> None:
        """被叫「去抖音回我」时，那边真发一句，再回 QQ 说一声。"""
        line = ""
        try:
            line = await self._adlib(
                umo,
                ask,
                "你要在他的抖音私信里发一句话，你已经到那边了",
                fallback="菲比啾比（来了，说吧）",
            )
            line = EMOJI_RE.sub("", line or "").strip() or "菲比啾比（来了，说吧）"
            ok = await self._dm_say(line)
            if ok:
                self._dm_remember([{"who": "你", "text": line}])
                self._save_dm_state()
            logger.info(f"[feibijiubi] QQ 让他去抖音回 ok={ok} 她={line[:40]}")
            back = (
                "菲比啾比（发了，去那边看）"
                if ok
                else "菲比啾比（那边没发出去，你等我一下）"
            )
            await self.context.send_message(umo, MessageChain([Plain(back)]))
        except Exception:
            logger.exception("[feibijiubi] 去抖音发一句失败")

    def _qq_umo(self) -> str:
        """挑一条 QQ 会话用来发消息——私信里被叫「去 QQ 回我」时要落在这儿。"""
        for k in self._share_all:
            if k.startswith("napcat:"):
                return k
        return self._dm_umo()

    # ---------------- 长期记忆：接「记忆大脑」 ----------------
    # 记忆大脑是独立插件，装在同一个 AstrBot 里。它把注入钩子挂在
    # on_llm_request 上，只有走事件管线的对话才吃得到；抖音私信是这边定时
    # 轮询出来的，直接调 provider.text_chat，钩子根本不会触发。所以这里
    # 从插件注册表拿到它的实例，借它的检索/缓冲接口，把私信接回同一条记忆。

    BRAIN_PLUGIN = "astrbot_plugin_memory_brain"

    def _brain(self):
        """拿记忆大脑的实例；没装或没启用就返回 None，绝不因此影响回话。"""
        try:
            meta = self.context.get_registered_star(self.BRAIN_PLUGIN)
            return getattr(meta, "star_cls", None) if meta else None
        except Exception:
            logger.debug("[feibijiubi] 取记忆插件失败", exc_info=True)
            return None

    def _brain_event(self, text: str) -> _BrainEvent:
        """两个端共用 QQ 那条 umo——同一个人，不该被记成两份。"""
        return _BrainEvent(self._qq_umo() or self._dm_umo(), text)

    async def _brain_selfcheck(self) -> None:
        """启动后自检一次长期记忆接线。

        私信不走事件管线，接错了只有等他聊到旧事才暴露，那时已经答非所问。
        这里先探一遍：插件在不在、检索接口通不通，结论直接落日志。
        """
        await asyncio.sleep(15)
        brain = self._brain()
        if brain is None:
            logger.warning("[feibijiubi] 长期记忆未接上：没找到记忆插件")
            return
        try:
            block = await brain._memory_block(self._brain_event("视频 图片"))
        except Exception:
            logger.exception("[feibijiubi] 长期记忆自检失败")
            return
        if block:
            logger.info(f"[feibijiubi] 长期记忆已接上，自检召回 {len(block)} 字")
        else:
            logger.info("[feibijiubi] 长期记忆已接上，自检 query 没命中（库小属正常）")

    async def _brain_recall(self, text: str) -> str:
        """取一段和当前话题相关的长期记忆，拼进私信的 system prompt。"""
        brain = self._brain()
        if brain is None or not (text or "").strip():
            return ""
        try:
            block = await brain._memory_block(self._brain_event(text))
            if block:
                logger.info(f"[feibijiubi] 私信注入长期记忆 {len(block)} 字")
            return block or ""
        except Exception:
            logger.warning("[feibijiubi] 长期记忆检索失败", exc_info=True)
            return ""

    def _brain_note(self, text: str, who: str = "user") -> None:
        """把这一轮的话送进记忆大脑。

        who=user 走它自己的缓冲：攒够一批由它摘要成 episodic 记忆落库；
        who=bot 只补一条原话，给它统计语感节奏（它原本也是这么记回复的）。
        """
        brain = self._brain()
        text = (text or "").strip()
        if brain is None or len(text) < 2:
            return
        if who == "user":
            fp = hash(text)
            now = time.time()
            if now - (self._brain_seen.get(fp) or 0) < 120:
                return
            self._brain_seen[fp] = now
            if len(self._brain_seen) > 200:
                self._brain_seen = {k: v for k, v in self._brain_seen.items() if now - v < 600}
        try:
            event = self._brain_event(text)
            if who == "user":
                brain._capture_raw(event)
                brain._buffer_message(event)
            else:
                keep = int(brain.config.get("raw_keep", 2000) or 2000)
                brain.store.add_raw(event.unified_msg_origin, "bot", text, "bot", keep=keep)
        except Exception:
            logger.debug("[feibijiubi] 记忆写入失败", exc_info=True)

    async def _dm_to_qq(self, ask: str) -> bool:
        """反方向：私信里被叫「去 QQ 回我」，真去 QQ 发一句。

        和 _go_dm 对称。之前只有 QQ→抖音这一条，私信里一叫她去 QQ，
        她手上同样没有任何「QQ 能发」的事实，只能回「我 QQ 回不了」。
        """
        umo = self._qq_umo()
        if not umo:
            logger.warning("[feibijiubi] 没有 QQ 会话，去不了")
            return False
        try:
            line = await self._adlib(
                umo,
                ask,
                "你要去 QQ 那边给他发一句话，你已经到 QQ 了",
                fallback="菲比啾比（来了，QQ 上说）",
            )
            line = EMOJI_RE.sub("", line or "").strip() or "菲比啾比（来了，QQ 上说）"
            await self.context.send_message(umo, MessageChain([Plain(line)]))
            logger.info(f"[feibijiubi] 私信叫他去 QQ 回 她={line[:40]}")
            # 私信这边应一声，不然他盯着私信以为没人动
            await self._dm_say("菲比啾比（过去了，QQ 上说）")
            return True
        except Exception:
            logger.exception("[feibijiubi] 去 QQ 发一句失败")
            return False

    async def _dm_watch_and_reply(
        self, msgs: list, last_text: str, card: str = ""
    ) -> bool:
        """被催着看视频、或者他直接甩来一个视频时：先垫一句，再真看完，最后拿原话或画面回。

        返回 True 表示这条已经处理掉了。整个流程约 35 秒，
        垫场那句就是给这 35 秒留的，不然私信那边像已读不回。
        """
        umo = self._dm_umo()
        view = self._dm_state.get("view") or {}
        fresh = time.time() - float(view.get("at") or 0)
        share = view.get("share") or {}

        # 3 分钟内刚看过同一条，就别再开一遍浏览器（他要连着问两遍很常见）。
        # 但他换了一条新卡片（封面地址变了），缓存必须作废，否则讲的还是上一条。
        same = bool(card) and (view.get("card") or "") == card
        if not (share.get("ok") and fresh < 180 and (not card or same)):
            await self._dm_say(
                await self._adlib(
                    umo, last_text, "你要点开他私信里分享的那个视频，从头看一遍"
                )
            )
            share = await asyncio.to_thread(_call, "/share_full", 300)
            self._dm_state["view"] = {
                "at": time.time(),
                "ok": bool(share.get("ok")),
                "share": share,
                "card": card,
            }
            self._save_dm_state()
            logger.info(
                f"[feibijiubi] 私信看视频 ok={share.get('ok')} "
                f"asr={len(share.get('transcript') or '')}字 "
                f"visual={len(share.get('visual') or '')}字 "
                f"err={share.get('error') or share.get('vision_note') or '-'}"
            )

        line = await self._speak_dm(
            umo,
            msgs,
            last_text,
            extra=_share_block(share),
            watched=bool(share.get("ok")),
        )
        if not line:
            line = "菲比（看完了，你想问哪段）"
        if await self._dm_say_rich(line):
            logger.info(f"[feibijiubi] 私信看完回复 ok 他={last_text[:24]} 她={line[:40]}")
            return True
        return False

    async def _speak_dm(
        self, umo: str, msgs: list, last_text: str, extra: str = "", watched: bool = True
    ) -> str:
        """就着私信里那几句，回一句短话。

        extra 非空表示这一轮已经去看了视频，事实块会一并交给她；
        watched=False 表示去了但没看成，别让她假装看过。
        """
        provider = None
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception:
            logger.warning("[feibijiubi] 取 provider 失败", exc_info=True)
        if provider is None:
            return ""

        lines = []
        for m in msgs[-8:]:
            t = (m.get("text") or "").replace("\n", " ").strip()
            if not t:
                continue
            # 分享卡片在页面里只剩作者名，直接写进对话她会当成他说的话，
            # 然后就着名字聊（「记下了，聂时同局」）。这里把性质说清楚。
            if m.get("share") or "ShareAweme" in (m.get("cls") or ""):
                t = f"[他发来一个视频，作者是「{t}」]"
            lines.append(("你：" if m.get("mine") else "他：") + t[:120])
        ctx = "\n".join(lines)

        tail = (
            (
                "\n- 这一轮你真的看过了，就着上面的事实说一句；不许再说「一会儿去看」。"
                if watched
                else "\n- 这一轮没看成。上面写了原因，就直说没看成，别编视频里有什么，也别怪他。"
            )
            if extra and not extra.startswith("\n\n【他刚让你干活")
            else (
                "\n- 上面那件事已经真办完了（或者你明确不办）。就着结果说一句，"
                "别再说「等下我去办」。"
                if extra
                else "\n- 没真看成就直说没看成，别在这儿编视频里有什么。"
            )
        )
        # QQ 那边刚聊的内容。两套上下文本来互不相通：她在私信里看不见
        # 五分钟前在 QQ 上被交代过什么，于是同一件事要说两遍、规则也落不下来。
        # 只带最近 6 句，并且明说「接着聊别提是在哪说的」——人设里不许提平台。
        qq_lines = []
        for r in (self._qq_recent or [])[-6:]:
            t = (r.get("text") or "").replace("\n", " ").strip()
            if t:
                qq_lines.append(f"{r.get('who') or '他'}：{t[:70]}")
        if qq_lines:
            logger.info(f"[feibijiubi] 私信带上 QQ 上下文 {len(qq_lines)} 句")
        qq_block = (
            "\n【你们几分钟前刚说过的（真事，你记得）】\n"
            + "\n".join(qq_lines)
            + "\n这部分是你本来就该记得的内容，能自然地接上；别提「刚才在哪说的」。\n"
            if qq_lines
            else ""
        )
        # 长期记忆。QQ 那边由记忆大脑自己的钩子负责，私信不走事件管线，
        # 得主动去要一段：不然她在私信里对几天前的事完全没印象。
        mem_block = await self._brain_recall(last_text)
        if mem_block:
            mem_block = (
                "\n"
                + mem_block
                + "\n这些是以前真聊过的，能自然地接上就接，别硬提、别逐条复述。\n"
            )
        moods = await self._live_moods()
        mood_rule = (
            "想发图就在句尾带一个标记，只能从这些里挑："
            + "/".join(moods)
            + "。只许带一个，别的标记一律不写。"
            if moods
            else "库里现在没有图，别写任何 [xx] 这样的标记。"
        )
        system = self._load_persona() + f"""

【你现在在抖音私信里跟他打字】
这是刚才的对话，最后一句是他刚发过来的：
{ctx}
{qq_block}{mem_block}

要求：
- 完整按人设说：一个音 + 一个括号白话，一行。私信里也一样，别只剩括号。
- 就回最后那句，别超过 30 字。
- 可以损一句，但只损这件事，不损他这个人；不许凶，也不许把他推开。
- 他催你办正事，先把事给了再说风凉话；办不了就直说办不了。
- 不许说「催什么」「急什么」「烦不烦」「关你什么事」这类把人顶回去的话。
- 不许拿糯糯跟他比，「糯糯都不这么催我」这种话一句都不许有。
- 可以扯到你们之前聊过的事；提糯糯就正常提，别拿她当尺子量他。
- 情绪上来了用「菲八」。
- 这里是打字不是发语音：同一个音最多重复两次，别写一长串。
- {mood_rule}
- 不许让他重发视频（「你再发我一遍」这种），也不许拿「我点不开」当理由。
- 他说「最后一个」「不是那条」时，就是你自己没找对，认一句就去看，别问他要。
- 不提「私信」「抖音」「QQ」「平台」这些词。{tail}""" + extra

        try:
            resp = await provider.text_chat(
                prompt=last_text, system_prompt=system, contexts=[]
            )
            text = (resp.completion_text or "").strip()
            # 拿到回复之后再把他说的话记进去：模型调用炸了就不算发生过。
            self._brain_note(last_text, "user")
        except Exception:
            logger.exception("[feibijiubi] 生成私信回复失败")
            return ""
        text = re.sub(r"\n[ \t\u3000]*\n+", "\n", text).strip()
        # 抖音私信发不了 QQ 表情，[偷笑] 会原样变成文字。
        # 但库里有的情绪可以真发一张图，所以留一个标记到句尾，别的全剥掉。
        mood = ""
        for m in EMOJI_RE.finditer(text):
            if m.group(0)[1:-1] in STICKER_MOODS:
                mood = m.group(0)
                break
        text = EMOJI_RE.sub("", text).strip()
        if mood and text:
            text = text + mood
        # 整条只剩一句括号旁白时把壳剥掉——私信里那样太怪
        m = re.fullmatch(r"[（(]([^（）()]{2,60})[）)]", text)
        if m:
            text = m.group(1).strip()
        self._brain_note(text, "bot")
        return text[:80]

    async def _adlib(self, umo: str, text: str, scene: str, fallback: str = "") -> str:
        """现场编一句垫场话，别再从固定池子里抽。

        以前这里走 random.choice(PEEK_LINES / DM_WAIT_LINES)，抽来抽去就那四句。
        他一说「看抖音」必出「行，我看看」——固定的池子就是人机味的来源，
        而且和前面的对话一点关系都没有。现在让她自己说，编砸了才退回池子。
        fallback 由调用方给：去私信说话和点开视频不是一回事，兜底句不能共用。
        """
        fallback = fallback or random.choice(PEEK_LINES)
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception:
            provider = None
        if provider is None:
            return fallback

        system = self._load_persona() + f"""

【你手上正要干的事】{scene}
{('他刚说：' + text[:60]) if text else '没有新消息，是你自己在做事、主动跟他说一句。'}

说一句你**正在动手**的话，别回应他的语气。8~14 字，一个音 + 一个括号白话，一行。
- 只说你在做什么：点开了、翻着呢、正在加载、找着了。
- 不要提他在催，也不要评价他说话的方式。
- 可以顺嘴抱怨点小事，比如网慢、眼睛酸。
- 不要用「行」「知道了」这种接旨式开头。

样例（只学味道，不要照抄）：
菲比（手已经点上了，等我一下）
菲比啾比（正翻着呢，网有点慢）
菲比比（找着了，我先看两眼）
- 只输出这一句，不要解释，不要引号。"""

        last_try = ""
        for _ in range(2):
            ask = text or "（自己去）"
            if last_try:
                ask += f"\n（换一种说法，别再说「{last_try[:20]}」）"
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=ask, system_prompt=system, contexts=[]),
                    timeout=15,
                )
                line = _enforce_format((resp.completion_text or "").strip())
                line = EMOJI_RE.sub("", line).strip()
            except Exception:
                logger.warning("[feibijiubi] 垫场句没编出来，用兜底", exc_info=True)
                return fallback
            # 空的、太长的、带火气的、跟上一句一字不差的，都算没编好，重来一次
            if (
                line
                and len(line) <= 40
                and line != self._last_adlib
                and not ADLIB_BAN.search(line)
            ):
                self._last_adlib = line
                logger.info(f"[feibijiubi] 现编垫场：{line[:30]}")
                return line
            last_try = line
        return fallback

    # ---------- 动作路由：两边都能使唤她，她也能不干 ----------
    async def _decide(self, umo: str, text: str, where: str) -> dict:
        """看这句话是不是在派活。是的话给出动作名，不是就 none。

        以前两边各写一串正则，命中才动，命不中就交给模型自由发挥——
        「给我分享个视频」不属于任何一条正则，她只能答应一句「行行行我找找」，
        然后什么都没发生。软话是她编的，活儿得靠这个判出来。
        """
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception:
            provider = None
        if provider is None:
            return {"act": "none"}

        system = (
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
        prompt = f"他在{where}说：{text[:200]}"
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system, contexts=[]),
                timeout=12,
            )
            raw = (resp.completion_text or "").strip()
        except Exception:
            logger.warning("[feibijiubi] 判动作失败", exc_info=True)
            return {"act": "none"}
        m = re.search(r"\{.*?\}", raw, re.S)
        if not m:
            return {"act": "none"}
        try:
            d = json.loads(m.group(0))
        except Exception:
            return {"act": "none"}
        act = (d.get("act") or "none").strip()
        known = {a.split()[0] for a in ACT_HELP.strip().splitlines() if a.strip()}
        if act not in known:
            act = "none"
        logger.info(f"[feibijiubi] 判动作 {where} -> {act} why={d.get('why')} 他={text[:24]}")
        return {"act": act, "why": (d.get("why") or "").strip()}

    async def _say_at(self, where: str, umo: str, text: str) -> bool:
        """在哪说就在哪回。私信走浏览器，QQ 走适配器。"""
        if where == "dm":
            return await self._dm_say_rich(text)
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception:
            logger.warning("[feibijiubi] 回消息没发出去")
            return False

    async def _sticker_at(self, where: str, mood: str = "") -> bool:
        """挑一张表情发过去。私信和 QQ 是两套发送通道。"""
        q = f"/sticker_pick?mood={urllib.parse.quote(mood)}" if mood else "/sticker_pick"
        it = await asyncio.to_thread(_call, q, 60)
        if not it.get("ok"):
            return False
        path = it.get("path") or ""
        ctr = it.get("ctr_path") or path
        if where == "dm":
            r = await asyncio.to_thread(
                _call, "/dm_img?path=" + urllib.parse.quote(ctr), 240
            )
            return bool(r.get("ok"))
        if not path:
            return False
        try:
            await self.context.send_message(
                self._qq_umo() or "", MessageChain([Image.fromFileSystem(path)])
            )
            return True
        except Exception:
            logger.warning("[feibijiubi] QQ 发图失败", exc_info=True)
            return False

    async def _run_act(
        self, umo: str, act: str, why: str, where: str, text: str, bot=None
    ) -> dict:
        """真去干。返回给她用的事实，由她自己的嘴说出来。

        fact 是「发生了什么」的大白话，不是成品台词——她要损两句、要抱怨
        网慢，那是她的事；插件只保证这事真的发生了。
        """
        out = {"ok": False, "fact": "", "slow": False, "refuse": False}
        if act in ("", "none"):
            return out
        if act == "refuse":
            return {**out, "ok": True, "refuse": True, "fact": why or "不想动"}

        try:
            if act == "share_video":
                pick = await asyncio.to_thread(_call, "/pick_link", 240)
                if not pick.get("ok"):
                    return {**out, "fact": "推荐流里没刷出视频"}
                line = await self._speak_video(umo, pick)
                body = f"{line} {pick.get('url')}".strip()
                mode = "link"
                if where == "dm":
                    # 私信里要的是卡片，不是网址：走分享面板的「分享给朋友」。
                    card = {}
                    if pick.get("aweme_id"):
                        card = await asyncio.to_thread(
                            _call, f"/share_card?aweme_id={pick['aweme_id']}", 260
                        )
                    if card.get("ok"):
                        mode = "card"
                        if line:
                            await asyncio.to_thread(
                                _call, "/dm?text=" + urllib.parse.quote(line), 200
                            )
                        ok = True
                    else:
                        r = await asyncio.to_thread(
                            _call, "/dm?text=" + urllib.parse.quote(body), 240
                        )
                        ok = bool(r.get("ok"))
                else:
                    ok = await self._say_at("qq", umo, body)
                logger.info(
                    f"[feibijiubi] 干了 share_video ok={ok} mode={mode} id={pick.get('aweme_id')}"
                )
                return {
                    **out,
                    "ok": ok,
                    "spoken": True,  # 台词已经跟着卡片/链接一起发出去了
                    "fact": (
                        f"把「{(pick.get('title') or '')[:30]}」直接甩进他私信了"
                        if ok and mode == "card"
                        else f"挑了一条「{(pick.get('title') or '')[:30]}」发给他了"
                        if ok
                        else "视频没发出去"
                    ),
                    "title": pick.get("title") or "",
                }

            if act == "watch_last":
                self._dm_state.pop("view", None)
                self._save_dm_state()
                share = await asyncio.to_thread(_call, "/share_full", 320)
                if where == "dm":
                    if not share.get("ok"):
                        return {**out, "fact": "那条没打开"}
                    self._dm_state["view"] = {
                        "at": time.time(), "ok": True, "share": share
                    }
                    self._save_dm_state()
                    line = await self._speak_dm(
                        self._dm_umo(), [], text, extra=_share_block(share)
                    )
                    return {**out, "ok": True, "fact": "看完了", "line": line}
                self._share_cache[umo] = (
                    time.time(), share, await self._speak_share(umo, share)
                    if share.get("ok") else ""
                )
                return {
                    **out,
                    "ok": bool(share.get("ok")),
                    "fact": "看完了" if share.get("ok") else "那条没打开",
                    "share": share,
                }

            if act == "browse":
                session = await asyncio.to_thread(_call, "/watch?scrolls=3&picks=2", 260)
                if not session.get("ok"):
                    return {**out, "fact": "刷不动，页面卡了"}
                self._save_last(umo, {
                    "at": session.get("at"),
                    "picked": session.get("picked"),
                    "scanned": session.get("scanned"),
                })
                line = await self._speak(umo, session)
                return {
                    **out, "ok": True, "line": line,
                    "fact": f"刷了 {len(session.get('picked') or [])} 条",
                }

            if act in ("like", "collect"):
                want_like = act == "like"
                r = await asyncio.to_thread(
                    _call,
                    f"/react?like={1 if want_like else 0}"
                    f"&collect={0 if want_like else 1}",
                    180,
                )
                ok = bool(r.get("ok"))
                noun = "点赞" if want_like else "收藏"
                logger.info(f"[feibijiubi] 干了 {noun} ok={ok} {r.get('error') or ''}")
                return {
                    **out, "ok": ok,
                    "fact": f"{noun}了" if ok else f"{noun}没点成",
                }

            if act == "sticker":
                ok = await self._sticker_at(where)
                return {**out, "ok": ok, "fact": "表情发出去了" if ok else "库里没图"}

            if act == "grab":
                r = await asyncio.to_thread(_call, "/comment_stickers?limit=3&scrolls=2", 240)
                n = int(r.get("n") or 0)
                return {
                    **out, "ok": bool(r.get("ok")),
                    "fact": f"从评论区捡了 {n} 张表情" if r.get("ok") else "评论区没捡到",
                }

            if act == "qzone":
                if bot is None or not hasattr(bot, "call_action"):
                    return {**out, "fact": "这边发不了空间"}
                await self._post_qzone(umo, bot, text)
                return {**out, "ok": True, "spoken": True, "fact": "说说发了"}

            if act == "say_dm":
                line = await self._adlib(
                    umo, text, "你要去抖音私信跟他说一句话",
                    fallback="菲比啾比（来了，抖音上说）",
                )
                ok = await self._dm_say(EMOJI_RE.sub("", line).strip())
                if ok:
                    self._dm_remember([{"who": "你", "text": line}])
                    self._save_dm_state()
                return {
                    **out, "ok": ok, "spoken": True,
                    "fact": "去私信说了" if ok else "私信没发出去",
                }

            if act == "say_qq":
                ok = await self._dm_to_qq(text)
                return {
                    **out, "ok": ok, "spoken": True,
                    "fact": "去 QQ 说了" if ok else "QQ 没发出去",
                }

        except Exception as e:
            logger.exception(f"[feibijiubi] 动作 {act} 失败")
            return {**out, "fact": f"没干成（{type(e).__name__}）"}
        return out

    async def _act_line(self, umo: str, fact: str, text: str) -> str:
        """事儿干完了，让她用自己的嘴报一句——别用固定模板，那就又成人机了。"""
        provider = await self.context.get_using_provider_async(umo)
        if provider is None:
            return f"菲比啾比（{fact}）"
        system = self._load_persona() + f"""

【你刚把这件事干完了，现在要跟他说一句】
{fact}
他刚才说的是：{text[:80]}

要求：
- 一行，8~20 字，一个音 + 一个括号白话。
- 说的是结果和你的态度，不是复述他的话。
- 可以顺嘴损一句、可以抱怨一句、可以说得敷衍一点，别邀功。
- 不许写「已完成」「已为您」「搞定啦」这种客服腔。
- 括号里是白话，不是技术说明。"""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt="（说一句）", system_prompt=system, contexts=[]),
                timeout=15,
            )
            line = _enforce_format((resp.completion_text or "").strip())
            line = EMOJI_RE.sub("", line).strip()
            if line and len(line) <= 40:
                return line
        except Exception:
            logger.warning("[feibijiubi] 汇报句没编出来", exc_info=True)
        return f"菲比啾比（{fact}）"

    async def _act_bg(
        self, umo: str, act: str, why: str, where: str, text: str, bot=None
    ) -> None:
        """慢动作走后台：先垫一句，干完再报。不占会话锁，也不让他干等。"""
        try:
            pre = await self._adlib(umo, text, ACT_SCENE.get(act, "你要去干他说的这件事"))
            await self._say_at(where, umo, pre)
            r = await self._run_act(umo, act, why, where, text, bot)
            if r.get("spoken"):
                return
            if r.get("line"):
                await self._say_at(where, umo, r["line"])
                return
            line = await self._act_line(umo, r.get("fact") or "没成", text)
            await self._say_at(where, umo, line)
        except Exception:
            logger.exception(f"[feibijiubi] 后台动作 {act} 出错")

    # ---------- 作息：每天晚上自己去刷抖音 ----------
    def _load_routine(self) -> dict:
        try:
            return json.loads(ROUTINE_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_routine(self) -> None:
        try:
            ROUTINE_STATE.write_text(
                json.dumps(self._routine, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.exception("[feibijiubi] 写作息状态失败")

    def _ensure_routine_loop(self) -> None:
        if self._routine_task and not self._routine_task.done():
            return
        self._routine_task = asyncio.create_task(self._routine_loop())
        logger.info(f"[feibijiubi] 作息任务已启动（每天 {ROUTINE_HOUR} 点刷抖音）")

    async def _routine_loop(self) -> None:
        await asyncio.sleep(40)
        while True:
            try:
                await self._routine_tick()
            except Exception:
                logger.exception("[feibijiubi] 作息任务出错")
            await asyncio.sleep(ROUTINE_CHECK)

    async def _routine_tick(self) -> None:
        """19:00-20:00 之间补跑一次，当天跑过就不再跑。

        他说"每天晚上七点到八点自己去刷抖音"，光在聊天里答应没用，
        得真有个任务在跑——否则第二天问"你昨天刷了吗"她只能编。
        """
        now = datetime.now(TZ)
        if now.hour != ROUTINE_HOUR:
            return
        today = now.strftime("%Y-%m-%d")
        if self._routine.get("last_date") == today:
            return
        umos = [k for k in self._share_all.keys() if k]
        if not umos:
            logger.info("[feibijiubi] 到点了，但还不知道往哪个会话发，跳过")
            return

        self._routine["last_date"] = today
        self._routine["last_at"] = time.time()
        self._save_routine()
        logger.info(f"[feibijiubi] 到点自己刷抖音 {now:%H:%M}")
        await self._auto_watch(umos[0], intro="菲比啾比（到点了，我刷会儿抖音）")

    # ---------- 主动甩视频 ----------
    def _load_share_all(self) -> dict:
        try:
            return json.loads(SHARE_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_share_all(self) -> None:
        try:
            SHARE_STATE.write_text(
                json.dumps(self._share_all, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[feibijiubi] 写分享状态失败")

    async def _speak_video(self, umo: str, pick: dict) -> str:
        """就着一条视频的标题，说一句随手甩过去的话。"""
        provider = await self.context.get_using_provider_async(umo)
        title = (pick.get("title") or "").strip()
        author = pick.get("author") or ""
        if provider is None:
            return "这个你去看"
        system = self._load_persona() + f"""

【你刚刷到一条，想直接甩给他看】
标题：{title or '(没写)'}
作者：@{author or '?'}

要求：
- 一行，像随手发过去的，别像推荐语、别像安利。
- 可以损一句、可以挑标题里某个词开火、可以扯到你们之前聊过的事。
- 不许报点赞数、时长、话题数量；不许说"分享给你""推荐你看""这个视频"。
- 括号里是说人话的白话翻译，情绪上来了用菲八。"""
        try:
            resp = await provider.text_chat(
                prompt="（把这条甩给他）",
                system_prompt=system,
                contexts=[],
            )
            text = (resp.completion_text or "").strip()
        except Exception:
            logger.exception("[feibijiubi] 生成分享台词失败")
            text = ""
        text = re.sub(r"\n[ \t\u3000]*\n+", "\n", text).strip()
        text = EMOJI_RE.sub("", text).strip()  # 这条进抖音私信，表情标记发不出去
        return text or "这个你去看"

    async def _share_video(self, umo: str) -> dict:
        """刷一条 → 把视频卡片直接分享到他抖音私信。

        以前是「一句话 + 链接」，他收到的是个网址，还得自己点开跳浏览器。
        现在走视频页的分享面板「分享给朋友」，对方收到的是带封面的卡片，
        和真人手点分享一模一样；卡片发出去之后再补一句吐槽（不带链接）。
        链接只在卡片失败时兜底，别彻底哑火。
        """
        pick = await asyncio.to_thread(_call, "/pick_link", 220)
        if not pick.get("ok"):
            return {"ok": False, "error": pick.get("error") or "没刷到", "title": ""}

        line = await self._speak_video(umo, pick)
        aid = pick.get("aweme_id") or ""
        card = {}
        if aid:
            card = await asyncio.to_thread(_call, f"/share_card?aweme_id={aid}", 260)
            logger.info(
                f"[feibijiubi] 分享卡片 ok={card.get('ok')} peer={card.get('peer')} "
                f"stage={card.get('stage')} err={card.get('error') or '-'}"
            )
        if card.get("ok"):
            if line:
                await asyncio.to_thread(
                    _call, "/dm?text=" + urllib.parse.quote(line), 200
                )
            return {
                "ok": True,
                "mode": "card",
                "line": line,
                "peer": card.get("peer") or "",
                "url": pick.get("url"),
                "title": pick.get("title") or "",
            }

        body = f"{line} {pick.get('url')}".strip()
        res = await asyncio.to_thread(
            _call, "/dm?text=" + urllib.parse.quote(body), 220
        )
        return {
            "ok": bool(res.get("ok")),
            "mode": "link",
            "error": card.get("error") or res.get("error") or "",
            "line": line,
            "url": pick.get("url"),
            "title": pick.get("title") or "",
        }

    async def _auto_share(self, umo: str) -> None:
        st = self._share_all.setdefault(umo, {})
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if st.get("date") != today:
            st["date"] = today
            st["n"] = 0
        if now.hour < SHARE_FROM_HOUR or now.hour >= SHARE_TO_HOUR:
            return
        if int(st.get("n") or 0) >= SHARE_DAY_MAX:
            return
        if time.time() - float(st.get("last") or 0) < SHARE_GAP:
            return

        st["last"] = time.time()
        st["n"] = int(st.get("n") or 0) + 1
        self._save_share_all()

        r = await self._share_video(umo)
        logger.info(
            f"[feibijiubi] 主动甩视频 ok={r.get('ok')} "
            f"id={r.get('url')} err={r.get('error') or '-'}"
        )
        if r.get("ok"):
            st["last_line"] = r.get("line")
            st["last_url"] = r.get("url")
            st["last_title"] = r.get("title") or ""
            st["last_at"] = time.time()
            self._save_share_all()
            try:
                await self.context.send_message(
                    umo, MessageChain([Plain(f"菲比啾比（{r['line']}）")])
                )
            except Exception:
                logger.warning("[feibijiubi] QQ 这边没通知出去")

    def _ensure_share_loop(self) -> None:
        if self._share_task and not self._share_task.done():
            return
        self._share_task = asyncio.create_task(self._share_loop())

    async def _share_loop(self) -> None:
        while True:
            try:
                for umo in list(self._share_all.keys()):
                    await self._auto_share(umo)
            except Exception:
                logger.exception("[feibijiubi] 定时分享出错")
            await asyncio.sleep(SHARE_CHECK)

    async def initialize(self) -> None:
        self._ensure_share_loop()
        asyncio.create_task(self._brain_selfcheck())

    def _save_last(self, umo: str, payload: dict) -> None:
        try:
            data = {}
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            data[umo] = payload
            STATE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("[feibijiubi] 记状态失败")

    def _get_last(self, umo: str) -> dict | None:
        try:
            if not STATE_FILE.exists():
                return None
            return json.loads(STATE_FILE.read_text(encoding="utf-8")).get(umo)
        except Exception:
            return None

    # ---------- 生成 ----------
    async def _speak(self, umo: str, session: dict) -> tuple[str, str]:
        """把刷到的东西交给她说。返回 (文案, 封面url)。"""
        picked = session.get("picked") or []
        if not picked:
            return "", ""
        lines = "\n".join(_fmt_item(i, n + 1) for n, i in enumerate(picked))
        top = picked[0]

        provider = await self.context.get_using_provider_async(umo)
        if provider is None:
            return f"菲比啾比（刚刷到{top.get('title', '一个东西')[:18]}…）", top.get("cover", "")

        system = self._load_persona() + f"""

【你刚刷完抖音】
下面是屏幕上一张一张划过去的东西，是你自己看见的：

{lines}

挑一条你最想说的，用你的口气讲出来。要求：
- 说到具体的东西：作者干了啥、标题里哪个细节离谱，别抽象。
- 不许报点赞数、时长、话题数量，也不许说"我刷到一个视频"这种废话。
- 一行说完；括号里是说人话的白话翻译。情绪上来了用菲八。
- 不说教、不推荐他去看、不总结。"""

        try:
            resp = await provider.text_chat(
                prompt="（把刚刷到的说出来）",
                system_prompt=system,
                contexts=[],
            )
            text = (resp.completion_text or "").strip()
        except Exception:
            logger.exception("[feibijiubi] 生成失败")
            text = ""
        if not text:
            text = f"菲比啾比（刚刷到{top.get('title', '一个东西')[:18]}…）"
        text = re.sub(r"\n[ \t\u3000]*\n+", "\n", text).strip()
        return text, top.get("cover", "")

    async def _speak_share(self, umo: str, share: dict) -> str:
        """他分享过来的视频：有原话/有画面就照那个说，什么都没有才老实说只看到标题。"""
        tags = "、".join(share.get("tags") or [])
        tr = (share.get("transcript") or "").strip()
        vs = (share.get("visual") or "").strip()
        comments = share.get("comments") or []
        provider = await self.context.get_using_provider_async(umo)
        if provider is None:
            return f"菲比啾比（{share.get('text', '这个')[:16]}…）"

        if tr or vs:
            seen = f"""
标题：{share.get('text') or '(没写)'}
作者：@{share.get('author') or '?'}
时长：{share.get('duration') or '?'}"""
            if tr:
                seen += f"\n视频里说的话（语音转写，可能有错别字）：\n{tr[:1000]}"
            if vs:
                seen += f"\n你看到的画面（系统抽帧后描述的）：\n{vs[:1000]}"
            if comments:
                seen += "\n几条热评：" + " / ".join(c[:40] for c in comments[:4])
            body = f"""
【他真的分享给你的那条视频，你刚看完了】
{seen}

要求：
- 就着上面**具体出现的东西**说一句：有原话就点一句原话，没原话就挑画面里最好笑的一个细节。
- 说"我看完了"这类事实可以，因为确实看完了。
- 这一轮不许出现「看不了」「打不开」「没权限」「我又没有手机」，也不许说"你都问第几遍了"。
- 一行说完；括号里是说人话的白话翻译。情绪上来了用菲八。
- 不道谢、不总结、不说教、不推荐他去看。"""
        else:
            body = f"""
【他刚给你分享了一个抖音视频，但你只拿到标题，声音和画面都没拿到】
标题：{share.get('text') or '(没写)'}
话题：{tags or '无'}
作者：@{share.get('author') or '?'}

要求：
- 只就标题里那几个具体的词说一句，别编画面、别编剧情。
- 一行说完；括号里是说人话的白话翻译。
- 不道谢、不说"我看看"、不许诺去看。"""

        system = self._load_persona() + body

        try:
            resp = await provider.text_chat(
                prompt="（说他刚分享给你的那个视频）",
                system_prompt=system,
                contexts=[],
            )
            text = (resp.completion_text or "").strip()
        except Exception:
            logger.exception("[feibijiubi] 生成分享台词失败")
            text = ""
        text = re.sub(r"\n[ \t\u3000]*\n+", "\n", text).strip()
        return text or f"菲比啾比（{share.get('text', '')[:16]}…）"

    async def _bg_watch(self, umo: str) -> None:
        """后台把那条分享看完，再主动说一句。

        不放在 on_llm_request 里等，是因为它会占住会话锁：
        实测 132 秒那轮，他连发的三条全排在锁后面，一条都没回。
        """
        if umo in self._watching:
            return
        self._watching.add(umo)
        try:
            # 25 秒还没回来就补一句，别让他干等一分多钟以为死了
            task = asyncio.create_task(asyncio.to_thread(_call, "/share_full", 300))
            try:
                share = await asyncio.wait_for(asyncio.shield(task), timeout=25)
            except asyncio.TimeoutError:
                try:
                    await self.context.send_message(
                        umo,
                        MessageChain([
                            Plain(
                                await self._adlib(
                                    umo,
                                    "",
                                    "你正在看那条视频，还没看完，跟他说一声让他别干等",
                                )
                            )
                        ]),
                    )
                except Exception:
                    pass
                share = await task
            line = ""
            if share.get("ok"):
                line = await self._speak_share(umo, share)
            self._share_cache[umo] = (time.time(), share, line)
            logger.info(
                f"[feibijiubi] 后台看完 ok={share.get('ok')} "
                f"asr={len(share.get('transcript') or '')}字 "
                f"visual={len(share.get('visual') or '')}字 "
                f"line={len(line)}字 timing={share.get('timing')} "
                f"err={share.get('error') or share.get('vision_note') or '-'}"
            )
            if not line:
                line = "菲比（没看着，等下再说）"
            await self.context.send_message(umo, MessageChain([Plain(line)]))
            if share.get("ok"):
                self._guard[umo] = (time.time(), line)
        except Exception:
            logger.exception("[feibijiubi] 后台看视频失败")
            try:
                await self.context.send_message(
                    umo, MessageChain([Plain("菲比（卡住了，等下再说）")])
                )
            except Exception:
                pass
        finally:
            self._watching.discard(umo)

    async def _watch_and_talk(self, event: AstrMessageEvent, intro: str = ""):
        umo = event.unified_msg_origin
        if intro:
            yield event.plain_result(intro)

        # 开浏览器要十几秒，超过 2.5 秒就先说一句，不然像卡死了
        task = asyncio.create_task(asyncio.to_thread(_call, "/watch?scrolls=2&picks=2"))
        try:
            session = await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
        except asyncio.TimeoutError:
            yield event.plain_result("菲比啾比（等下，我刷会儿）")
            try:
                session = await task
            except Exception as e:
                logger.exception("[feibijiubi] 调服务失败")
                session = {"ok": False, "error": str(e)}

        if not session.get("ok"):
            logger.warning(f"[feibijiubi] 刷失败：{session.get('error')}")
            yield event.plain_result("菲比啾比（手机卡住了…等下再说）")
            return

        self._save_last(umo, {
            "at": session.get("at"),
            "picked": session.get("picked"),
            "scanned": session.get("scanned"),
        })

        text, cover = await self._speak(umo, session)
        chain = [Plain(text)]
        try:
            cover_path = await asyncio.to_thread(_fetch_cover, 0) if cover else ""
        except Exception:
            cover_path = ""
        if cover_path:
            try:
                chain.append(Image.fromFileSystem(cover_path))
            except Exception:
                logger.exception("[feibijiubi] 封面发送失败")
        yield event.chain_result(chain)

    async def _auto_watch(self, umo: str, intro: str = "") -> None:
        """自然语言喊她刷抖音时走这条路（命令那条是生成器，hook 里不能 yield）。

        「再看一次抖音」以前被当成读分享：缓存里还是上次那个视频，
        她能说的只剩「嗯」。现在明确分到推荐流这边。
        """

        async def say(line: str) -> None:
            try:
                await self.context.send_message(umo, MessageChain([Plain(line)]))
            except Exception:
                logger.warning("[feibijiubi] 消息没发出去")

        try:
            await say(intro or await self._adlib(umo, "", "你要去推荐流刷一会儿抖音"))
            session = await asyncio.to_thread(_call, "/watch?scrolls=3&picks=2", 240)
            if not session.get("ok"):
                logger.warning(f"[feibijiubi] 刷失败：{session.get('error')}")
                await say("菲比啾比（手机卡住了…等下再说）")
                return

            self._save_last(umo, {
                "at": session.get("at"),
                "picked": session.get("picked"),
                "scanned": session.get("scanned"),
            })
            text, cover = await self._speak(umo, session)
            chain = [Plain(text)]
            cover_path = ""
            if cover:
                try:
                    cover_path = await asyncio.to_thread(_fetch_cover, 0)
                except Exception:
                    cover_path = ""
            if cover_path:
                try:
                    chain.append(Image.fromFileSystem(cover_path))
                except Exception:
                    logger.exception("[feibijiubi] 封面发送失败")
            await self.context.send_message(umo, MessageChain(chain))
            logger.info(
                f"[feibijiubi] 自然语言刷抖音 picked={len(session.get('picked') or [])}"
            )
        except Exception:
            logger.exception("[feibijiubi] 自然语言刷抖音失败")

    # ---------- QQ 空间 ----------
    async def _post_qzone(self, umo: str, bot, said: str) -> None:
        """写一条说说，发到她的 QQ 空间。"""

        async def say(line: str) -> None:
            try:
                await self.context.send_message(umo, MessageChain([Plain(line)]))
            except Exception:
                logger.warning("[feibijiubi] 空间回执没发出去")

        try:
            content = ""
            provider = await self.context.get_using_provider_async(umo)
            if provider is not None:
                system = self._load_persona() + f"""

【他让你发一条 QQ 空间说说】
他原话：{said}

要求：
- 写 30~60 字，第一人称，像你真的随手发在空间里的。
- 不要话题标签、不要"今天也要加油"这种模板话。
- 不要说"已发布""马上发"这类，正文就是说说本身。"""
                try:
                    resp = await provider.text_chat(
                        prompt="（写这条说说）", system_prompt=system, contexts=[]
                    )
                    content = (resp.completion_text or "").strip()
                except Exception:
                    logger.exception("[feibijiubi] 生成说说失败")

            if not content:
                await say("菲比啾比（想不出来写啥…）")
                return

            try:
                res = await bot.call_action(
                    "send_qzone_msg",
                    content=content,
                    images=[],
                    ugc_right=1,
                    target_uins=[],
                )
            except Exception as e:
                logger.exception("[feibijiubi] 发说说失败")
                await say(f"菲比啾比（空间没发出去：{type(e).__name__}）")
                return

            tid = (res or {}).get("tid") or ""
            logger.info(f"[feibijiubi] 发说说 ok tid={tid} len={len(content)}")
            await say(f"菲比啾比（发了：「{content[:40]}」）")
        except Exception:
            logger.exception("[feibijiubi] 空间流程异常")

    @filter.on_llm_request()
    async def handle_qzone(self, event: AstrMessageEvent, req) -> None:
        """他说要发说说，就真去发；不然人设只会回一句「好呀」然后什么都没发生。"""
        try:
            text = (event.message_str or "").strip()
            if not text or len(text) > 60:
                return
            if not QZONE_HINT.search(text):
                return
            bot = getattr(event, "bot", None)
            if bot is None or not hasattr(bot, "call_action"):
                logger.warning("[feibijiubi] 当前平台没有 bot，发不了空间")
                return
            logger.info(f"[feibijiubi] 要发说说：{text[:40]}")
            event.stop_event()
            asyncio.create_task(self._post_qzone(event.unified_msg_origin, bot, text))
        except Exception:
            logger.exception("[feibijiubi] 空间触发失败")

    # ---------- 命令 ----------
    @filter.command("刷抖音")
    async def cmd_watch(self, event: AstrMessageEvent):
        """她会真的去刷两屏，然后把刷到的讲给你听。"""
        async for r in self._watch_and_talk(event):
            yield r

    @filter.command("刷会抖音")
    async def cmd_watch2(self, event: AstrMessageEvent):
        async for r in self._watch_and_talk(event):
            yield r

    @filter.command("看分享")
    async def cmd_share(self, event: AstrMessageEvent):
        """去抖音私信里翻他刚分享过来的视频，并真看内容。"""
        umo = event.unified_msg_origin
        task = asyncio.create_task(asyncio.to_thread(_call, "/share_full", 300))
        try:
            share = await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
        except asyncio.TimeoutError:
            yield event.plain_result("菲比啾比（等下，我看看…）")
            try:
                share = await task
            except Exception as e:
                logger.exception("[feibijiubi] 翻分享失败")
                share = {"ok": False, "error": str(e)}

        if not share.get("ok"):
            yield event.plain_result(f"菲比啾比（没翻到…{share.get('error', '')}）")
            return

        text = await self._speak_share(umo, share)
        chain = [Plain(text)]
        cover_path = ""
        if share.get("cover"):
            try:
                cover_path = await asyncio.to_thread(_fetch_cover, 0, "share.jpg")
            except Exception:
                cover_path = ""
        if cover_path:
            try:
                chain.append(Image.fromFileSystem(cover_path))
            except Exception:
                logger.exception("[feibijiubi] 分享封面发送失败")
        yield event.chain_result(chain)

        # 他要是想看"到底说了啥"，把转写原话也给一份，方便核对不是在编
        tr = (share.get("transcript") or "").strip()
        if tr:
            yield event.plain_result(f"（原话：{tr[:220]}）")
        elif share.get("asr_note"):
            yield event.plain_result(f"（没转出文字：{share.get('asr_note')[:80]}）")

    # ---------- 表情包 ----------
    @filter.command("表情包")
    async def cmd_sticker_stats(self, event: AstrMessageEvent):
        """看看她现在存了多少表情、都是些什么情绪。"""
        res = await asyncio.to_thread(_call, "/sticker_stats", 30)
        total = res.get("total") or 0
        if not total:
            yield event.plain_result("菲比（一张都没存，你先发我几张）")
            return
        by = res.get("by_mood") or {}
        top = "、".join(f"{k}{v}" for k, v in sorted(by.items(), key=lambda x: -x[1])[:7])
        yield event.plain_result(f"菲比啾比（存了 {total} 张：{top}）")

    @filter.command("捡图")
    async def cmd_grab_stickers(self, event: AstrMessageEvent):
        """从当前打开的那条视频的评论区里捡几张图。"""
        yield event.plain_result(random.choice([
            "菲比啾比（等下，我去评论区翻翻）",
            "菲比比（我看看有没有能捡的）",
            "菲比啾比……（翻一下）",
        ]))
        res = await asyncio.to_thread(
            _call, "/comment_stickers?limit=3&scrolls=2", 200
        )
        items = [it for it in (res.get("items") or []) if it.get("ok")]
        if not items:
            yield event.plain_result("菲比（没翻到能用的，评论区全是字）")
            return
        got = "；".join(
            f"{it.get('mood') or '没分类'}{('·' + it['desc']) if it.get('desc') else ''}"
            for it in items
        )
        yield event.plain_result(f"菲比啾比（捡了 {len(items)} 张：{got}）")

    @filter.command("抖音日报")
    async def cmd_report(self, event: AstrMessageEvent):
        """把最近的观看记录汇成一份她自己的小结。"""
        rep = await asyncio.to_thread(_call, "/report", 40)
        if not rep.get("ok"):
            yield event.plain_result("菲比啾比（记录翻不出来了…）")
            return
        tags = "、".join(t["tag"] for t in (rep.get("top_tags") or [])[:6]) or "什么都看"
        recent = rep.get("recent") or []
        head = f"最近一共看了 {rep.get('watch_total', 0)} 条，看得最多的是 {tags}。"
        if recent:
            last = recent[-1]
            head += f"\n最后一条是 @{last.get('author')} 的《{(last.get('title') or '')[:24]}》。"
        yield event.plain_result(f"菲比啾比（{head}）")

    # ---------- 工具（他自然语言提到分享时用） ----------
    @filter.llm_tool(name="read_douyin_share")
    async def read_douyin_share(self, event: AstrMessageEvent, reason: str = ""):
        """去自己的抖音私信里，把别人最近分享过来的那个视频看完。

        当他提到"我给你分享了个视频""发你了""你看这个"，或者你好奇他分享了什么时调用。
        会拿到视频里说的话（语音转写）和热评；拿不到语音时只剩标题。

        Args:
            reason(string): 他说的原话，或你想看的原因（随便填）
        """
        # 同一轮前面已经预取过就直接用，别再开一遍浏览器（实测白等 33 秒）
        umo = event.unified_msg_origin
        cached = self._share_cache.get(umo)
        if cached and time.time() - cached[0] < 600:
            share = cached[1]
        else:
            share = await asyncio.to_thread(_call, "/share_full", 300)
            self._share_cache[umo] = (time.time(), share, "")
        if not share.get("ok"):
            return f"没看成，真实原因：{share.get('error')}。别编别的原因。"
        tr = (share.get("transcript") or "").strip()
        comments = share.get("comments") or []
        out = (
            f"分享人：{share.get('from') or '他'}\n"
            f"标题：{share.get('text') or '(没写)'}\n"
            f"作者：@{share.get('author') or '?'}\n"
            f"时长：{share.get('duration') or '?'}\n"
        )
        if tr:
            out += f"视频里说的话（语音转写，可能有错别字）：{tr[:1200]}\n"
        else:
            out += f"（没听到语音内容：{share.get('asr_note') or '转写不可用'}，只能就标题说，别编）\n"
        if comments:
            out += "热评：" + " / ".join(c[:40] for c in comments[:4]) + "\n"
        return out

    @filter.command("分享视频给我")
    async def cmd_share_video(self, event: AstrMessageEvent):
        """她自己挑一条，发到你抖音私信里。"""
        umo = event.unified_msg_origin
        self._share_all.setdefault(umo, {})
        self._ensure_share_loop()
        yield event.plain_result("菲比啾比（等下，我挑一条）")
        r = await self._share_video(umo)
        if not r.get("ok"):
            yield event.plain_result(f"菲比啾比（没发出去…{r.get('error') or '不知道为啥'}）")
            return
        st = self._share_all.setdefault(umo, {})
        st["last"] = time.time()
        st["last_line"] = r.get("line")
        st["last_url"] = r.get("url")
        st["last_title"] = r.get("title") or ""
        st["last_at"] = time.time()
        self._save_share_all()
        yield event.plain_result(f"菲比啾比（发你抖音私信了：{r['line']}）")

    # ---------- 上下文 ----------
    @filter.on_llm_request()
    async def inject_last_watch(self, event: AstrMessageEvent, req) -> None:
        """把上次刷到的东西塞进上下文，免得他问「你刚看啥呢」她装傻。"""
        try:
            # 记下这个会话，定时分享时知道往哪发
            self._share_all.setdefault(event.unified_msg_origin, {})
            self._ensure_share_loop()
            self._ensure_dm_loop()  # __init__ 里没抢到事件循环时的兜底
            self._ensure_routine_loop()
            last = self._get_last(event.unified_msg_origin)
            if not last:
                return
            picked = last.get("picked") or []
            if not picked:
                return
            lines = "\n".join(_fmt_item(i, n + 1) for n, i in enumerate(picked))
            req.system_prompt = (req.system_prompt or "") + f"""

【你刚才自己刷抖音刷到的（真的看过，可以拿出来说）】
{lines}
如果他说到抖音、视频、刷手机，或者问你在干嘛，可以自然提起其中一条；
但别复述数据，也别每句话都往这上面扯。"""
        except Exception:
            logger.exception("[feibijiubi] 注入失败")

    @filter.on_llm_request()
    async def remember_qq_turn(self, event: AstrMessageEvent, req) -> None:
        """把 QQ 这轮他说的话记下来，等会儿私信里要用。

        私信回复走的是独立 prompt，看不见这个会话。不记这一份，
        她到了抖音就跟失忆一样，同一件事要再说一遍。
        """
        try:
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return
            self._qq_recent.append({"who": "他", "text": text[:120]})
            self._qq_recent = self._qq_recent[-8:]
        except Exception:
            logger.warning("[feibijiubi] 记 QQ 对话失败", exc_info=True)

    @filter.on_llm_request()
    async def inject_dm_recent(self, event: AstrMessageEvent, req) -> None:
        """他提到抖音/那边时，把私信里刚说过的几句摆给她看。

        QQ 会话和私信是两套上下文：私信每轮都是新 prompt，QQ 这边也看不到
        私信。所以她被问「那边回我」时手上没有任何事实，只能顺着印象编——
        「我回不了，那边我发不出去」就是这么来的，而且她越编越自信。
        """
        try:
            text = (event.message_str or "").strip()
            if not re.search(r"抖音|私信|那边", text):
                return
            rec = (self._dm_state.get("recent") or [])[-6:]
            if not rec:
                return
            lines = "\n".join(
                f"{r.get('who') or '你'}：{(r.get('text') or '')[:60]}" for r in rec
            )
            req.system_prompt = (req.system_prompt or "") + f"""

【你刚在抖音私信里跟他说的（真说过）】
{lines}
那边你能打字回他，也能发视频。别再说「我回不了」「那边发不出去」。"""
        except Exception:
            logger.warning("[feibijiubi] 注入私信记录失败", exc_info=True)

    # ---------- 历史消毒：别让它学自己写坏的样子 ----------
    @filter.on_llm_request()
    async def clean_history(self, event: AstrMessageEvent, req) -> None:
        """上几轮她写「两个括号 + 一句旁白」，历史里原样留着，她就当范文抄。

        手机上看是压过的（polish_reply 改了要发出去的那份），但存进会话的是
        模型原文，下一轮又喂回去。所以每轮请求前把历史里的助手消息也压一遍。
        """
        try:
            n = 0
            for msg in getattr(req, "contexts", None) or []:
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                fixed = _enforce_format(content)
                if fixed != content:
                    msg["content"] = fixed
                    n += 1
                elif ABILITY_DENY.search(fixed):
                    # 她自己写过的「我回不了」留在历史里就是范文，下一轮照着抄。
                    # 事实是 /dm 能发，所以这句删掉，换成能发的口吻。
                    msg["content"] = "菲比啾比（能发，我这就去那边说）"
                    n += 1
            if n:
                logger.info(f"[feibijiubi] 历史消毒 {n} 条")
        except Exception:
            logger.exception("[feibijiubi] 历史消毒失败")

    # ---------- 使唤她：在 QQ 说一句，活干在抖音 ----------
    @filter.on_llm_request()
    async def dispatch_act(self, event: AstrMessageEvent, req) -> None:
        """他那句话要是个活儿，就先真干完，再让她开口。

        必须排在 prefetch_share 前面：「给我分享个视频」里有「视频」两个字，
        会先被读分享那条抓走——她会去翻他上次发的卡片，然后讲一个不相干的。
        所以这里先判，判中了就占住 umo，后面那条直接让路。
        """
        try:
            umo = event.unified_msg_origin
            if not umo.startswith("napcat:"):
                return
            text = (event.message_str or "").strip()
            if not text or len(text) > 60:
                return
            # 这几条老链路更细（要垫话术、要纠错重读），交给它们自己跑
            if (
                WATCH_HINT.search(text)
                or QQ_DM_SEND.search(text)
                or QZONE_HINT.search(text)
            ):
                return
            if not ACT_HINT.search(text):
                return

            d = await self._decide(umo, text, "QQ")
            act = d.get("act") or "none"
            if act == "none":
                return
            bot = getattr(event, "bot", None)

            # 不想干是允许的，理由得是她自己的。把「不想」交给她说，
            # 但要把「不许说不会」这条压在前面——嘴硬可以，装不会不行。
            if act == "refuse":
                req.system_prompt = (req.system_prompt or "") + (
                    f"\n\n【他刚让你干活，你不想干】\n"
                    f"你自己的理由：{d.get('why') or '懒得动'}\n"
                    "照你的脾气回他。可以说不去、可以让他等、可以谈条件；\n"
                    "但不许说「不会」「做不到」「没权限」——那些东西你手上都有。"
                )
                return

            if act in SLOW_ACTS:
                self._act_claim[umo] = time.time()
                event.stop_event()
                asyncio.create_task(
                    self._act_bg(umo, act, d.get("why") or "", "qq", text, bot)
                )
                return

            r = await self._run_act(
                umo, act, d.get("why") or "", "qq", text, bot
            )
            if r.get("spoken"):
                # 话已经说完了（发链接/发表情/去那边说），别让主模型再补一句
                self._act_claim[umo] = time.time()
                event.stop_event()
                return
            req.system_prompt = (req.system_prompt or "") + (
                f"\n\n【他刚让你干活，你已经干了】\n"
                f"结果：{r.get('fact')}\n"
                "用你的口气回一句，说结果和态度，别邀功、别说「已完成」。"
            )
        except Exception:
            logger.exception("[feibijiubi] 派活路由出错")

    # ---------- 强制预取：不靠模型自觉 ----------
    @filter.on_llm_request()
    async def prefetch_share(self, event: AstrMessageEvent, req) -> None:
        """他只要提到分享/能不能看，就先把视频真看一遍，再由她自己说。

        两个坑都踩过：
        1. 工具注册成功了，但模型看到人设里"打不开"的倾向，压根不调工具；
        2. 把事实拼进 system_prompt 也没用——她照样顺着前几轮"我看不了"往下编，
           一句"你都问第三遍了"就把真转写顶掉。

        现在是两段式：先回一句"点开看看"并 stop_event() 掐掉本轮，
        再把 share_full 丢到后台（`_bg_watch`）——它实测能跑到 75 秒，
        留在钩子里会占住会话锁，他连发的几句全排队。

        手上有缓存（10 分钟内看过）时还是同步注入事实，当轮就能答。
        """
        try:
            text = (event.message_str or "").strip()
            if not text or len(text) > 40:
                return

            # 派活路由已经接管这一轮：它去挑视频/刷流，别再抓去读旧卡片
            if time.time() - self._act_claim.get(event.unified_msg_origin, 0) < 120:
                logger.info("[feibijiubi] 本轮已派活，预取让路")
                return

            umo = event.unified_msg_origin

            # 「再看一次抖音」「刷会儿」= 去推荐流再刷一遍，不是重看那条旧分享。
            # 走这里就掐掉主模型，否则它会先说刷、再答一句不相干的。
            if WATCH_HINT.search(text):
                logger.info("[feibijiubi] 要再刷一次，走推荐流")
                event.stop_event()
                asyncio.create_task(self._auto_watch(umo))
                return

            # 「你在抖音回我」——他要的是那边真收到一条，不是这边解释。
            # 以前没这个分支，主模型只能凭印象编，编出来就是「我回不了」。
            if QQ_DM_SEND.search(text):
                logger.info(f"[feibijiubi] QQ 上让他去抖音回：{text[:30]}")
                event.stop_event()
                asyncio.create_task(self._go_dm(umo, text))
                return

            # 「抖音这次是你发的吗」——问的是归属，不是内容。
            # 给一条事实让她认，但绝不开浏览器、也不喊「我点开看看」。
            if OWNER_HINT.search(text):
                req.system_prompt = (req.system_prompt or "") + _owner_block(
                    self._share_all.get(umo)
                )
                if not SEE_HINT.search(text):
                    logger.info("[feibijiubi] 归属问句，不预取")
                    return

            if not SEE_HINT.search(text):
                return

            # 他自己看完了来汇报，不是叫她看
            if SELF_WATCHED.search(text) and not re.search(r"你没看|你看没看|能看不", text):
                logger.info("[feibijiubi] 他自己看过了，不预取")
                return
            now = time.time()
            cached = self._share_cache.get(umo)
            ttl = 600 if (cached and cached[1].get("ok")) else 60

            if umo in self._watching:
                # 已经有一条在后台看了。这轮只给状态，别再开第二个浏览器。
                req.system_prompt = (req.system_prompt or "") + """

【你正在看那条视频，还没看完】
只回一句你还在看，别报视频里的内容、别编画面、别让他重发。"""
                return

            if cached and now - cached[0] < ttl:
                share, line = cached[1], cached[2]
            else:
                # 长任务不能留在 on_llm_request 里：它会占住会话锁。
                # 实测 share_full 有一次跑了 132 秒（下载 43s + 转写看图 65s），
                # 这期间他连发三条全在排队，一条都没回。
                self._auto_at[umo] = now
                try:
                    line = await self._adlib(
                        umo, text, "你要点开他分享的那个视频，从头看一遍"
                    )
                    await event.send(MessageChain([Plain(line)]))
                except Exception:
                    logger.warning("[feibijiubi] 预告消息没发出去")
                event.stop_event()
                asyncio.create_task(self._bg_watch(umo))
                return

            # 事实交回给她自己说——人设里已写明三档，不再越权替她发言
            req.system_prompt = (req.system_prompt or "") + _share_block(share)
            if share.get("ok") and (share.get("transcript") or "").strip() and line:
                # 万一她还是嘴硬，on_llm_response 用这条台词兜底
                self._guard[umo] = (now, line)

            # 事实已经在手上了，就别让她再去看一遍：上一版她照样调工具，白等 33~60 秒
            try:
                tools = getattr(req, "func_tool", None)
                if tools and "read_douyin_share" in tools.names():
                    tools.remove_tool("read_douyin_share")
                    logger.info("[feibijiubi] 本轮已给事实，摘掉重复看视频的工具")
            except Exception:
                logger.warning("[feibijiubi] 摘工具失败", exc_info=True)
        except Exception:
            logger.exception("[feibijiubi] 预取分享失败")

    # ---------- 表情包 ----------
    async def _send_sticker(self, umo: str, mood: str) -> None:
        """按情绪从库里挑一张发出去。没图就什么都不做——文字里的标记已经剥掉了。"""
        try:
            it = await asyncio.to_thread(
                _call, "/sticker_pick?mood=" + urllib.parse.quote(mood), 30
            )
            if not it.get("ok"):
                return
            p = it.get("ctr_path") or ""
            if not p or not os.path.isfile(p):
                logger.warning(f"[feibijiubi] 表情文件容器里没有 {p}")
                return
            await asyncio.sleep(0.6)  # 让文字先落地，像「说完补个图」
            await self.context.send_message(umo, MessageChain([Image.fromFileSystem(p)]))
            logger.info(f"[feibijiubi] 发表情 mood={mood} file={os.path.basename(p)}")
        except Exception:
            logger.warning("[feibijiubi] 发表情失败", exc_info=True)

    async def _save_sticker(self, comp: Image) -> None:
        """收他发的图。能不能进库由宿主的 flash 判（截图/自拍/长图会被丢掉）。"""
        try:
            path = str(getattr(comp, "file", "") or getattr(comp, "path", "") or "")
            if path.startswith("file:///"):
                path = path[7:]  # 容器里是 file:///AstrBot/...，去掉协议才认
            url = str(getattr(comp, "url", "") or "")
            raw = b""
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    raw = f.read()
            if not raw and url:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
            if len(raw) < 600:
                return
            ext = os.path.splitext(path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".gif" if raw[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
            res = await asyncio.to_thread(_post_sticker, raw, ext, "qq")
            logger.info(
                f"[feibijiubi] 收表情 ok={res.get('ok')} mood={res.get('mood') or '-'} "
                f"desc={res.get('desc') or '-'} err={res.get('error') or '-'}"
            )
        except Exception:
            logger.warning("[feibijiubi] 收表情失败", exc_info=True)

    @filter.on_llm_request()
    async def capture_sticker(self, event: AstrMessageEvent, req=None) -> None:
        """他发图就收，同一条消息里最多取两张，别把相册刷进来。

        req 是 on_llm_request 固定会传的第二个参数。上一版签名只接了 event，
        AstrBot 每次请求都抛 TypeError——收图这条路一直是死的。
        """
        try:
            imgs = [c for c in event.message_obj.message if isinstance(c, Image)]
            if imgs:
                logger.info(f"[feibijiubi] 收到 {len(imgs)} 张图，试试能不能存进表情库")
            for c in imgs[:2]:
                asyncio.create_task(self._save_sticker(c))
        except Exception:
            logger.warning("[feibijiubi] 检查表情失败", exc_info=True)

    # ---------- 回复后处理：兜嘴硬 + 压格式 ----------
    @filter.on_llm_response()
    async def polish_reply(self, event: AstrMessageEvent, resp) -> None:
        """两件事：嘴硬换台词，格式压回一行。

        格式是可判定的（数左括号），所以放代码里执行。人设写「一条只许一个括号」，
        模型该写三行还是写三行——实测就是三行、两个括号、外加一句旁白。
        """
        try:
            text = getattr(resp, "completion_text", "") or ""
            umo = event.unified_msg_origin

            item = self._guard.get(umo)
            if item:
                ts, line = item
                if line and time.time() - ts <= 600 and DENY_HINT.search(text):
                    logger.warning(f"[feibijiubi] 兜底换台词，原回复={text[:80]}")
                    text = line
                self._guard.pop(umo, None)

            # 能力自贬：/dm 是通的，这句只可能是顺着前几轮编的，直接换掉。
            if ABILITY_DENY.search(text):
                raw = text
                logger.warning(f"[feibijiubi] 她否认能回私信，换掉：{raw[:60]}")
                text = "菲比啾比（能发，别听我瞎说）"
                # 只有他确实在说「去抖音」时才真发一条；说别的别乱开浏览器
                if re.search(r"抖音|那边|私信", raw + (event.message_str or "")):
                    asyncio.create_task(
                        self._go_dm(umo, event.message_str or "他叫你去抖音回")
                    )

            fixed = _enforce_format(text)
            if fixed != text:
                logger.info(f"[feibijiubi] 格式收紧 {len(text)}→{len(fixed)}字")

            # 表情标记换成真图：库里没有这张情绪就不发，但标记必须剥掉（不然显示成字面）
            mood = ""
            m = EMOJI_RE.search(fixed)
            if m:
                cand = m.group(0)[1:-1]
                if cand in STICKER_MOODS:
                    mood = cand
                fixed = EMOJI_RE.sub("", fixed).strip()

            resp.completion_text = fixed
            # 她自己这轮说了什么也要记，不然到了私信只记得他说的话
            try:
                said = EMOJI_RE.sub("", fixed).strip()
                if said:
                    self._qq_recent.append({"who": "你", "text": said[:120]})
                    self._qq_recent = self._qq_recent[-8:]
            except Exception:
                pass
            if mood:
                asyncio.create_task(self._send_sticker(umo, mood))
        except Exception:
            logger.exception("[feibijiubi] 回复后处理失败")
