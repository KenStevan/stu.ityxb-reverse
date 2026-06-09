# -*- coding: utf-8 -*-
"""
传智播客高校学习平台 (stu.ityxb.com) 预习视频自动刷课
========================================================
- requests + Session 维持登录态(cookie)
- SPEED 全局倍速：控制刷视频的速度（每秒真实时间上报多少秒播放进度）
- 复刻 preview/updateProgress 进度上报(明文 POST) + updateWatchDuration 心跳(RSA 加密)

流程(对应浏览器抓包):
  1. bxg/course/getHaveList      -> 我的所有课程 (courseId)
  2. bxg/preview/list            -> 某课程下的所有预习 (previewId)
  3. bxg/preview/info            -> 某预习的章节/小节(points, 每个含 video_duration)
  4. bxg/preview/updateWatchDuration  -> 上报某小节的观看进度(请求体 RSA 加密)

加密原理(已从平台 JS 逆向确认):
  http 封装模块 830b 的 b(t):
    JSEncrypt.setPublicKey(<硬编码 1024 位 RSA 公钥>)
    return JSEncrypt.encrypt(JSON.stringify(t))   # PKCS#1 v1.5, 输出 base64
  w() 把 config.data 取出传给 b() (整 config 超 117 字节塞不进单块, 故只加密 data),
  以 text/plain POST 到 /back/<url>。
  RSA 公钥加密单向, 只需公钥即可生成合法请求, 无需私钥。

依赖: pip install requests pycryptodome
"""

import time
import json
import base64
import argparse
import re
from urllib.parse import unquote

import requests

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


# ============================================================
#  一、配置区
# ============================================================

# 登录态 Cookie(含 HttpOnly 的 ityxb_sss 会话令牌)。
# 推荐用 EditThisCookie 插件导出(分号分隔), document.cookie 读不到 HttpOnly 会漏 ityxb_sss -> 请登录。
#   - 留空: 运行时脚本会提示你粘贴
#   - 或直接把整段 Cookie 粘到这里
def parse_cookie_file(file_path):
    cookies = ""

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('//'):
                continue

            # 按分号分割多个 cookie
            # for pair in line.split(';'):
            #     pair = pair.strip()
            #     if '=' not in pair:
            #         continue
            #     # 只分割第一个 =，防止 value 里有 =
            #     key, value = pair.split('=', 1)
            #     cookies[key.strip()] = value.strip()
            cookies+=line.strip()

    return cookies



COOKIE=""
COOKIE = parse_cookie_file("cookie.tnt")
print(COOKIE)

# login-name 请求头(登录账号/手机号)。留空 = 运行时自动解析, 无需写死。
# 解析顺序: 此处手动配置 > _uc_t_ cookie 解析 > loginInfo 接口查询
LOGIN_NAME = ""

# 全局倍速: SPEED=10 表示「真实 1 秒 = 进度推进 10 秒」
# 例如一段 600 秒的视频, SPEED=10 时大约 60 秒真实时间刷完
SPEED = 10

# 上报节奏: 每隔 INTERVAL 秒(真实时间)上报一次, 每次进度推进 INTERVAL*SPEED 秒
# 平台只接受「单调递增」的进度, 单次跳跃过大可能被风控; 建议 INTERVAL*SPEED <= 60
INTERVAL = 5

# 指定要刷的 courseId 列表; 留空 [] = 刷 getHaveList 返回的全部课程
COURSE_IDS = []

# 只刷单个 preview 做快速验证(填 previewId); 留空 = 按课程目录遍历
TARGET_PREVIEW_ID = ""

# 只刷未完成的(progress100 < 100); 设 False 则连已完成的也重刷一遍
SKIP_DONE = True

# 请求间隔轻微抖动, 模拟真实用户
JITTER = 0.3


# ============================================================
#  二、RSA 加密(公钥从平台 JS 830b 模块 b() 提取)
# ============================================================
RSA_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDoJCfMU6AG0Wc3zJqgVFhiPJVCFz0+3VCtjklx"
    "712todjRIX/d3CT4/t0xG07/YfZBuiXPr9kcBRahhEJNG8TcouDwLcZfBB+74kMy/EwWrErIUZvv"
    "uEmdOcxqGVeLJWr3rZb/I37rJkoz2pCFyQ3aYmIZ1xHTiqLWAkWc9iZC3wIDAQAB"
)

_RSA_KEY = None
_RSA_CIPHER = None


def _get_cipher():
    global _RSA_KEY, _RSA_CIPHER
    if _RSA_CIPHER is not None:
        return _RSA_CIPHER
    if not _HAS_CRYPTO:
        raise RuntimeError("缺少 pycryptodome, 请: pip install pycryptodome")
    der = base64.b64decode(RSA_PUBKEY_B64)
    _RSA_KEY = RSA.import_key(der)
    if _RSA_KEY.size_in_bits() != 1024:
        raise RuntimeError(f"公钥位长异常: {_RSA_KEY.size_in_bits()} (期望 1024)")
    _RSA_CIPHER = PKCS1_v1_5.new(_RSA_KEY)
    return _RSA_CIPHER


def encrypt_payload(obj) -> str:
    """RSA-1024 / PKCS#1 v1.5 加密 JSON 对象, 返回 base64 字符串。

    明文上限 = 128 - 11 = 117 字节; {previewId,pointId,watchedDuration} 约 115 字节, 刚好单块。
    JSEncrypt 默认 PKCS#1 v1.5 + 随机填充 => 每次密文不同但服务端均可解密。
    """
    plain = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(plain) > 117:
        raise ValueError(f"明文 {len(plain)} 字节超过 RSA-1024 单块上限 117: {plain!r}")
    cipher = _get_cipher()
    ct = cipher.encrypt(plain)          # 128 字节
    return base64.b64encode(ct).decode("ascii")


# ============================================================
#  三、Session & 公共请求
# ============================================================
BASE = "https://stu.ityxb.com/back/bxg"

COMMON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": "https://stu.ityxb.com",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}


def read_cookie() -> str:
    """返回要用的 Cookie。COOKIE 配置为空时, 运行时提示用户粘贴 document.cookie。"""
    if COOKIE.strip():
        return COOKIE.strip()
    print("未配置 COOKIE，请获取后粘贴：")
    print("  1. 浏览器登录 https://stu.ityxb.com 并打开任一学习页")
    print("  2. F12 → Console 控制台")
    print("  3. 输入  document.cookie  回车")
    print("  4. 复制输出的整行 (name=value; name=value; ...) 粘到下面")
    raw = input("Cookie> ").strip()
    # 兼容用户误带引号 / Cookie: 前缀
    raw = raw.strip('"').strip("'")
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    if not raw:
        raise SystemExit("未输入 Cookie，退出。")
    return raw


def get_cookie_value(raw_cookie: str, name: str) -> str:
    """从 'a=1; b=2' 串里取某个 cookie 的值。"""
    prefix = name + "="
    for kv in raw_cookie.split(";"):
        kv = kv.strip()
        if kv.startswith(prefix):
            return kv[len(prefix):]
    return ""


def resolve_login_name(session: requests.Session, raw_cookie: str) -> str:
    """运行时确定 login-name(登录账号/手机号), 不写死。
    优先级: 手动配置 LOGIN_NAME > _uc_t_ cookie 解析 > loginInfo 接口。
    _uc_t_ URL 解码后形如:  userId;手机号;token;bxg;timestamp
    """
    if LOGIN_NAME.strip():
        return LOGIN_NAME.strip()
    # 1) 从 _uc_t_ cookie 解析手机号
    uc = get_cookie_value(raw_cookie, "_uc_t_")
    m = re.search(r"(?<!\d)1\d{10}(?!\d)", unquote(uc))
    if m:
        return m.group(0)
    # 2) 查 loginInfo 接口(bxg_anon, 仅靠 Cookie 鉴权)
    try:
        resp = session.get(
            "https://stu.ityxb.com/back/bxg_anon/user/loginInfo",
            params={"t": int(time.time() * 1000)},
            headers={**COMMON_HEADERS, "referer": "https://stu.ityxb.com/"},
            timeout=10,
        )
        ln = (resp.json().get("resultObject") or {}).get("login_name")
        if ln:
            return str(ln)
    except Exception:
        pass
    return ""   # 取不到就空着, 多数接口靠 Cookie 鉴权仍可跑


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(COMMON_HEADERS)
    # 把 COOKIE 原样塞进 Cookie 请求头, 不做任何拆分/解析
    s.headers["Cookie"] = read_cookie()
    # login-name 运行时解析(手机号), 不硬编码
    login_name = resolve_login_name(s, s.headers["Cookie"])
    if login_name:
        s.headers["login-name"] = login_name
        print(f"[登录态] login-name = {login_name}")
    return s


def _ok(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return {"success": False, "errorMessage": f"非JSON响应: {resp.text[:200]}"}


# ---------- 接口封装 ----------

def get_have_list(s: requests.Session):
    """1. 我的课程列表"""
    url = f"{BASE}/course/getHaveList"
    resp = s.post(
        url,
        data={"type": 1, "pageNumber": 1, "pageSize": 100},
        headers={
            **COMMON_HEADERS,
            "content-type": "application/x-www-form-urlencoded",
            "referer": "https://stu.ityxb.com/Classroom/course/learning",
        },
    )
    return _ok(resp)


def get_preview_list(s: requests.Session, course_id: str):
    """2. 某课程下的所有预习"""
    url = f"{BASE}/preview/list"
    params = {
        "name": "", "isEnd": "", "pageNumber": 1, "pageSize": 200,
        "type": 1, "courseId": course_id, "t": int(time.time() * 1000),
    }
    resp = s.get(
        url,
        params=params,
        headers={
            **COMMON_HEADERS,
            "referer": f"https://stu.ityxb.com/learning/{course_id}/preview/list",
        },
    )
    return _ok(resp)


def get_preview_info(s: requests.Session, preview_id: str):
    """3. 预习详情: chapters[].points[] (每个 point 是一段视频)"""
    url = f"{BASE}/preview/info"
    resp = s.get(
        url,
        params={"previewId": preview_id, "t": int(time.time() * 1000)},
        headers={
            **COMMON_HEADERS,
            "referer": f"https://stu.ityxb.com/preview/detail/{preview_id}",
        },
    )
    return _ok(resp)


def update_progress(s: requests.Session, preview_id: str, point_id, watched: int):
    """4. 上报单节进度(秒)。bxg/preview/updateProgress, 普通 POST(form 表单, 不加密)。
    参数 {previewId, pointId, watchedDuration}。服务端直接记录 watchedDuration 值。
    (这才是真正推进 progress100 的接口; 加密的 updateWatchDuration 只是心跳。)
    """
    url = f"{BASE}/preview/updateProgress"
    data = {
        "previewId": preview_id,
        "pointId": point_id,
        "watchedDuration": int(watched),
    }
    resp = s.post(
        url,
        data=data,
        headers={
            **COMMON_HEADERS,
            "content-type": "application/x-www-form-urlencoded",
            "referer": f"https://stu.ityxb.com/preview/detail/{preview_id}",
        },
    )
    return resp, data


def send_heartbeat(s: requests.Session, preview_id: str, seconds: int = 60):
    """可选: 观看心跳 bxg/preview/updateWatchDuration(RSA 加密 {time,previewId})。
    不推进进度, 仅模拟真实观看行为降低风控概率。默认不调用(测试证明 updateProgress 单独即可推进)。
    """
    url = f"{BASE}/preview/updateWatchDuration"
    body = encrypt_payload({"time": int(seconds), "previewId": preview_id})
    return s.post(
        url,
        data=body,
        headers={
            **COMMON_HEADERS,
            "content-type": "text/plain",
            "referer": f"https://stu.ityxb.com/preview/detail/{preview_id}",
        },
    )


# ============================================================
#  四、刷课主逻辑
# ============================================================

def iter_points(info):
    """从 preview/info 的结果里展平出所有 video point。"""
    result = info.get("resultObject") or {}
    for ch in result.get("chapters") or []:
        for p in ch.get("points") or []:
            yield p


def watch_point(s, preview_id, point):
    """把一段 point(视频)刷到 100%。"""
    pid = point.get("point_id") or point.get("id")
    name = point.get("point_name") or point.get("name") or "?"
    total = point.get("video_duration") or point.get("duration") or 0
    watched = point.get("watched_duration") or 0   # 已看秒数(接着看, 不重头)
    done = point.get("progress100") or 0

    if SKIP_DONE and done >= 100:
        print(f"    [跳过] {name} (已完成 {done}%)")
        return
    if total <= 0:
        print(f"    [跳过] {name} (无视频时长, 可能是文档/习题)")
        return

    print(f"    [刷课] {name}  时长={total}s 已看={watched}s 进度={done}%  "
          f"(SPEED={SPEED}x, 预计 ~{max(1, int((total - watched) / SPEED))}s)")
    step = max(1, int(INTERVAL * SPEED))
    watched = min(watched, total)
    last = -1
    while watched < total:
        watched = min(watched + step, total)
        if watched == last:           # 单调递增保护
            break
        last = watched
        resp, obj = update_progress(s, preview_id, pid, watched)
        try:
            data = resp.json()
        except ValueError:
            data = {"success": False, "raw": resp.text[:120]}
        ok = data.get("success")
        pct = int(watched / total * 100)
        flag = "OK " if ok else "FAIL"
        print(f"        -> {flag} {pct:3d}%  ({watched}/{total}s)"
              + ("" if ok else f"  resp={data}"))
        if not ok:
            print(f"        [提示] 上报失败。明文样本: {json.dumps(obj, separators=(',',':'))}")
            print(f"        若返回鉴权/参数错误, 优先检查: ① COOKIE 是否过期 ② pointId 取值")
            return False
        time.sleep(INTERVAL + (JITTER if watched & 1 else -JITTER))
    print(f"    [完成] {name} -> 100%")
    return True


def watch_preview(s, preview_id):
    info = get_preview_info(s, preview_id)
    if not info.get("success"):
        print(f"  预习详情获取失败: {info}")
        return
    title = (info.get("resultObject") or {}).get("preview", {}).get("previewName", preview_id)
    print(f"  ▶ 预习: {title}  ({preview_id})")
    pts = list(iter_points(info))
    if not pts:
        print("    (该预习没有视频小节)")
        return
    print(f"    共 {len(pts)} 个小节")
    for p in pts:
        watch_point(s, preview_id, p)


def main():
    global SPEED
    parser = argparse.ArgumentParser(description="传智播客预习视频自动刷课")
    parser.add_argument("--speed", type=float, default=SPEED, help="全局倍速(默认 %(default)s)")
    parser.add_argument("--preview-id", default=TARGET_PREVIEW_ID, help="只刷单个 previewId")
    parser.add_argument("--course-id", action="append", default=COURSE_IDS, help="指定 courseId(可多次)")
    parser.add_argument("--list-only", action="store_true", help="只列出课程/预习, 不刷")
    parser.add_argument("--self-test", action="store_true", help="只验证 RSA 加密能产生 128 字节密文, 不发请求")
    args = parser.parse_args()

    SPEED = args.speed

    # 自检: RSA 公钥 + 加密管线是否正常
    if args.self_test:
        ct = encrypt_payload({"previewId": "a48c391b62e7404097121b7ccfb9a7bf",
                              "pointId": "00000000000000000000000000000000",
                              "watchedDuration": 1})
        raw = base64.b64decode(ct)
        print(f"明文加密后 base64 长度={len(ct)} 字符, 解码后 {len(raw)} 字节")
        print("✅ RSA-1024 加密自检通过" if len(raw) == 128 else f"❌ 期望 128 字节, 实际 {len(raw)}")
        return

    s = build_session()
    print(f"=== 传智播客预习刷课  SPEED={SPEED}x  INTERVAL={INTERVAL}s ===")

    # 单个 preview 快速验证
    if args.preview_id:
        watch_preview(s, args.preview_id)
        return

    # 否则遍历课程
    have = get_have_list(s)
    if not have.get("success"):
        print(f"获取课程列表失败: {have}")
        if "needLogin" in str(have) or "请登录" in str(have):
            print("[提示] 返回「请登录」= Cookie 里缺会话令牌(通常是 HttpOnly 的 ityxb_sss)。")
            print("       document.cookie 读不到 HttpOnly Cookie, 请改从")
            print("       F12 → Application → Cookies → stu.ityxb.com 里把 ityxb_sss 的值")
            print("       连同其它 cookie 一起复制, 补进 COOKIE 串。")
        return
    courses = (have.get("resultObject") or {}).get("items") or []
    print(f"我的课程: {len(courses)} 门")

    for c in courses:
        cid = c.get("id") or c.get("courseId")
        cname = c.get("courseName") or c.get("name") or cid
        if args.course_id and cid not in args.course_id:
            continue
        print(f"\n课程: {cname}  ({cid})")
        pv = get_preview_list(s, cid)
        if not pv.get("success"):
            print(f"  预习列表获取失败: {pv}")
            continue
        previews = (pv.get("resultObject") or {}).get("items") or []
        print(f"  预习: {len(previews)} 个")
        if args.list_only:
            for p in previews:
                print(f"    - {p.get('previewName', p.get('name', ''))}  "
                      f"previewId={p.get('id')}")
            continue
        for p in previews:
            pid = p.get("id") or p.get("previewId")
            watch_preview(s, pid)

    print("\n全部完成")


if __name__ == "__main__":
    main()
