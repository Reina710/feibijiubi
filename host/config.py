# -*- coding: utf-8 -*-
"""集中配置。

所有路径和口令都能用环境变量覆盖，默认值按「本项目放在 AstrBot 数据目录旁边」
这一种常见布局推导。换机器部署时只改环境变量，不用动代码。

    FEIBI_TOKEN       宿主服务和插件之间的口令（两边必须一致）
    FEIBI_PORT        宿主服务监听端口，默认 8791
    ASTRBOT_DATA      AstrBot 的 data 目录（宿主机路径）
    DEEPSEEK_API_KEY  画面理解用的 key；没有就从 AstrBot 配置里找
    FEIBI_VISION_MODEL 视觉模型名，默认 deepseek-flash
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_astrbot_data() -> str:
    """按 common 布局找 AstrBot/data：同级目录、上级目录、用户主目录。"""
    here = BASE_DIR
    cands = [
        os.path.join(os.path.dirname(here), "astrbot", "data"),
        os.path.join(os.path.dirname(os.path.dirname(here)), "astrbot", "data"),
        os.path.join(os.path.expanduser("~"), "astrbot", "data"),
        "/AstrBot/data",
    ]
    for c in cands:
        if os.path.isdir(c):
            return c.replace("\\", "/")
    return cands[0].replace("\\", "/")


ASTRBOT_DATA = (os.environ.get("ASTRBOT_DATA") or _find_astrbot_data()).replace("\\", "/")

TOKEN = os.environ.get("FEIBI_TOKEN", "feibi-dy-2026")
PORT = int(os.environ.get("FEIBI_PORT", "8791"))

LOG_PATH = os.path.join(BASE_DIR, "service.log")
CONFIG_JSON = os.path.join(ASTRBOT_DATA, "cmd_config.json")
STICKER_DIR = os.path.join(
    ASTRBOT_DATA, "plugin_data", "feibijiubi", "stickers"
)

# 插件在容器里看到的是这个挂载点，传给宿主服务时要换回宿主机路径
DATA_CTR = "/AstrBot/data"
