# -*- coding: utf-8 -*-
"""本地语音转写。faster-whisper，模型走 hf-mirror，首次会下载几百 MB。"""
from __future__ import annotations

import os
import threading

_MODEL = None
_LOCK = threading.Lock()
MODEL_SIZE = os.environ.get("FEIBI_WHISPER", "small")
_HERE = os.path.dirname(os.path.abspath(__file__))
# 优先用本地目录（由 dl_model.py 从 ModelScope 拉），拉不到才回落到 HF 名字
LOCAL_MODEL = os.path.join(_HERE, "models", MODEL_SIZE)


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        # hf-mirror 不支持 xet 的 CAS 协议，会 401；强制走普通 HTTP 下载
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
        from faster_whisper import WhisperModel

        if os.path.exists(os.path.join(LOCAL_MODEL, "model.bin")):
            _MODEL = WhisperModel(LOCAL_MODEL, device="cpu", compute_type="int8")
        else:
            _MODEL = WhisperModel(
                MODEL_SIZE,
                device="cpu",
                compute_type="int8",
                download_root=os.path.join(_HERE, "models"),
            )
        return _MODEL


def _read_wav(path: str):
    """读 16k 单声道 wav 成 float32 数组。

    不走 faster-whisper 自带的 decode_audio——它用 PyAV 的 metadata_errors 参数，
    PyAV 19 已经删了，会直接 TypeError。ffmpeg 已经把音频抽好了，读 PCM 就行。
    """
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("需要 16bit 单声道 wav")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(wav_path: str) -> dict:
    """转写 wav，返回 {text, segments, lang}。失败返回 {error}。"""
    if not os.path.exists(wav_path):
        return {"error": "没有音频文件"}
    try:
        model = _get_model()
        audio = _read_wav(wav_path)
        segments, info = model.transcribe(
            audio,
            language="zh",
            vad_filter=True,
            # beam_size=1 比 5 快一倍多；短视频转写只用来「知道他说了啥」，
            # 不值得为几个字的精度多等三十秒。
            beam_size=1,
            # 关掉跨段条件：长视频不会越转越慢，也能少掉「点赞订阅转发」这类幻觉。
            condition_on_previous_text=False,
            initial_prompt="以下是普通话口语视频的内容，可能带网络用语和动漫角色名。",
        )
        lines = []
        for s in segments:
            t = (s.text or "").strip()
            if t:
                lines.append(t)
        text = "".join(lines) if lines else ""
        return {"text": text, "lang": getattr(info, "language", "zh"), "segments": len(lines)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
