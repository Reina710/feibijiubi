# -*- coding: utf-8 -*-
"""从 ModelScope 拉 faster-whisper 模型（国内速度可用）。"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "models", "small")
REPO = "pengzhendong/faster-whisper-small"
FILES = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
    "preprocessor_config.json",
]


def dl(name: str) -> bool:
    url = f"https://www.modelscope.cn/models/{REPO}/resolve/master/{name}"
    dst = os.path.join(OUT, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        print(f"skip {name} ({os.path.getsize(dst)} bytes)")
        return True
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total and done % (1024 * 1024 * 8) < 262144:
                        print(f"  {name} {done/1e6:.0f}/{total/1e6:.0f} MB", flush=True)
            os.replace(tmp, dst)
        print(f"ok {name} {done/1e6:.1f}MB {time.time()-t0:.1f}s")
        return True
    except Exception as e:
        print(f"FAIL {name} {type(e).__name__}: {e}")
        return False


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in FILES:
        dl(f)
    print("--- dir ---")
    for f in sorted(os.listdir(OUT)):
        print(f, os.path.getsize(os.path.join(OUT, f)))


if __name__ == "__main__":
    main()
