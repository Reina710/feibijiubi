# -*- coding: utf-8 -*-
"""菲比刷抖音 —— 宿主侧浏览器引擎。

用本机 Edge（Playwright channel=msedge）+ 持久化用户目录打开抖音网页版，
像真人一样滚动、挑选、停留，把"看到了什么"结构化输出。

对外只暴露 session() / detail()，其余都是内部实现。
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from datetime import datetime
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "edge_profile")
MEMORY_PATH = os.path.join(BASE_DIR, "memory.json")
SHOT_DIR = os.path.join(BASE_DIR, "shots")
COVER_DIR = os.path.join(BASE_DIR, "covers")
HOME = "https://www.douyin.com/jingxuan"
# 登录后个性化推荐流；未登录时这页会拿不到卡片，自动退回 HOME（精选）
HOME_FEED = "https://www.douyin.com/?recommend=1"
# 抖音私信（网页版），别人分享过来的视频落在这里
CHAT_URL = "https://www.douyin.com/chat"

# 消息列表里找“分享[视频]”那一条，返回最小容器的点击坐标
JS_FIND_SHARE_CHAT = r"""
() => {
  const out = [];
  document.querySelectorAll('div, li').forEach(n => {
    const t = (n.innerText || '').trim();
    if (!t || t.length > 60) return;
    if (!/分享\[|分享了|分享视频/.test(t)) return;
    const r = n.getBoundingClientRect();
    if (r.width < 80 || r.height < 25 || r.top < 30) return;
    out.push({x: r.x + Math.min(60, r.width / 2), y: r.y + r.height / 2,
              area: r.width * r.height, name: (t.split('\n')[0] || '').trim()});
  });
  if (!out.length) return null;
  out.sort((a, b) => a.area - b.area);
  return out[0];
}
"""

# 分享卡片的数据不在 DOM 文本里，藏在 React fiber 的 memoizedProps.message 上。
# 顺着 __reactFiber$ 往上爬，找到带 parsedContent 的那层，itemId / 标题 / 作者 / 封面全有。
JS_SHARE_MSG = r"""
() => {
  const imgs = Array.from(document.querySelectorAll('img'))
    .filter(i => ((i.className || '').toString()).includes('ShareAweme'));
  if (!imgs.length) return null;
  // 私信列表是「新→旧」倒序：index 0 才是最新那条。
  // 之前取 length-1，等于永远拿最早的一张卡片，问她「看了没」也只能复述旧视频。
  const img = imgs[0];
  const key = Object.keys(img).find(k => k.startsWith('__reactFiber$'));
  if (!key) return null;
  let f = img[key];
  for (let i = 0; i < 25 && f; i++) {
    const p = f.memoizedProps;
    if (p && p.message && p.message.parsedContent) {
      const c = p.message.parsedContent;
      const cover = ((c.cover_url || {}).url_list || [])[0] || '';
      return {
        item_id: String(c.itemId || ''),
        title: c.content_title || '',
        author: c.content_name || '',
        author_sec_uid: c.secUID || '',
        cover: cover
      };
    }
    f = f.return;
  }
  return null;
}
"""

# 会话里最后一张分享卡片。class 名会被 CSS Modules 改写，所以三重兜底：
# MessageItemShareAweme 类名 -> 封面图 CDN 前缀 -> 视口内像卡片的尺寸
JS_LAST_CARD = r"""
() => {
  const by = (fn) => Array.from(document.querySelectorAll('img')).filter(fn);
  let imgs = by(i => ((i.className || '').toString()).includes('MessageItemShareAweme'));
  if (!imgs.length) {
    imgs = by(i => ((i.currentSrc || i.src || '')).includes('image-cut-tos-priv'));
  }
  if (!imgs.length) {
    imgs = by(i => {
      const r = i.getBoundingClientRect();
      return r.width > 80 && r.width < 220 && r.height > 100 && r.height < 320 && r.x > 260;
    });
  }
  if (!imgs.length) return null;
  const img = imgs[0];  // 同 JS_SHARE_MSG：0 才是最新那张卡片
  const r = img.getBoundingClientRect();
  if (r.width < 30 || r.height < 30) return null;
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""


DUR_RE = re.compile(r"^\d{1,2}:\d{2}$")
LIKE_RE = re.compile(r"^[\d\.]+\s*[万亿wW]?\+?$")


def _to_jpeg(raw: bytes, out_path: str, max_w: int = 720) -> bool:
    """抖音封面有时是 webp，QQ 那边认不稳，统一转成 jpg。"""
    try:
        import io

        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(raw))
        im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)))
        im.save(out_path, "JPEG", quality=88)
        return True
    except Exception:
        # 没有 Pillow 时退回原图（至少 webp 也能发）
        try:
            with open(out_path, "wb") as f:
                f.write(raw)
            return len(raw) > 1000
        except Exception:
            return False


def _load_memory() -> dict:
    if os.path.exists(MEMORY_PATH):
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": {}, "tags": {}, "history": [], "stats": {"sessions": 0, "watched": 0}}


def _save_memory(mem: dict) -> None:
    tmp = MEMORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_PATH)


# --------------------------------------------------------------------------
# 页面脚本
# --------------------------------------------------------------------------

JS_COUNT_DUR = r"""
() => {
  // 精选页的卡片是 div[data-aweme-id]，外面没有 a 标签，时长也不在独立节点里
  // （"04:49 19.6万 标题" 全挤在一行）。只数时长会永远是 0，_wait_feed 直接超时。
  const cards = document.querySelectorAll('[data-aweme-id]').length;
  if (cards > 0) return cards;
  const all = document.querySelectorAll('div,span');
  let n = 0;
  for (const e of all) {
    if (e.children.length === 0 && /^\d{1,2}:\d{2}$/.test((e.innerText||'').trim())) n++;
  }
  return n;
}
"""

JS_COLLECT = r"""
() => {
  const all = Array.from(document.querySelectorAll('div,span'));
  const durs = all.filter(e => /^\d{1,2}:\d{2}$/.test((e.innerText||'').trim()) && e.children.length === 0);
  const out = [];
  durs.forEach((d) => {
    let n = d, best = null;
    for (let k = 0; k < 6 && n; k++) {
      const t = (n.innerText || '').trim();
      if (t.includes('@') && t.length > 15) { best = n; break; }
      n = n.parentElement;
    }
    if (!best) return;
    const im = best.querySelector('img');
    const vid = best.querySelector('video');
    const rect = best.getBoundingClientRect();
    out.push({
      lines: (best.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean),
      img: im ? (im.currentSrc || im.src || '') : '',
      poster: vid ? (vid.poster || '') : '',
      w: Math.round(rect.width), h: Math.round(rect.height),
    });
  });
  return out;
}
"""

JS_CLOSE_LOGIN = r"""
() => {
  const dlg = document.querySelector('div[role="dialog"]');
  if (!dlg) return false;
  const nodes = Array.from(dlg.querySelectorAll('*'));
  for (const n of nodes) {
    const r = n.getBoundingClientRect();
    const isSmall = r.width > 0 && r.width < 60 && r.height > 0 && r.height < 60;
    const txt = (n.innerText || '').trim();
    if (isSmall && (txt === '×' || txt === '✕' || txt === 'x' || n.className.toString().includes('close'))) {
      n.click();
      return true;
    }
  }
  return false;
}
"""

# 私信输入框旁边有个 hidden 的上传框（accept 图片/视频），直接往里塞文件，
# 比点「+」再等系统文件框稳——Playwright 拦截 file chooser 在 Edge 上偶尔不触发。
JS_MARK_FILE_INPUT = r"""
() => {
  const els = Array.from(document.querySelectorAll('input[type=file]'));
  if (!els.length) return 0;
  els.forEach(e => e.removeAttribute('data-feibi-file'));
  const el = els.find(e => e.multiple) || els[0];
  el.setAttribute('data-feibi-file', '1');
  return els.length;
}
"""

JS_HAS_SEND_DIALOG = r"""
() => (document.body.innerText || '').includes('发送给')
"""

JS_DM_IMG_PREVIEW = r"""
() => {
  // 上传后编辑区/预览区会出现一张待发送的图
  const imgs = Array.from(document.querySelectorAll('img'))
    .map(n => {
      const r = n.getBoundingClientRect();
      return {src: (n.src || '').slice(0, 120), x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height)};
    })
    .filter(o => o.w > 60 && o.h > 60 && o.y > 500);
  return imgs.slice(-3);
}
"""

# 视频详情页的操作栏在视频下方，不是右侧竖排。
# class 是构建哈希（o2tLobnl 这种），每次发版都会变，只能按「方形图标 + 位置」找：
# 从左到右依次是 赞 / 评论 / 收藏 / 分享。
# 评论区的图：头像是 40x40，emoji 是 16x16，真正的配图都上百像素，用尺寸就能分开
JS_SCROLL_COMMENTS = r"""
() => {
  const el = document.querySelector('[data-e2e="comment-list"]');
  if (!el) return false;
  const before = el.scrollTop;
  el.scrollTop = before + 800;
  return el.scrollTop > before;
}
"""

JS_COMMENT_IMGS = r"""
() => {
  const out = [];
  document.querySelectorAll('[data-e2e="comment-item"] img').forEach(n => {
    const r = n.getBoundingClientRect();
    if (r.width < 60 || r.height < 60) return;
    const src = n.src || '';
    if (!src || src.startsWith('data:')) return;
    out.push({src: src, w: Math.round(r.width), h: Math.round(r.height)});
  });
  return out;
}
"""

JS_REACT_BTNS = r"""
() => {
  const vh = window.innerHeight;
  // 页面里常有两份（推荐流 + 详情页），只认视口内那一份
  const pick = (sel) => {
    const rs = Array.from(document.querySelectorAll(sel))
      .map(n => n.getBoundingClientRect())
      .filter(r => r.width > 10 && r.height > 10 && r.y > 0 && r.y < vh - 20);
    if (!rs.length) return null;
    const r = rs[0];
    return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height * 0.32)};
  };
  return {
    digg: pick('[data-e2e="video-player-digg"]'),
    collect: pick('[data-e2e="video-player-collect"]'),
  };
}
"""

JS_ACTION_BTNS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const sq = Array.from(document.querySelectorAll('div,span,button'))
    .map(n => ({r: n.getBoundingClientRect()}))
    .filter(o => {
      const w = Math.round(o.r.width), h = Math.round(o.r.height);
      return w >= 30 && w <= 60 && h >= 30 && h <= 60 && o.r.width > 0 && o.r.height > 0;
    })
    .map(o => ({
      x: Math.round(o.r.x + o.r.width / 2),
      y: Math.round(o.r.y + o.r.height / 2),
      l: Math.round(o.r.x), t: Math.round(o.r.y),
    }));
  const seen = new Set();
  const uniq = sq.filter(o => {
    const k = o.x + ',' + o.y;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  // 详情页：视频下方横排；推荐流：右侧竖排。坐标全按视口比例算，别写死像素。
  const row = uniq.filter(o => o.t > vh * 0.70 && o.t < vh * 0.84
                            && o.l > vw * 0.09 && o.l < vw * 0.45)
                  .sort((a, b) => a.x - b.x);
  const col = uniq.filter(o => o.l > vw * 0.78 && o.t > vh * 0.30 && o.t < vh * 0.80)
                  .sort((a, b) => a.y - b.y);
  // 同一个图标会命中父子多层（y 差一两像素），按左边缘聚一下
  const ax = new Map();
  for (const o of row) if (!ax.has(o.l)) ax.set(o.l, o);
  const cy = new Map();
  for (const o of col) if (!cy.has(o.t)) cy.set(o.t, o);
  return {url: location.href, vw, vh,
          row: [...ax.values()].sort((a, b) => a.l - b.l),
          col: [...cy.values()].sort((a, b) => a.t - b.t)};
}
"""

# 推荐流首次进入的新手蒙层，挡住操作栏
JS_DISMISS_GUIDE = r"""
() => {
  const hit = Array.from(document.querySelectorAll('div,button,span'))
    .filter(n => /^(我知道了|知道了|跳过)$/.test((n.innerText || '').trim()));
  if (!hit.length) return null;
  const r = hit[hit.length - 1].getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""

# 收藏会弹一个「收藏到」面板，面板里的确认按钮
JS_COLLECT_CONFIRM = r"""
() => {
  const hit = Array.from(document.querySelectorAll('button, [role="button"], div'))
    .filter(n => /^(确定|完成|收藏)$/.test((n.innerText || '').trim()))
    .map(n => {
      const r = n.getBoundingClientRect();
      return {x: r.x, y: r.y, w: r.width, h: r.height, t: (n.innerText || '').trim()};
    })
    .filter(o => o.w > 0 && o.h > 0);
  return hit.length ? hit[hit.length - 1] : null;
}
"""

# 视频详情页底部操作条上的「分享」图标。详情页有 data-e2e，推荐流没有，
# 所以下面还有一层按竖排顺序兜底（赞/评/藏/享 里第 4 个）。
JS_SHARE_BTN = r"""
() => {
  const vh = window.innerHeight, vw = window.innerWidth;
  const ok = (r) => r.width > 8 && r.height > 8 && r.y > 0 && r.y < vh - 10
                     && r.x > 0 && r.x < vw;
  const c = document.querySelector('[data-e2e="video-share-icon-container"]');
  if (c) {
    // 容器是「图标 + 数字」一横排（42x42 的 svg 在左，计数在右），
    // 点容器中心会落在两个东西中间的空隙上，实测点不出面板。
    const svg = c.querySelector('svg');
    const r0 = (svg || c).getBoundingClientRect();
    if (ok(r0)) return {x: Math.round(r0.x + r0.width / 2),
                        y: Math.round(r0.y + r0.height / 2),
                        src: svg ? 'detail-svg' : 'detail'};
  }
  // 推荐流：右侧竖排图标，从评论数那一栏往下数第 4 个就是分享
  const col = Array.from(document.querySelectorAll('div,span'))
    .map(n => n.getBoundingClientRect())
    .filter(r => r.width >= 30 && r.width <= 60 && r.height >= 30 && r.height <= 60
                 && r.x > vw * 0.78 && r.y > vh * 0.30 && r.y < vh * 0.85);
  const seen = new Map();
  for (const r of col) {
    const k = Math.round(r.x);
    if (!seen.has(k)) seen.set(k, r);
  }
  const list = [...seen.values()].sort((a, b) => a.y - b.y);
  if (list.length >= 4) {
    const r = list[list.length - 1];
    return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), src: 'feed'};
  }
  return null;
}
"""

# 分享面板：以「复制链接」定位面板容器（每个面板都有），再收它下面的短文本项。
# 不写死渠道名和坐标——抖音改版频率比我们改代码高。
JS_SHARE_PANEL = r"""
() => {
  const vis = (n) => {
    const r = n.getBoundingClientRect();
    const s = getComputedStyle(n);
    return r.width > 8 && r.height > 8 && s.visibility !== 'hidden'
           && s.display !== 'none' && r.y > -5 && r.y < window.innerHeight + 5;
  };
  let root = null;
  const all = Array.from(document.querySelectorAll('div,span,button,a,li'));
  for (const n of all) {
    if (!vis(n)) continue;
    if ((n.innerText || '').trim() !== '复制链接') continue;
    let p = n;
    for (let i = 0; i < 5 && p; i++) {
      const r = p.getBoundingClientRect();
      if (r.width > 170 && r.height > 60) { root = p; break; }
      p = p.parentElement;
    }
    if (root) break;
  }
  const scope = root || document.body;
  const items = [];
  const seen = new Set();
  scope.querySelectorAll('div,span,button,a,li').forEach(n => {
    if (!vis(n)) return;
    const t = (n.innerText || '').trim();
    if (!t || t.length > 8 || t.includes('\n')) return;
    const r = n.getBoundingClientRect();
    const x = Math.round(r.x + r.width / 2), y = Math.round(r.y + r.height / 2);
    const k = t + '@' + x + ',' + y;
    if (seen.has(k)) return;
    seen.add(k);
    items.push({t, x, y, w: Math.round(r.width), h: Math.round(r.height)});
  });
  return {
    has_panel: !!root,
    items: items.slice(0, 60),
    layers: Array.from(document.querySelectorAll('div')).filter(n => {
      const s = getComputedStyle(n);
      const r = n.getBoundingClientRect();
      return (s.position === 'fixed' || s.position === 'absolute')
             && r.width > 150 && r.height > 50 && r.width < window.innerWidth - 50
             && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
    }).slice(0, 8).map(n => {
      const r = n.getBoundingClientRect();
      return {cls: (n.className || '').toString().slice(0, 50),
              w: Math.round(r.width), h: Math.round(r.height),
              txt: (n.innerText || '').replace(/\n+/g, '|').slice(0, 90)};
    }),
    body: (document.body.innerText || '').slice(-600),
  };
}
"""

# 分享面板里的「分享给朋友」列表：每行一个好友 + 一个「分享」按钮。
# 给每个按钮打上 data-feibi-friend 序号，点的时候按序号找回来——
# 这样不用把坐标再传一遍，也能避开「鼠标移到别处就关面板」的问题。
JS_SHARE_FRIENDS = r"""
() => {
  const vis = (n) => {
    const r = n.getBoundingClientRect();
    const s = getComputedStyle(n);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  let root = null;
  for (const n of document.querySelectorAll('div')) {
    if (!vis(n)) continue;
    if (!((n.innerText || '').includes('分享给朋友'))) continue;
    const r = n.getBoundingClientRect();
    if (r.width > 220 && r.height > 90) { root = n; break; }
  }
  if (!root) return {ok: false, err: '没有「分享给朋友」面板'};

  const out = [];
  root.querySelectorAll('div,span,button').forEach(n => {
    if (!vis(n)) return;
    if ((n.innerText || '').trim() !== '分享') return;
    const r = n.getBoundingClientRect();
    if (r.width < 20 || r.width > 140 || r.height < 16 || r.height > 60) return;
    let p = n, row = '';
    for (let i = 0; i < 4 && p; i++) {
      const t = (p.innerText || '').trim();
      if (t && t !== '分享' && t.length < 60) { row = t.split('\n')[0]; break; }
      p = p.parentElement;
    }
    const x = Math.round(r.x + r.width / 2), y = Math.round(r.y + r.height / 2);
    const idx = out.length;
    n.setAttribute('data-feibi-friend', String(idx));
    out.push({i: idx, x, y, w: Math.round(r.width), h: Math.round(r.height), row});
  });
  return {
    ok: out.length > 0,
    n: out.length,
    btns: out,
    text: (root.innerText || '').replace(/\n+/g, '|').slice(0, 200),
  };
}
"""

# 按序号点「分享」按钮。用 dispatchEvent 而不是真实鼠标：
# 真实鼠标要先 move 过去，一离开分享图标 onMouseLeave 就把面板收了。
JS_CLICK_FRIEND = r"""
(idx) => {
  const el = document.querySelector('[data-feibi-friend="' + idx + '"]');
  if (!el) return {ok: false, err: '按钮不见了'};
  el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  return {ok: true};
}
"""

# 分享成功的两个标志（实测：面板不会关，而是原地变成这样）：
# 1) 面板中间冒出「分享成功」
# 2) 那一行的按钮从「分享」变成「捎句话」（就是分享完问你要不要说一句）
JS_SHARE_STATE = r"""
(idx) => {
  const el = document.querySelector('[data-feibi-friend="' + idx + '"]');
  const label = el ? (el.innerText || '').trim() : '';
  const body = (document.body.innerText || '');
  return {label, sent_toast: body.includes('分享成功')};
}
"""

JS_DM_PROBE = r"""
() => {
  const pick = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      ce: el.getAttribute('contenteditable'),
      ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
      cls: (el.className || '').toString().slice(0, 90),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      visible: r.width > 0 && r.height > 0,
    };
  };
  const boxes = Array.from(document.querySelectorAll(
    '[contenteditable="true"], textarea, input[type=text]'
  )).map(pick);
  const btns = Array.from(document.querySelectorAll('button, [role="button"], div'))
    .filter(n => /发送|Send/.test((n.innerText || '').trim()) && (n.innerText || '').trim().length <= 6)
    .slice(0, 6)
    .map(pick);
  return {
    url: location.href,
    boxes: boxes,
    btns: btns,
    body: (document.body.innerText || '').slice(0, 500),
  };
}
"""

JS_DM_INPUT = r"""
() => {
  const els = Array.from(document.querySelectorAll(
    '[contenteditable="true"], textarea'
  ));
  const cands = els.map(el => {
    const r = el.getBoundingClientRect();
    return {el, x: r.x, y: r.y, w: r.width, h: r.height};
  }).filter(c => c.w > 150 && c.h > 12);
  if (!cands.length) return null;
  // 私信输入框在最下面
  cands.sort((a, b) => b.y - a.y);
  const c = cands[0];
  return {x: Math.round(c.x), y: Math.round(c.y), w: Math.round(c.w), h: Math.round(c.h)};
}
"""

JS_DM_LEFT = r"""
() => {
  const els = Array.from(document.querySelectorAll('[contenteditable="true"], textarea'));
  const cands = els.map(el => {
    const r = el.getBoundingClientRect();
    return {el, y: r.y, w: r.width, h: r.height};
  }).filter(c => c.w > 150 && c.h > 12);
  if (!cands.length) return '';
  cands.sort((a, b) => b.y - a.y);
  const el = cands[0].el;
  const raw = (el.value !== undefined ? el.value : el.innerText) || '';
  // 清空后 contenteditable 里还留个零宽空格，不当成"没发出去"
  return raw.replace(/[\u200b\u200c\u200d\ufeff]/g, '').trim();
}
"""

JS_DM_SEND_BTN = r"""
() => {
  // 发图会弹「发送给 XXX」的确认框，按钮文案是「发送 (1)」，只认单个「发送」会漏
  const hit = Array.from(document.querySelectorAll('button, [role="button"], div'))
    .filter(n => {
      const t = (n.innerText || '').trim();
      return /^(发送|Send)(\s*[（(]\s*\d+\s*[)）])?$/i.test(t);
    })
    .map(n => {
      const r = n.getBoundingClientRect();
      return {x: r.x, y: r.y, w: r.width, h: r.height};
    })
    .filter(c => c.w > 0 && c.h > 0);
  return hit.length ? hit[hit.length - 1] : null;
}
"""

# 私信会话里的消息气泡。抖音给每条消息的 class 都带 MessageItem 前缀
# （分享卡片是 MessageItemShareAweme），所以按这个前缀收最稳。
# 父子节点会同时命中，按「同文本取面积最小」的去重，留下真正装内容的那一层。
JS_DM_LIST = r"""
() => {
  const vw = window.innerWidth;
  const nodes = Array.from(document.querySelectorAll(
    '[class*="MessageItem"], [class*="messageItem"]'
  ));
  const seen = new Map();
  for (const n of nodes) {
    const cls = (n.className || '').toString();
    const r = n.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.left < 240) continue;      // 左边那一栏是会话列表
    if (r.top < 40) continue;        // 顶部是对方昵称
    const t = (n.innerText || '').trim();
    if (!t) continue;
    // 分享卡片（他甩过来的视频）里只有作者名，看起来像一句废话。
    // 标出来，插件才知道该点开看，而不是就着这个名字瞎聊。
    const isShare = /MessageItemShareAweme/.test(cls);
    const cardEl = n.closest('[class*="MessageItemShareAwemecontainer"]') || n;
    const imgEl = cardEl.querySelector(
      '[class*="MessageItemShareAwemeawemeContainer"] img'
    ) || cardEl.querySelector('img');
    const card = imgEl ? (imgEl.src || '').slice(-80) : '';
    const area = r.width * r.height;
    const prev = seen.get(t);
    if (!prev || area < prev.area) {
      seen.set(t, {
        text: t.slice(0, 400),
        cls: cls.slice(0, 90),
        x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height),
        area: area,
        share: isShare,
        card: card,
      });
    }
  }
  const items = Array.from(seen.values()).sort((a, b) => a.y - b.y);
  for (const it of items) {
    // 靠右的气泡是自己发的
    it.mine = (it.x + it.w / 2) > vw * 0.62;
    delete it.area;
  }
  return items;
}
"""

# 会话列表最上面那一项（最近聊天）。x 很靠左、单行昵称。
JS_CHAT_ITEM = r"""
() => {
  const out = [];
  document.querySelectorAll('div, li').forEach(n => {
    const r = n.getBoundingClientRect();
    if (r.left > 240 || r.width < 100 || r.width > 300) return;
    if (r.top < 60 || r.top > window.innerHeight - 80) return;
    if (r.height < 36 || r.height > 96) return;
    const t = (n.innerText || '').trim();
    if (!t || t.length > 40) return;
    const line = t.split('\n')[0].trim();
    // 会话列表里也挂着数字 ID 之类的节点，昵称不会是纯长数字
    if (!line || /^\d{8,}$/.test(line)) return;
    out.push({x: r.x + Math.min(60, r.width / 2), y: r.y + r.height / 2,
              top: r.top, name: line});
  });
  out.sort((a, b) => a.top - b.top);
  return out[0] || null;
}
"""

JS_PICK_LINK = r"""
() => {
  const clean = (s) => {
    let t = (s || '').trim().replace(/\s+/g, ' ');
    t = t.replace(/^\d{1,2}:\d{2}\s*/, '');          // 时长
    t = t.replace(/^\d+(\.\d+)?\s*万?\s*/, '');       // 点赞数
    return t.slice(0, 120);
  };
  const vw = window.innerWidth, vh = window.innerHeight;

  // 精选页/瀑布流：卡片就是 div[data-aweme-id]，没有 a 标签可抓。
  // 之前只找 a[href*="/video/"]，在这个页面上一个都找不到，pick_link 永远失败。
  const cards = [];
  for (const n of document.querySelectorAll('[data-aweme-id]')) {
    const r = n.getBoundingClientRect();
    if (r.width < 120 || r.height < 100) continue;
    if (r.bottom < 0 || r.top > vh) continue;
    const id = (n.getAttribute('data-aweme-id') || '').trim();
    if (!/^\d{15,}$/.test(id)) continue;
    cards.push({
      id,
      href: 'https://www.douyin.com/video/' + id,
      text: clean(n.innerText),
      author: (n.querySelector('[class*="author"],[class*="Author"]') || {}).innerText || '',
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
      area: Math.round(r.width * r.height),
    });
  }
  if (cards.length) {
    // 取屏幕中间偏上的那块：正在播的通常在最上面
    cards.sort((a, b) => (a.y - b.y) || (b.area - a.area));
    return cards[0];
  }

  // 推荐流（单列播放器）：卡片外面通常包着 a[href*="/video/"]
  const as = Array.from(document.querySelectorAll('a[href*="/video/"]'));
  const cands = [];
  for (const a of as) {
    const r = a.getBoundingClientRect();
    if (r.width < 120 || r.height < 120) continue;
    if (r.bottom < 0 || r.top > vh) continue;
    const m = (a.href || '').match(/\/video\/(\d+)/);
    if (!m) continue;
    cands.push({
      id: m[1],
      href: a.href.split('?')[0],
      text: clean(a.innerText),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
      area: Math.round(r.width * r.height),
    });
  }
  if (!cands.length) return {};
  cands.sort((a, b) => b.area - a.area);
  return cands[0];
}
"""

JS_DETAIL = r"""
() => {
  const txt = document.body.innerText || '';
  const items = Array.from(document.querySelectorAll('[data-e2e="comment-item"]')).map(e => (e.innerText||'').trim());
  return {
    text: txt.slice(0, 6000),
    comments: items,
    url: location.href,
    has_video: !!document.querySelector('video'),
    duration: (document.querySelector('video')||{}).duration || 0,
  };
}
"""


class DouyinEngine:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        # 推荐流接口里捞出来的 aweme（id + 文案），用来挑一条甩给他
        self._spy: list[dict] = []
        self._spy_on = False
        # 网络流水（排查接口用，只留最近 200 条）
        self._net: list[str] = []
        os.makedirs(PROFILE_DIR, exist_ok=True)
        os.makedirs(SHOT_DIR, exist_ok=True)
        os.makedirs(COVER_DIR, exist_ok=True)

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        if self._ctx:
            return
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="msedge",
            headless=self.headless,
            viewport={"width": 1280, "height": 860},
            args=[
                "--no-proxy-server",
                "--disable-blink-features=AutomationControlled",
                "--mute-audio" if self.headless else "--autoplay-policy=user-gesture-required",
            ],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._ctx = None
            self._page = None
            self._pw = None

    @property
    def page(self) -> Page:
        if not self._page:
            self.start()
        assert self._page is not None
        return self._page

    # ---------------- 内部动作 ----------------
    def _durs(self) -> int:
        try:
            return int(self.page.evaluate(JS_COUNT_DUR))
        except Exception:
            return 0

    def _wait_feed(self, timeout: float = 20.0) -> bool:
        """等首屏卡片出现；期间登录弹窗会几次冒出来，一直按掉。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.page.query_selector('div[role="dialog"]'):
                self._dismiss_login()
            if self._durs() > 0:
                return True
            self.page.wait_for_timeout(1500)
        return False

    def _goto(self, url: str, tries: int = 2) -> bool:
        """goto 会被页面自身的跳转打断（扫码后尤其常见），失败就等等再来一次。"""
        for i in range(tries):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                return True
            except Exception:
                if i == tries - 1:
                    return False
                self.page.wait_for_timeout(2000)
        return False

    def _goto_home(self) -> None:
        # 优先个性化推荐流（要登录态）；拿不到卡片再退回精选页
        for url in (HOME_FEED, HOME):
            if not self._goto(url):
                continue
            self.page.wait_for_timeout(2600)
            self._dismiss_login()
            if self._wait_feed(20):
                return
        # 兜底：刷新一次。绝不 clear_cookies——登录态是用户扫码换来的
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(3000)
            self._dismiss_login()
            self._wait_feed(18)
        except Exception:
            pass

    def _dismiss_login(self) -> None:
        """登录弹窗的关闭按钮是图标，只能按坐标点。视口固定 1280x860，先试实测坐标，再退回相对位置。"""
        page = self.page
        for _ in range(3):
            dlg = page.query_selector('div[role="dialog"]')
            if not dlg:
                return
            for xy in [(953, 237), (952, 236), (954, 238)]:
                try:
                    page.mouse.click(*xy)
                except Exception:
                    pass
                page.wait_for_timeout(600)
                if not page.query_selector('div[role="dialog"]'):
                    return
            try:
                box = dlg.bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] - 30, box["y"] + 30)
                    page.wait_for_timeout(600)
            except Exception:
                return

    def _scroll(self, times: int = 3, pause: float = 1.6) -> None:
        page = self.page
        page.mouse.move(640, 500)
        for _ in range(times):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(int(pause * 1000))

    # ---------------- 解析 ----------------
    @staticmethod
    def _parse_card(card: dict) -> dict | None:
        lines = card.get("lines") or []
        if len(lines) < 3:
            return None
        if not DUR_RE.match(lines[0]):
            return None
        duration = lines[0]
        likes = lines[1] if LIKE_RE.match(lines[1]) else ""
        rest = lines[2:]
        author = ""
        date = ""
        title_parts = []
        for ln in rest:
            if ln.startswith("@"):
                author = ln[1:].strip()
            elif ln.startswith("·") or ln.startswith("‧") or re.match(r"^[·•]\s*\S+前$", ln) or re.match(r"^[·•]\s*\d+月\d+日$", ln):
                date = ln.lstrip("·• ").strip()
            else:
                title_parts.append(ln)
        title = " ".join(title_parts).strip()
        if not title or not author:
            return None
        tags = re.findall(r"#([^\s#@]+)", title)
        clean = re.sub(r"#[^\s#@]+", "", title).strip()
        return {
            "duration": duration,
            "likes": likes,
            "title": clean,          # 去掉话题词的正文
            "raw_title": title,
            "author": author,
            "date": date,
            "tags": tags[:6],
            "cover": card.get("img") or card.get("poster") or "",
            "is_ad": ("广告" in title) or ("广告" in author),
            "size": [card.get("w", 0), card.get("h", 0)],
        }

    # ---------------- 对外 ----------------
    def collect(self, scrolls: int = 3) -> list[dict]:
        """滚几屏，边滚边收，返回去重后的卡片列表。"""
        self._goto_home()
        page = self.page
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def harvest() -> int:
            added = 0
            try:
                raw = page.evaluate(JS_COLLECT)
            except Exception:
                return 0
            for c in raw:
                it = self._parse_card(c)
                if not it or it["is_ad"]:
                    continue
                key = (it["author"], it["title"][:24])
                if key in seen:
                    continue
                seen.add(key)
                items.append(it)
                added += 1
            return added

        page.mouse.move(640, 500)
        harvest()
        for _ in range(max(0, scrolls)):
            page.mouse.wheel(0, 820)
            page.wait_for_timeout(1900)
            if page.query_selector('div[role="dialog"]'):
                self._dismiss_login()
            harvest()
        if page.query_selector('div[role="dialog"]'):
            self._dismiss_login()
        return items

    def _score(self, item: dict, mem: dict) -> float:
        tags = mem.get("tags", {})
        s = 0.0
        for t in item["tags"]:
            s += float(tags.get(t, 0)) * 1.0
        if item["author"] in mem.get("authors", {}):
            s += 1.5
        return s

    def session(self, scrolls: int = 3, picks: int = 2, explore: float = 0.35) -> dict:
        """一次"刷抖音"：滚动 -> 收集 -> 按兴趣挑几条 -> 落记忆。"""
        mem = _load_memory()
        items = self.collect(scrolls=scrolls)
        if not items:
            return {"ok": False, "error": "没抓到视频，可能页面没加载出来", "items": []}

        scored = []
        for it in items:
            if it["raw_title"] in mem.get("seen", {}):
                continue
            scored.append((self._score(it, mem), it))
        pool = scored or [(0.0, it) for it in items]

        pool.sort(key=lambda x: -x[0])
        chosen = []
        if random.random() < explore:
            # 探索：随机抽，模拟"手滑点进一个不相关的"
            chosen = [random.choice(items)]
            for _, it in pool:
                if it is not chosen[0] and len(chosen) < picks:
                    chosen.append(it)
        else:
            chosen = [it for _, it in pool[:picks]]
        chosen = chosen[:picks]

        now = datetime.now().isoformat(timespec="seconds")
        for it in chosen:
            it["score"] = round(self._score(it, mem), 2)
            mem["seen"][it["raw_title"]] = now
            mem["history"].append({
                "at": now,
                "author": it["author"],
                "title": it["title"],
                "tags": it["tags"],
                "likes": it["likes"],
            })
            for t in it["tags"]:
                mem["tags"][t] = min(float(mem["tags"].get(t, 0)) + 1, 30.0)
            mem.setdefault("authors", {})
            mem["authors"][it["author"]] = min(float(mem["authors"].get(it["author"], 0)) + 1, 20.0)

        mem["history"] = mem["history"][-200:]
        mem["stats"]["sessions"] = int(mem["stats"].get("sessions", 0)) + 1
        mem["stats"]["watched"] = int(mem["stats"].get("watched", 0)) + len(chosen)
        _save_memory(mem)

        shot = os.path.join(SHOT_DIR, "last.png")
        try:
            self.page.screenshot(path=shot)
        except Exception:
            shot = ""

        # 封面就地存一份：抖音 CDN 带签名 + 校验 referer，容器里直接下会 403
        covers = []
        for n, it in enumerate(chosen):
            path = os.path.join(COVER_DIR, f"{n}.jpg")
            try:
                r = self._ctx.request.get(
                    it["cover"],
                    headers={"Referer": "https://www.douyin.com/"},
                    timeout=15000,
                )
                if not r.ok:
                    covers.append("")
                    continue
                raw = r.body()
                if _to_jpeg(raw, path):
                    covers.append(path)
                else:
                    covers.append("")
            except Exception:
                covers.append("")

        top_tags = sorted(mem["tags"].items(), key=lambda kv: -float(kv[1]))[:5]
        return {
            "ok": True,
            "at": now,
            "scanned": len(items),
            "picked": chosen,
            "covers": covers,
            "top_tags": [{"tag": k, "n": int(float(v))} for k, v in top_tags],
            "stats": mem["stats"],
            "shot": shot,
        }

    # ---------------- 别人分享过来的视频 ----------------
    def _open_share_chat(self) -> str:
        """进消息页，点开最近那个有分享卡片的会话。返回会话名。"""
        page = self.page
        self._goto(CHAT_URL)

        # 原来这里靠 `"分享[" in body.inner_text()` 判断列表好了没有：文案其实
        # 不一定带方括号，条件几乎从不成立，于是每次都白等满 22 秒。
        # 改成直接轮询"分享卡片在不在视野里"，拿到坐标就点，拿不到才等到超时。
        pos = None
        end = time.time() + 12
        while time.time() < end:
            pos = page.evaluate(JS_FIND_SHARE_CHAT)
            if pos:
                break
            page.wait_for_timeout(800)

        name = (pos or {}).get("name") or ""
        if pos:
            page.mouse.click(pos["x"], pos["y"])
        else:
            page.mouse.click(100, 80)  # 兜底：列表第一项

        # 等卡片出现（图片是懒加载的）
        end = time.time() + 8
        while time.time() < end:
            try:
                if page.locator('img[class*="ShareAweme"]').count() or page.locator(
                    'img[src*="image-cut-tos-priv"]'
                ).count():
                    break
            except Exception:
                pass
            page.wait_for_timeout(600)

        # 曾经想在这儿滚到会话底部再取卡片，实测设置 scrollTop 无效（React 接管），
        # 而且这个列表本身就是倒序渲染的——最新的卡片 index 0，一开始就在 DOM 里。
        return name

    def _click_share_card(self) -> bool:
        """点最新一张分享卡片；点开是页内弹窗，不是跳页。

        用 CSS 属性包含选择器而不是 evaluate：`img[class*="ShareAweme"]` 不依赖
        CSS Modules 的具体后缀，也不用算坐标。列表是倒序的，第一张才是最新。
        """
        page = self.page
        for sel in ('img[class*="ShareAweme"]', 'img[src*="image-cut-tos-priv"]'):
            try:
                loc = page.locator(sel)
                n = loc.count()
            except Exception:
                n = 0
            if not n:
                continue
            try:
                box = loc.nth(0).bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(4500)
            return True
        return False

    def read_share(self) -> dict:
        """读抖音私信里别人分享过来的最新一条视频。

        数据不在 DOM 文本里，也不靠点开卡片：找到卡片 img，顺着 __reactFiber$
        往上爬到 message.parsedContent，itemId / 标题 / 作者 / 封面全在里面。
        """
        who = self._open_share_chat()
        meta = self.page.evaluate(JS_SHARE_MSG)
        if not meta:
            return {"ok": False, "error": "没找到分享卡片", "from": who}

        desc = (meta.get("title") or "").strip()
        cover_url = meta.get("cover") or ""
        cover_path = ""
        if cover_url:
            try:
                r = self._ctx.request.get(
                    cover_url, headers={"Referer": "https://www.douyin.com/"}, timeout=15000
                )
                if r.ok:
                    p = os.path.join(COVER_DIR, "share.jpg")
                    if _to_jpeg(r.body(), p):
                        cover_path = p
            except Exception:
                pass

        item_id = str(meta.get("item_id") or "")
        return {
            "ok": True,
            "from": who,
            "aweme_id": item_id,
            "desc": desc,
            "text": re.sub(r"#[^\s#@]+", "", desc).strip(),
            "tags": re.findall(r"#([^\s#@]+)", desc)[:6],
            "author": meta.get("author") or "",
            "url": f"https://www.douyin.com/video/{item_id}" if item_id else "",
            "cover": cover_path,
        }

    def watch_video(self, aweme_id: str, want_asr: bool = True, want_vision: bool = True) -> dict:
        """打开某个视频的详情页，把它的内容真正读出来。

        视频页会请求 `/aweme/v1/web/aweme/detail/`，里面带 play_addr；
        下回来之后走两条线：
        - 声音：ffmpeg 抽音频 → 本地 whisper 转文字
        - 画面：ffmpeg 抽 4 帧 → deepseek-flash 看图描述（vision.py）

        两条线互补：没口播的视频只有画面，有口播的也可能看不清画面。
        评论区一起收，作为氛围参考。
        """
        if not aweme_id:
            return {"ok": False, "error": "没有 aweme_id"}

        t0 = time.time()
        page = self.page
        hits: list[str] = []

        def on_resp(resp):
            if "/aweme/detail/" in resp.url:
                try:
                    hits.append(resp.text())
                except Exception:
                    pass

        page.on("response", on_resp)
        try:
            ok = self._goto(f"https://www.douyin.com/video/{aweme_id}")
            page.wait_for_timeout(4000)
            self._dismiss_login()
            try:
                page.mouse.click(640, 400)
                page.wait_for_timeout(3000)
            except Exception:
                pass

            aweme = None
            for body in hits:
                try:
                    d = json.loads(body)
                except Exception:
                    continue
                lst = d.get("aweme_list") or (
                    [d["aweme_detail"]] if d.get("aweme_detail") else []
                )
                if lst:
                    aweme = lst[0]
                    break

            comments = []
            try:
                comments = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-e2e="comment-item"]'))
                         .slice(0, 10)
                         .map(e => (e.innerText||'').replace(/\\n+/g,' ').trim().slice(0,90))
                         .filter(Boolean)"""
                ) or []
            except Exception:
                pass

            src = ""
            duration_ms = 0
            desc = ""
            if aweme:
                video = aweme.get("video") or {}
                duration_ms = int(video.get("duration") or 0)
                desc = (aweme.get("desc") or "").strip()
                play = (video.get("play_addr") or {}).get("url_list") or []
                if play:
                    src = play[0]
                # 抽 4 帧 + 16k 音频根本用不上高清，优先挑码率最低那档。
                # 实测同一条 33.2MB 下载花了 43 秒，是整轮最冤的一段。
                try:
                    cands = []
                    for b in video.get("bit_rate") or []:
                        urls = (b.get("play_addr") or {}).get("url_list") or []
                        if urls:
                            cands.append((int(b.get("bit_rate") or 0), urls[0]))
                    if cands:
                        src = min(cands, key=lambda x: x[0])[1]
                except Exception:
                    pass
            if not src:
                # 兜底：页面上 <video> 的地址
                try:
                    src = page.evaluate("() => (document.querySelector('video')||{}).src || ''")
                except Exception:
                    src = ""
            if not src:
                return {"ok": False, "error": "拿不到播放地址"}
        finally:
            try:
                page.remove_listener("response", on_resp)
            except Exception:
                pass

        mp4 = os.path.join(COVER_DIR, f"v_{aweme_id}.mp4")
        wav = os.path.join(COVER_DIR, f"v_{aweme_id}.wav")
        t_page = time.time()
        try:
            r = self._ctx.request.get(
                src, headers={"Referer": "https://www.douyin.com/"}, timeout=90000
            )
            if not r.ok or len(r.body()) < 50000:
                return {"ok": False, "error": f"视频下载失败 HTTP {r.status}"}
            with open(mp4, "wb") as f:
                f.write(r.body())
        except Exception as e:
            return {"ok": False, "error": f"下载异常 {type(e).__name__}"}

        t_dl = time.time()

        # 声音和画面互不依赖，并行跑。串行的话 20s 转写 + 12s 看图 = 三十多秒，
        # 他等过一次就再也不用了。
        def _do_asr():
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000", wav],
                    capture_output=True,
                    timeout=120,
                )
                from asr import transcribe

                res = transcribe(wav)
                if res.get("error"):
                    return "", res["error"]
                return (res.get("text") or "").strip(), ""
            except FileNotFoundError:
                return "", "宿主机没装 ffmpeg"
            except Exception as e:
                return "", f"{type(e).__name__}: {e}"

        def _do_vision():
            try:
                from vision import describe

                vres = describe(mp4)
                if vres.get("ok"):
                    return (vres.get("text") or "").strip(), ""
                return "", vres.get("note") or "看图失败"
            except Exception as e:
                return "", f"{type(e).__name__}: {e}"

        transcript, asr_note = "", ""
        visual, vision_note = "", ""
        if want_asr and want_vision:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_do_asr)
                f2 = pool.submit(_do_vision)
                transcript, asr_note = f1.result()
                visual, vision_note = f2.result()
        elif want_asr:
            transcript, asr_note = _do_asr()
        elif want_vision:
            visual, vision_note = _do_vision()

        return {
            "ok": True,
            "aweme_id": aweme_id,
            "desc": desc,
            "duration": f"{duration_ms // 1000 // 60}:{duration_ms // 1000 % 60:02d}"
            if duration_ms
            else "",
            "transcript": transcript,
            "asr_note": asr_note,
            "visual": visual,
            "vision_note": vision_note,
            "timing": {
                "page": round(t_page - t0, 1),
                "download": round(t_dl - t_page, 1),
                "content": round(time.time() - t_dl, 1),
            },
            "comments": comments[:8],
            "mp4": mp4 if os.path.exists(mp4) else "",
        }

    def share_full(self) -> dict:
        """读分享卡片 + 真去看这条视频的内容。

        timing 里分段记耗时：他说"慢"的时候要能指到具体哪一段，不能凭感觉。
        """
        t0 = time.time()
        share = self.read_share()
        t1 = time.time()
        if not share.get("ok"):
            share["timing"] = {"read": round(t1 - t0, 1)}
            return share
        detail = self.watch_video(share.get("aweme_id") or "")
        t2 = time.time()
        if detail.get("ok"):
            share["transcript"] = detail.get("transcript") or ""
            share["comments"] = detail.get("comments") or []
            share["duration"] = detail.get("duration") or ""
            share["asr_note"] = detail.get("asr_note") or ""
            share["visual"] = detail.get("visual") or ""
            share["vision_note"] = detail.get("vision_note") or ""
        else:
            share["transcript"] = ""
            share["comments"] = []
            share["asr_note"] = detail.get("error") or ""
            share["visual"] = ""
            share["vision_note"] = ""
        share["timing"] = {
            "read": round(t1 - t0, 1),
            "watch": round(t2 - t1, 1),
            **(detail.get("timing") or {}),
        }
        return share

    # ---------------- 分享卡片到私信 ----------------
    def _open_share_panel(self) -> dict:
        """在视频详情页点开「分享」面板，返回面板里的可点项。

        点击必须用 Playwright 的真实鼠标（locator.click 走 CDP 注入，算 trusted 事件）：
        分享按钮的监听挂在 React 上，JS 的 dispatchEvent 实测点不开，
        elementFromPoint 命中的还是里面的 svg（svg 连 .click() 都没有）。
        """
        page = self.page
        box, end = None, time.time() + 15
        while time.time() < end:
            box = page.evaluate(JS_SHARE_BTN)
            if box:
                break
            page.wait_for_timeout(700)
        if not box:
            return {"ok": False, "error": "没找到分享按钮", "url": page.url}

        how = ""
        # 这个按钮上只挂了 onMouseEnter / onMouseLeave，没有 onClick——
        # 网页版的分享面板是「鼠标移上去」弹出来的，点它反而什么都不会发生。
        # 所以先 hover，hover 不出来再退回点击。
        try:
            page.locator('[data-e2e="video-share-icon-container"] svg').first.hover(timeout=4000)
            how = "hover-svg"
        except Exception:
            try:
                page.mouse.move(box["x"], box["y"])
                how = "mouse-move"
            except Exception:
                how = "none"
        page.wait_for_timeout(900)
        early = page.evaluate(JS_SHARE_PANEL) or {}
        if not early.get("has_panel"):
            try:
                page.locator('[data-e2e="video-share-icon-container"] svg').first.click(timeout=3000)
                how += "+click"
                page.wait_for_timeout(700)
            except Exception:
                pass
        page.wait_for_timeout(900)

        panel = page.evaluate(JS_SHARE_PANEL) or {}
        panel.update({"ok": bool(panel.get("has_panel")), "how": how, "btn": box,
                      "early_has_panel": early.get("has_panel"),
                      "early_items": [i["t"] for i in (early.get("items") or [])][:12],
                      "url": page.url})
        if not panel.get("ok"):
            panel["error"] = "分享面板没弹出来"
        return panel

    def share_card(self, aweme_id: str = "", peer: str = "") -> dict:
        """把一条视频以「分享卡片」发进抖音私信——不是贴链接。

        路径：视频详情页 → 鼠标移到底部「分享」图标上 → 面板里的
        「分享给朋友」→ 点对应用户那一行的「分享」。对方收到的是带封面的卡片，
        点开就是原视频，和自己手点分享一模一样。
        """
        page = self.page
        t0 = time.time()
        steps: list[str] = []

        if aweme_id and f"/video/{aweme_id}" not in page.url:
            if not self._goto(f"https://www.douyin.com/video/{aweme_id}"):
                return {"ok": False, "stage": "goto", "error": "视频页打不开"}
            page.wait_for_timeout(2600)
            self._dismiss_login()

        panel = self._open_share_panel()
        steps.append(f"面板{'开了' if panel.get('ok') else '没开'}({panel.get('how')})")
        if not panel.get("ok"):
            return {"ok": False, "stage": "panel",
                    "error": panel.get("error") or "分享面板没出来",
                    "steps": steps, "url": page.url}

        fr = page.evaluate(JS_SHARE_FRIENDS) or {}
        steps.append(f"好友{fr.get('n', 0)}个")
        if not fr.get("ok"):
            return {"ok": False, "stage": "friends",
                    "error": fr.get("err") or "面板里没有可分享的好友",
                    "panel_text": (fr.get("text") or "")[:200],
                    "steps": steps, "url": page.url}

        btns = fr.get("btns") or []
        pick = None
        if peer:
            for b in btns:
                if peer in (b.get("row") or ""):
                    pick = b
                    break
            if pick is None:
                # 点名发给谁就不能随便找个替身——发错人比发不出去严重得多
                return {"ok": False, "stage": "peer",
                        "error": f"分享面板里没找到「{peer}」",
                        "peers": [b.get("row") for b in btns][:8],
                        "steps": steps, "url": page.url}
        else:
            pick = btns[0]

        clicked = page.evaluate(JS_CLICK_FRIEND, pick["i"]) or {}
        st: dict = {}
        end = time.time() + 7
        while time.time() < end:
            page.wait_for_timeout(500)
            st = page.evaluate(JS_SHARE_STATE, pick["i"]) or {}
            if st.get("sent_toast") or st.get("label") not in ("", "分享"):
                break
        steps.append(f"点了「{pick.get('row') or '第一个'}」→ {st.get('label') or '?'}")

        after = page.evaluate(JS_SHARE_PANEL) or {}
        closed = not after.get("has_panel")
        shot = os.path.join(SHOT_DIR, "share_card.png")
        try:
            page.screenshot(path=shot)
        except Exception:
            shot = ""

        # 判据不是「面板关了」——抖音分享完面板不关，会把按钮换成「捎句话」
        # 并在中间打一行「分享成功」。这两个标志任一个出现就算真发出去。
        sent = bool(clicked.get("ok")) and (
            bool(st.get("sent_toast")) or st.get("label") not in ("", "分享")
        )
        return {
            "ok": sent,
            "stage": "done",
            "peer": pick.get("row") or "",
            "button_after": st.get("label") or "",
            "toast": bool(st.get("sent_toast")),
            "panel_closed": closed,
            "error": "" if sent else "点了但没看到分享成功的标志",
            "aweme_id": aweme_id,
            "steps": steps,
            "cost_s": round(time.time() - t0, 1),
            "shot": shot,
            "url": page.url,
        }

    def share_panel_probe(self, aweme_id: str = "") -> dict:
        """一次性排查：打开视频页的分享面板，把里面的渠道名和坐标打出来。"""
        page = self.page
        if aweme_id and f"/video/{aweme_id}" not in page.url:
            self._goto(f"https://www.douyin.com/video/{aweme_id}")
            page.wait_for_timeout(2500)
        res = self._open_share_panel()
        shot = os.path.join(SHOT_DIR, "share_panel.png")
        try:
            page.screenshot(path=shot)
        except Exception:
            shot = ""
        res["shot"] = shot
        return res

    def eval_js(self, code: str):
        """调试用：在页面里跑一段 JS 看返回。

        选器只能靠真实 DOM 反推，所以留个口子，别再靠猜。
        """
        return self.page.evaluate(code)

    def _click_first_chat(self) -> str:
        """点开会话列表第一个（最近那个）。返回它的昵称。"""
        page = self.page
        item = None
        end = time.time() + 8
        while time.time() < end:
            item = page.evaluate(JS_CHAT_ITEM)
            if item:
                break
            page.wait_for_timeout(600)
        name = (item or {}).get("name") or ""
        if item:
            page.mouse.click(item["x"], item["y"])
        else:
            page.mouse.click(110, 110)  # 兜底：老坐标
        page.wait_for_timeout(1800)
        return name

    def read_dm(self, limit: int = 15) -> dict:
        """读最近那个私信会话里的文字消息，用来让她能自己看、自己回。

        只读视口内的气泡。列表能滚，但滚上去再滚回来会把页面状态搞乱，
        先只吃最近这一屏——他发的新消息总在最下面。
        """
        page = self.page
        # 轮询时大概率已经停在 /chat，别每轮都整页重载（省 2~4 秒）
        if "/chat" not in page.url:
            self._goto(CHAT_URL)
            page.wait_for_timeout(2200)
        peer = self._click_first_chat()
        msgs = page.evaluate(JS_DM_LIST) or []
        return {
            "ok": True,
            "peer": peer,
            "url": page.url,
            "n": len(msgs),
            "msgs": msgs[-limit:],
            "at": time.time(),
        }

    def dm_probe(self) -> dict:
        """探一次私信页的输入框长啥样，用来调选择器。"""
        page = self.page
        self._goto(CHAT_URL)
        page.wait_for_timeout(4500)
        page.mouse.click(110, 110)  # 列表第一项
        page.wait_for_timeout(1800)
        info = page.evaluate(JS_DM_PROBE)
        info["ok"] = True
        return info

    def _on_feed_resp(self, resp) -> None:
        """推荐流接口里带 aweme_list，顺手把 id 和文案记下来。"""
        try:
            u = resp.url
            try:
                if "douyin.com" in u:
                    self._net.append(u[:170])
                    if len(self._net) > 200:
                        del self._net[:80]
            except Exception:
                pass
            path = u.split("?")[0]
            if "/aweme/v" not in path:
                return
            if "/feed" not in path and "/list" not in path:
                return
            data = resp.json()
        except Exception:
            return
        for aw in (data.get("aweme_list") or []):
            aid = str(aw.get("aweme_id") or "")
            if not aid or any(x["id"] == aid for x in self._spy):
                continue
            self._spy.append({
                "id": aid,
                "desc": (aw.get("desc") or "").strip()[:120],
                "author": ((aw.get("author") or {}).get("nickname") or ""),
            })
        # 只留最近 40 条，别无限涨
        if len(self._spy) > 40:
            del self._spy[:-40]

    def peek(self) -> dict:
        """看一眼当前页面长什么样（排查用）。"""
        page = self.page
        info = page.evaluate(r"""
        () => {
          const as = Array.from(document.querySelectorAll('a[href]'));
          const vids = as.filter(a => (a.href || '').includes('/video/'));
          return {
            url: location.href,
            has_video: !!document.querySelector('video'),
            n_a: as.length,
            n_video_a: vids.length,
            sample_a: as.slice(0, 12).map(a => {
              const r = a.getBoundingClientRect();
              return {href: (a.href||'').slice(0,80), w: Math.round(r.width), h: Math.round(r.height),
                      text: (a.innerText||'').trim().slice(0,30)};
            }),
            sample_v: vids.slice(0, 8).map(a => {
              const r = a.getBoundingClientRect();
              return {href: (a.href||'').slice(0,90), w: Math.round(r.width), h: Math.round(r.height)};
            }),
            body: (document.body.innerText || '').slice(0, 400),
          };
        }
        """)
        info["ok"] = True
        info["net"] = list(self._net[-60:])
        info["spy"] = list(self._spy[-6:])
        return info

    def pick_link(self) -> dict:
        """从推荐流里挑一条正在播的，拿它的分享链接（用来主动甩给他）。

        精选页的卡片不是 a 标签，点它也不一定稳定跳详情页；所以走接口：
        页面加载推荐流时会请求 /aweme/v1/web/*feed*，响应里的 aweme_list
        就是这一屏的片子，id 和文案都在。
        """
        page = self.page
        self._spy.clear()
        if not self._spy_on:
            page.on("response", self._on_feed_resp)
            self._spy_on = True

        self._goto_home()
        self._wait_feed(15)
        self._scroll(2, 1.4)
        page.wait_for_timeout(1200)

        if self._spy:
            last = self._spy[-1]
            return {
                "ok": True,
                "aweme_id": last["id"],
                "url": f"https://www.douyin.com/video/{last['id']}",
                "title": last["desc"],
                "author": last.get("author") or "",
                "n_spy": len(self._spy),
            }

        # 兜底：点开当前这条，从地址栏里认（精选页点开是 modal，id 在 modal_id 里）
        info = page.evaluate(JS_PICK_LINK) or {}
        if not info.get("id"):
            try:
                page.mouse.click(640, 420)
                page.wait_for_timeout(3500)
                m = re.search(r"(?:/video/|modal_id=)(\d{15,})", page.url)
                if m:
                    info = {
                        "id": m.group(1),
                        "href": f"https://www.douyin.com/video/{m.group(1)}",
                        "text": "",
                    }
            except Exception:
                pass
        if not info.get("id"):
            return {"ok": False, "error": "推荐流里没抓到视频 id", "n_spy": 0}
        return {
            "ok": True,
            "aweme_id": info["id"],
            "url": info.get("href") or f"https://www.douyin.com/video/{info['id']}",
            "title": (info.get("text") or "")[:120],
            "n_spy": 0,
        }

    def send_dm(self, text: str) -> dict:
        """在自己的抖音私信里，给最近的那个会话发一句文字。

        网页版没有开放接口，只能走 UI：进 /chat → 点最近会话 → 点输入框
        → insert_text（中文直接进，不经过输入法）→ 回车。
        """
        page = self.page
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "空消息"}
        # 和 read_dm 点同一个会话，否则可能把回复发到别的人那儿
        if "/chat" not in page.url:
            self._goto(CHAT_URL)
            page.wait_for_timeout(3000)
        self._click_first_chat()

        box = page.evaluate(JS_DM_INPUT)
        if not box:
            return {"ok": False, "error": "没找到私信输入框"}
        page.mouse.click(box["x"] + 30, box["y"] + box["h"] / 2)
        page.wait_for_timeout(400)
        page.keyboard.insert_text(text)
        page.wait_for_timeout(600)

        # 先试回车；发出去的表现是输入框被清空
        page.keyboard.press("Enter")
        page.wait_for_timeout(1600)
        left = page.evaluate(JS_DM_LEFT)
        if left and left.strip():
            btn = page.evaluate(JS_DM_SEND_BTN)
            if btn:
                page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
                page.wait_for_timeout(1500)
                left = page.evaluate(JS_DM_LEFT)

        shot = os.path.join(SHOT_DIR, "dm.png")
        try:
            page.screenshot(path=shot, full_page=False)
        except Exception:
            shot = ""
        ok = not (left or "").strip()
        return {
            "ok": ok,
            "error": "" if ok else "输入框还有内容，可能没发出去",
            "rest": (left or "")[:80],
            "url": page.url,
            "shot": shot,
        }

    def send_dm_image(self, path: str) -> dict:
        """给自己的抖音私信发一张图。

        网页版没有接口，塞 hidden file input → 等预览 → 点发送。
        发出去的表现是预览图消失，所以以「预览里还剩不剩图」当成功判据。
        """
        page = self.page
        path = (path or "").strip()
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "文件不存在"}
        if "/chat" not in page.url:
            self._goto(CHAT_URL)
            page.wait_for_timeout(3000)
        self._click_first_chat()
        page.wait_for_timeout(600)

        n = page.evaluate(JS_MARK_FILE_INPUT)
        if not n:
            return {"ok": False, "error": "没找到上传入口"}
        try:
            page.set_input_files('[data-feibi-file="1"]', path, timeout=25000)
        except Exception as e:
            return {"ok": False, "error": f"塞文件失败 {type(e).__name__}"}
        page.wait_for_timeout(2600)

        shot1 = os.path.join(SHOT_DIR, "dm_img_before.png")
        try:
            page.screenshot(path=shot1)
        except Exception:
            shot1 = ""

        dialog = bool(page.evaluate(JS_HAS_SEND_DIALOG))
        clicked = False
        btn = page.evaluate(JS_DM_SEND_BTN)
        if btn:
            page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
            clicked = True
            page.wait_for_timeout(2400)

        left = page.evaluate(JS_DM_LEFT)
        after_dialog = bool(page.evaluate(JS_HAS_SEND_DIALOG))
        shot2 = os.path.join(SHOT_DIR, "dm_img_after.png")
        try:
            page.screenshot(path=shot2)
        except Exception:
            shot2 = ""

        # 上传成功的标志是弹了「发送给 XXX」或出现了发送按钮；
        # 发出去的标志是这个框关掉了。预览图本身不算数——确认框里的缩略图位置不固定。
        uploaded = dialog or bool(btn)
        ok = uploaded and not after_dialog
        return {
            "ok": ok,
            "error": "" if ok else ("没上传成功" if not uploaded else "确认框还在，没发出去"),
            "dialog_before": dialog,
            "clicked_send": clicked,
            "has_text_left": (left or "")[:40],
            "shot_before": shot1,
            "shot_after": shot2,
        }

    def grab_comment_stickers(self, limit: int = 3, scrolls: int = 3) -> dict:
        """从当前视频的评论区捡图。

        一张图一次打标（flash 看图约 3 秒），所以默认只捡 3 张，别一屏全端走。
        """
        import stickers

        page = self.page
        for _ in range(max(0, scrolls)):
            try:
                # 必须滚评论容器本身：在视频页滚鼠标滚轮是切下一个视频
                if not page.evaluate(JS_SCROLL_COMMENTS):
                    break
                page.wait_for_timeout(1200)
            except Exception:
                break

        imgs = page.evaluate(JS_COMMENT_IMGS) or []
        seen: set[str] = set()
        items = []
        for it in imgs:
            src = it.get("src") or ""
            if not src or src in seen:
                continue
            seen.add(src)
            if len(items) >= limit:
                break
            try:
                r = self._ctx.request.get(
                    src, headers={"Referer": "https://www.douyin.com/"}, timeout=20000
                )
                raw = r.body() if r.ok else b""
            except Exception:
                continue
            if len(raw) < 800:
                continue
            ext = ".gif" if raw[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
            res = stickers.add_bytes(raw, ext, src="douyin")
            items.append(res)

        return {"ok": bool(items), "n": len(items), "found": len(imgs), "items": items}

    def react_video(self, like: bool = True, collect: bool = False) -> dict:
        """给当前打开的视频点赞/收藏。

        不判断「是不是已经赞过」——再点一次是取消，手滑比没点更糟。
        所以调用方按 aweme_id 去重，同一条只点一次。
        """
        page = self.page
        # 详情页是可滚动的长页，刷新后可能停在评论区——那样操作栏根本不在视口里，
        # 硬按坐标点会点到评论的赞。先回顶。
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(700)
            g = page.evaluate(JS_DISMISS_GUIDE)
            if g:
                page.mouse.click(g["x"], g["y"])
                page.wait_for_timeout(700)
        except Exception:
            pass
        btns = page.evaluate(JS_REACT_BTNS) or {}
        digg = btns.get("digg")
        collect = btns.get("collect")
        if not digg:
            # 详情页那两个按钮没有 data-e2e，只能按位置找：下排横着第 1 个是赞、第 3 个是收藏
            got = page.evaluate(JS_ACTION_BTNS) or {}
            row = got.get("row") or []
            col = got.get("col") or []
            seq = row if len(row) >= 3 else col
            if len(seq) >= 3:
                digg = seq[0]
                collect = collect or seq[2]
        if not digg:
            return {"ok": False, "error": "没找到点赞按钮"}
        out = {"ok": True, "has_collect": bool(collect), "src": "e2e" if btns.get("digg") else "geo"}
        if like:
            page.mouse.click(digg["x"], digg["y"])
            page.wait_for_timeout(1100)
            out["liked"] = True
        if collect and collect:
            page.mouse.click(collect["x"], collect["y"])
            page.wait_for_timeout(1600)
            btn = page.evaluate(JS_COLLECT_CONFIRM)
            if btn:
                page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
                page.wait_for_timeout(900)
                out["collect_confirm"] = True
            out["collected"] = True
        shot = os.path.join(SHOT_DIR, "react.png")
        try:
            page.screenshot(path=shot)
        except Exception:
            shot = ""
        out["shot"] = shot
        return out

    def detail(self, want_comment: bool = True) -> dict:
        """点开当前第一条视频，读评论区。"""
        page = self.page
        try:
            page.mouse.click(400, 300)
            page.wait_for_timeout(3500)
        except Exception:
            pass
        info = page.evaluate(JS_DETAIL)
        return {
            "url": info.get("url", ""),
            "comments": info.get("comments", [])[:8],
            "has_video": info.get("has_video", False),
        }

    def report(self, days: int = 1) -> dict:
        mem = _load_memory()
        hist = mem.get("history", [])
        by_tag: dict[str, int] = {}
        for h in hist[-60:]:
            for t in h.get("tags", []):
                by_tag[t] = by_tag.get(t, 0) + 1
        top = sorted(by_tag.items(), key=lambda kv: -kv[1])[:8]
        return {
            "ok": True,
            "watch_total": len(hist),
            "recent": hist[-12:],
            "top_tags": [{"tag": k, "n": v} for k, v in top],
            "stats": mem.get("stats", {}),
        }


if __name__ == "__main__":
    eng = DouyinEngine(headless=False)
    eng.start()
    try:
        t0 = time.time()
        s = eng.session(scrolls=2, picks=3)
        print("elapsed", round(time.time() - t0, 1), "s")
        print(json.dumps(s, ensure_ascii=False, indent=2)[:2500])
        d = eng.detail()
        print("DETAIL", d["url"], "comments", len(d["comments"]))
        for c in d["comments"][:3]:
            print("  -", c[:80].replace("\n", " / "))
    finally:
        eng.close()
