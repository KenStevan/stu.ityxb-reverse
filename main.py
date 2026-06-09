#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
传智播客高校学习平台 (stu.ityxb.com) 预习视频自动刷课 —— 优化版 (main.py)
======================================================================
核心接口(已逆向确认):
  - bxg/preview/updateProgress   普通 POST(form 表单) {previewId,pointId,watchedDuration}
                                 ↑ 真正推进单节进度, 服务端直接接收 watchedDuration 值
  - bxg/preview/updateWatchDuration  RSA 加密 {time,previewId}  仅心跳(可选, 不推进进度)

为什么这样: 平台有两个同名相似的接口。updateWatchDuration(RSA 加密)只是观看心跳;
真正刷进度的是 updateProgress(明文 POST)。服务端对 watchedDuration 的大幅跳跃很宽松,
所以可以大步长快速刷完。

用法:
  python3 main.py                                  # 刷全部课程的全部预习
  python3 main.py --preview-id <id>                # 只刷一个预习
  python3 main.py --course-id <id> --speed 200     # 指定课程, 每次 +200s
  python3 main.py --list-only                      # 只列清单不刷

依赖: pip install requests pycryptodome
"""

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    RSA = None  # 仅心跳功能需要; 刷进度(updateProgress)不需要


# ============================================================
#  配置
# ============================================================
COOKIE_FILE = "cookie.tnt"     # EditThisCookie 导出的 cookie 文件(分号分隔, 含 ityxb_sss)
COOKIE = ""                    # 或直接把整段 Cookie 写这里, 优先于文件
LOGIN_NAME = ""                # 留空 = 自动从 _uc_t_ cookie 解析手机号

DEFAULT_SPEED = 100            # 每次 updateProgress 推进的秒数(服务端对大跳跃宽松, 100~500 都行)
DEFAULT_INTERVAL = 1.5         # 两次上报之间的真实秒数(防风控, 别太密)
SKIP_DONE = True               # 跳过已完成(progress100>=100)的小节
HEARTBEAT = False              # 是否额外发 RSA 加密心跳(模拟真实观看, 默认关; updateProgress 单独即够)
MAX_RETRY = 2                  # 单次上报失败重试次数
JITTER = 0.3                   # 间隔抖动, 模拟真实用户

# RSA 公钥(平台 app.13cf5afc.js 的 830b 模块 b() 内硬编码) —— 仅心跳用
RSA_PUBKEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDoJCfMU6AG0Wc3zJqgVFhiPJVCFz0+3VCtjklx"
    "712todjRIX/d3CT4/t0xG07/YfZBuiXPr9kcBRahhEJNG8TcouDwLcZfBB+74kMy/EwWrErIUZvv"
    "uEmdOcxqGVeLJWr3rZb/I37rJkoz2pCFyQ3aYmIZ1xHTiqLWAkWc9iZC3wIDAQAB"
)

BASE = "https://stu.ityxb.com/back/bxg"
ORIGIN = "https://stu.ityxb.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


# ============================================================
#  RSA 加密(仅心跳)
# ============================================================
_cipher = None


def rsa_encrypt(obj) -> str:
    global _cipher
    if RSA is None:
        raise RuntimeError("心跳需要 pycryptodome: pip install pycryptodome (或关闭 HEARTBEAT)")
    if _cipher is None:
        _cipher = PKCS1_v1_5.new(RSA.import_key(base64.b64decode(RSA_PUBKEY)))
    plain = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(plain) > 117:
        raise ValueError(f"明文 {len(plain)} 字节超过 RSA-1024 单块上限 117")
    return base64.b64encode(_cipher.encrypt(plain)).decode("ascii")


# ============================================================
#  Cookie / Session
# ============================================================
def _read_cookie_file() -> str:
    """读 cookie.tnt, 兼容 EditThisCookie 导出(带 // 注释行)。无文件返回 ''。"""
    p = Path(COOKIE_FILE)
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8").strip()
    parts = [ln.strip() for ln in txt.splitlines()
             if ln.strip() and not ln.strip().startswith("//")]
    return "".join(parts) if parts else txt


def _prompt_cookie() -> str:
    """交互输入 Cookie。"""
    print("请粘贴 Cookie(EditThisCookie 导出, 必须含 HttpOnly 的 ityxb_sss):")
    raw = input("Cookie> ").strip().strip('"').strip("'")
    return raw[len("Cookie:"):].strip() if raw.lower().startswith("cookie:") else raw


def load_cookie(override: str = None, ask: bool = False) -> str:
    """加载顺序: override(--cookie) > ask 强制交互 > COOKIE 直填 > cookie.tnt 文件 > 交互输入。"""
    if override and override.strip():          # --cookie 直接传
        c = override.strip().strip('"').strip("'")
        return c[len("Cookie:"):].strip() if c.lower().startswith("cookie:") else c
    if ask:                                    # --ask 强制交互输入
        return _prompt_cookie()
    if COOKIE.strip():                         # 模块常量直填
        return COOKIE.strip()
    txt = _read_cookie_file()                  # cookie.tnt 文件
    if txt and cookie_value(txt, "ityxb_sss"):
        return txt
    if txt:                                    # 有文件但缺 ityxb_sss, 提示后仍用
        print(f"⚠️  cookie.tnt 里没找到 ityxb_sss, 可能登录态缺失。")
    return _prompt_cookie()                    # 兜底交互输入


def cookie_value(cookie: str, name: str) -> str:
    for kv in cookie.split(";"):
        kv = kv.strip()
        if kv.startswith(name + "="):
            return kv[len(name) + 1:]
    return ""


def resolve_login_name(cookie: str) -> str:
    """从 _uc_t_ cookie 解析手机号(userId;手机号;token;bxg;ts)。"""
    if LOGIN_NAME.strip():
        return LOGIN_NAME.strip()
    m = re.search(r"(?<!\d)1\d{10}(?!\d)", unquote(cookie_value(cookie, "_uc_t_")))
    return m.group(0) if m else ""


def build_session(override: str = None, ask: bool = False) -> requests.Session:
    cookie = load_cookie(override=override, ask=ask)
    if not cookie_value(cookie, "ityxb_sss"):
        print("⚠️  Cookie 缺 ityxb_sss(HttpOnly 会话令牌) -> 服务端会返回「请登录」。")
        print("    别用 document.cookie(读不到 HttpOnly), 用 EditThisCookie 导出 cookie.tnt。")
    s = requests.Session()
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "origin": ORIGIN,
        "user-agent": UA,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": cookie,   # 原样进请求头
    })
    ln = resolve_login_name(cookie)
    if ln:
        s.headers["login-name"] = ln
        print(f"[登录] {ln}")
    return s


# ============================================================
#  API
# ============================================================
def _json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"success": False, "errorMessage": f"非JSON: {resp.text[:120]}"}


def get_courses(s: requests.Session) -> list:
    r = s.post(f"{BASE}/course/getHaveList",
               data={"type": 1, "pageNumber": 1, "pageSize": 100},
               headers={"content-type": "application/x-www-form-urlencoded",
                        "referer": f"{ORIGIN}/Classroom/course/learning"})
    d = _json(r)
    return (d.get("resultObject") or {}).get("items") or [] if d.get("success") else []


def get_previews(s: requests.Session, course_id: str) -> list:
    r = s.get(f"{BASE}/preview/list",
              params={"name": "", "isEnd": "", "pageNumber": 1, "pageSize": 200,
                      "type": 1, "courseId": course_id, "t": int(time.time() * 1000)},
              headers={"referer": f"{ORIGIN}/learning/{course_id}/preview/list"})
    d = _json(r)
    return (d.get("resultObject") or {}).get("items") or [] if d.get("success") else []


def get_points(s: requests.Session, preview_id: str) -> list:
    """返回 preview/info 里展平的所有 video point。"""
    r = s.get(f"{BASE}/preview/info",
              params={"previewId": preview_id, "t": int(time.time() * 1000)},
              headers={"referer": f"{ORIGIN}/preview/detail/{preview_id}"})
    d = _json(r)
    if not d.get("success"):
        return []
    pts = []
    for ch in (d.get("resultObject") or {}).get("chapters") or []:
        for p in ch.get("points") or []:
            pts.append(p)
    return pts


def update_progress(s: requests.Session, preview_id: str, point_id, watched: int) -> dict:
    """核心: 推进单节进度。明文 POST。"""
    r = s.post(f"{BASE}/preview/updateProgress",
               data={"previewId": preview_id, "pointId": point_id, "watchedDuration": int(watched)},
               headers={"content-type": "application/x-www-form-urlencoded",
                        "referer": f"{ORIGIN}/preview/detail/{preview_id}"})
    return _json(r)


def send_heartbeat(s: requests.Session, preview_id: str, seconds: int = 60):
    """可选: RSA 加密心跳 {time, previewId}。不推进进度。"""
    s.post(f"{BASE}/preview/updateWatchDuration",
           data=rsa_encrypt({"time": int(seconds), "previewId": preview_id}),
           headers={"content-type": "text/plain",
                    "referer": f"{ORIGIN}/preview/detail/{preview_id}"})


# ============================================================
#  刷课主逻辑
# ============================================================
def watch_point(s, preview_id, point, speed, interval, heartbeat):
    pid = point.get("point_id") or point.get("id")
    name = point.get("point_name") or point.get("name") or "?"
    total = point.get("video_duration") or 0
    watched = point.get("watched_duration") or 0
    done = point.get("progress100") or 0

    if SKIP_DONE and done >= 100:
        print(f"    [跳过] {name} (已完成)")
        return True
    if total <= 0:
        print(f"    [跳过] {name} (非视频)")
        return True

    eta = max(1, int((total - watched) / speed * interval))
    print(f"    [刷] {name}  {watched}/{total}s ({done}%)  → 速度 {speed}s/次, 预计 ~{eta}s")
    watched = min(watched, total)
    while watched < total:
        watched = min(watched + speed, total)
        ok = False
        for attempt in range(MAX_RETRY + 1):
            res = update_progress(s, preview_id, pid, watched)
            if res.get("success"):
                ok = True
                break
            if attempt < MAX_RETRY:
                time.sleep(1)
        pct = int(watched / total * 100)
        print(f"        {'OK ' if ok else 'FAIL'}  {pct:3d}%  ({watched}/{total}s)"
              + ("" if ok else f"  {res}"))
        if not ok:
            return False
        if HEARTBEAT and heartbeat:
            try:
                send_heartbeat(s, preview_id, min(speed, 60))
            except Exception:
                pass
        time.sleep(interval + (JITTER if watched & 1 else -JITTER))
    print(f"    [完成] {name} ✓")
    return True


def watch_preview(s, preview_id, speed, interval, heartbeat):
    points = get_points(s, preview_id)
    if not points:
        print(f"  预习 {preview_id}: 无小节或获取失败")
        return
    todo = [p for p in points if (p.get("progress100") or 0) < 100] if SKIP_DONE else points
    print(f"  预习 {preview_id}: 共 {len(points)} 节, 待刷 {len(todo)} 节")
    for p in points:
        if not watch_point(s, preview_id, p, speed, interval, heartbeat):
            print(f"  !! {p.get('point_name')} 刷失败, 跳过")


def main():
    ap = argparse.ArgumentParser(description="传智播客预习自动刷课(优化版)")
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED, help=f"每次推进秒数(默认 {DEFAULT_SPEED}, 越大越快)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help=f"两次上报间隔秒(默认 {DEFAULT_INTERVAL})")
    ap.add_argument("--preview-id", help="只刷单个预习 previewId")
    ap.add_argument("--course-id", action="append", default=[], help="指定 courseId(可多次)")
    ap.add_argument("--list-only", action="store_true", help="只列出课程/预习")
    ap.add_argument("--redo", action="store_true", help="不跳过已完成的小节, 重刷")
    ap.add_argument("--cookie", help="直接传 Cookie 字符串(优先于 cookie.tnt 和交互输入)")
    ap.add_argument("--ask", action="store_true", help="强制交互输入 Cookie(忽略 cookie.tnt)")
    args = ap.parse_args()

    global SKIP_DONE
    SKIP_DONE = not args.redo

    s = build_session(override=args.cookie, ask=args.ask)
    print(f"=== 刷课开始  {args.speed}s/次 × 间隔{args.interval}s  心跳={'开' if HEARTBEAT else '关'} ===")

    if args.preview_id:
        watch_preview(s, args.preview_id, args.speed, args.interval, HEARTBEAT)
        return

    courses = get_courses(s)
    if not courses:
        print("获取课程失败: 检查 cookie.tnt 里的 ityxb_sss 是否有效。")
        return
    print(f"我的课程: {len(courses)} 门")
    for c in courses:
        cid = c.get("id") or c.get("courseId")
        if args.course_id and cid not in args.course_id:
            continue
        print(f"\n课程: {c.get('courseName', cid)}  ({cid})")
        previews = get_previews(s, cid)
        print(f"  预习: {len(previews)} 个")
        if args.list_only:
            for p in previews:
                print(f"    - {p.get('previewName','')}  id={p.get('id')}")
            continue
        for p in previews:
            watch_preview(s, p.get("id") or p.get("previewId"),
                          args.speed, args.interval, HEARTBEAT)
    print("\n全部完成 ✅")


if __name__ == "__main__":
    main()
