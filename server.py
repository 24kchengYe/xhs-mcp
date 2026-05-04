"""
XHS-MCP: 小红书内容采集 MCP Server

架构：
  Claude Code ←MCP stdio→ 本Server ←HTTP /request→ 社媒助手Node Server(:3082)
                                         ←Socket.IO→ Chrome开源版插件 ←→ 小红书API

端口：3082（社媒助手 Node Server）
MCP通信：stdio（无额外端口）
"""

import json
import os
import sys
import time
import random
import urllib.request
import urllib.error

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("pip install 'mcp[cli]'", file=sys.stderr)
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

COPILOT_SERVER = os.environ.get("COPILOT_SERVER", "http://localhost:3082")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ============================================================
# 请求统计（全局 monitor）
# ============================================================

_stats = {
    "requests": 0,
    "success": 0,
    "failed": 0,
    "retries": 0,
    "notes_scraped": 0,
    "images_downloaded": 0,
    "consecutive_failures": 0,
    "start_time": None,
}


def _reset_stats():
    _stats.update({
        "requests": 0, "success": 0, "failed": 0, "retries": 0,
        "notes_scraped": 0, "images_downloaded": 0,
        "consecutive_failures": 0, "start_time": time.time(),
    })


# ============================================================
# 网络层 — 带重试 + 自适应延时
# ============================================================

def proxy_request(url: str, method: str = "GET", data: dict | None = None,
                  max_retries: int = 3) -> dict:
    """通过社媒助手代理请求，带指数退避重试"""
    payload = {"url": url, "method": method}
    if data:
        payload["data"] = data
    req_data = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries):
        _stats["requests"] += 1
        req = urllib.request.Request(
            f"{COPILOT_SERVER}/request",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            _stats["success"] += 1
            _stats["consecutive_failures"] = 0
            return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 412:
                _stats["failed"] += 1
                return {"error": "Chrome插件未连接"}
            # 461 等服务端错误不重试
            if e.code in (461, 400, 403):
                _stats["failed"] += 1
                return {"error": f"HTTP {e.code}: {body[:200]}"}
            # 5xx / 429 可重试
            if attempt < max_retries - 1:
                _stats["retries"] += 1
                time.sleep(2 ** attempt + random.random())
                continue
            _stats["failed"] += 1
            _stats["consecutive_failures"] += 1
            return {"error": f"HTTP {e.code} (重试{max_retries}次后失败): {body[:200]}"}
        except Exception as e:
            if attempt < max_retries - 1:
                _stats["retries"] += 1
                time.sleep(2 ** attempt + random.random())
                continue
            _stats["failed"] += 1
            _stats["consecutive_failures"] += 1
            return {"error": f"{e} (重试{max_retries}次后失败)"}

    return {"error": "未知错误"}


def _smart_sleep():
    """自适应延时：正常 2-4s，连续失败后 8-15s"""
    cf = _stats["consecutive_failures"]
    if cf >= 3:
        delay = random.uniform(8, 15)
    elif cf >= 1:
        delay = random.uniform(4, 8)
    else:
        delay = random.uniform(2, 4)
    time.sleep(delay)


# ============================================================
# 存储层
# ============================================================

def _safe_name(s: str, max_len: int = 30) -> str:
    return "".join(c for c in s if c not in r'\/:*?"<>|').strip()[:max_len].strip()


def _user_dir(user_id: str, nickname: str = "") -> str:
    users_root = os.path.join(DATA_DIR, "users")
    os.makedirs(users_root, exist_ok=True)
    if os.path.exists(users_root):
        for d in os.listdir(users_root):
            if d.endswith(f"_{user_id}"):
                return os.path.join(users_root, d)
    safe_nick = _safe_name(nickname) if nickname else ""
    dirname = f"{safe_nick}_{user_id}" if safe_nick else user_id
    d = os.path.join(users_root, dirname)
    os.makedirs(d, exist_ok=True)
    return d


def _note_dir(user_id: str, note_id: str, nickname: str = "",
              title: str = "") -> str:
    udir = _user_dir(user_id, nickname)
    notes_root = os.path.join(udir, "notes")
    os.makedirs(notes_root, exist_ok=True)
    if os.path.exists(notes_root):
        for d in os.listdir(notes_root):
            if note_id in d:
                return os.path.join(notes_root, d)
    date_str = time.strftime("%Y%m%d")
    safe_title = _safe_name(title) if title else ""
    dirname = f"{safe_title}_{note_id}_{date_str}" if safe_title else f"{note_id}_{date_str}"
    d = os.path.join(notes_root, dirname)
    os.makedirs(d, exist_ok=True)
    return d


def _save_json(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _load_json(path: str):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _find_note_json(note_id: str) -> dict | None:
    users_dir = os.path.join(DATA_DIR, "users")
    if not os.path.exists(users_dir):
        return None
    for udir in os.listdir(users_dir):
        notes_dir = os.path.join(users_dir, udir, "notes")
        if not os.path.exists(notes_dir):
            continue
        for ndir in os.listdir(notes_dir):
            if note_id in ndir:
                path = os.path.join(notes_dir, ndir, "note.json")
                if os.path.exists(path):
                    return _load_json(path)
    return None


def _is_scraped(note_id: str) -> bool:
    """检查笔记是否已抓过（去重）"""
    return _find_note_json(note_id) is not None


# ============================================================
# 内部复用函数（原子操作，不暴露为 MCP 工具）
# ============================================================

def _do_search(keyword: str, page: int = 1, sort: str = "general",
               note_type: int = 0) -> list[dict]:
    """搜索，返回结构化列表"""
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
    data = {
        "keyword": keyword, "page": page, "page_size": 20,
        "search_id": f"{int(time.time()*1000)}{random.randint(100000,999999)}",
        "sort": sort, "note_type": note_type,
        "image_formats": ["jpg", "webp", "avif"],
    }
    result = proxy_request(url, method="POST", data=data)
    if "error" in result:
        return []
    items = result.get("data", {}).get("items", [])
    notes = []
    for item in items:
        nc = item.get("note_card", {})
        user = nc.get("user", {})
        interact = nc.get("interact_info", {})
        notes.append({
            "note_id": nc.get("note_id", ""),
            "title": nc.get("display_title", ""),
            "author": user.get("nickname", ""),
            "user_id": user.get("user_id", ""),
            "liked": interact.get("liked_count", "0"),
            "collected": interact.get("collected_count", "0"),
            "xsec_token": item.get("xsec_token", nc.get("xsec_token", "")),
        })
    return notes


def _do_get_detail(note_id: str, xsec_token: str = "") -> dict | None:
    """获取笔记详情，返回结构化 dict 或 None"""
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
    data = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
    }
    if xsec_token:
        data["xsec_token"] = xsec_token

    result = proxy_request(url, method="POST", data=data)
    if "error" in result:
        return None
    items = result.get("data", {}).get("items", [])
    if not items:
        return None

    note = items[0].get("note_card", {})
    user = note.get("user", {})
    interact = note.get("interact_info", {})

    img_urls = []
    for img in note.get("image_list", []):
        url_val = ""
        for info in img.get("info_list", []):
            if info.get("url"):
                url_val = info["url"]
                break
        if not url_val:
            url_val = img.get("url_default", "")
        if url_val:
            img_urls.append(url_val)

    return {
        "note_id": note_id,
        "title": note.get("title", ""),
        "author": user.get("nickname", ""),
        "user_id": user.get("user_id", "unknown"),
        "desc": note.get("desc", ""),
        "liked": interact.get("liked_count", "0"),
        "collected": interact.get("collected_count", "0"),
        "comments_count": interact.get("comment_count", "0"),
        "tags": [t.get("name", "") for t in note.get("tag_list", [])],
        "image_urls": img_urls,
        "xsec_token": xsec_token,
    }


def _do_download_images(note_data: dict) -> tuple[int, int]:
    """下载图片，返回 (成功数, 失败数)"""
    img_urls = note_data.get("image_urls", [])
    if not img_urls:
        return 0, 0
    ndir = _note_dir(note_data["user_id"], note_data["note_id"],
                     note_data.get("author", ""), note_data.get("title", ""))
    img_dir = os.path.join(ndir, "images")
    os.makedirs(img_dir, exist_ok=True)

    ok, fail = 0, 0
    for i, img_url in enumerate(img_urls):
        filepath = os.path.join(img_dir, f"{i:02d}.jpg")
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            ok += 1  # 已下载过，跳过
            continue
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(filepath, "wb") as f:
                    f.write(resp.read())
            ok += 1
            _stats["images_downloaded"] += 1
        except Exception:
            fail += 1
    return ok, fail


def _do_save_note(note_data: dict):
    """持久化笔记数据"""
    ndir = _note_dir(note_data["user_id"], note_data["note_id"],
                     note_data.get("author", ""), note_data.get("title", ""))
    _save_json(os.path.join(ndir, "note.json"), note_data)
    _stats["notes_scraped"] += 1


# ============================================================
# MCP 工具 — 原子操作（保留，方便单条调用）
# ============================================================

mcp = FastMCP("xhs-scraper")


@mcp.tool()
def check_status() -> str:
    """检查社媒助手连接状态。"""
    try:
        req = urllib.request.Request(f"{COPILOT_SERVER}/", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return f"✅ 社媒助手已连接 ({COPILOT_SERVER})"
    except Exception as e:
        return f"❌ 无法连接 ({COPILOT_SERVER}): {e}"


@mcp.tool()
def get_stats() -> str:
    """查看抓取统计数据（请求数、成功率、失败数等）。"""
    s = _stats
    elapsed = time.time() - s["start_time"] if s["start_time"] else 0
    rate = s["success"] / s["requests"] * 100 if s["requests"] > 0 else 0
    return (
        f"运行时间: {elapsed:.0f}s\n"
        f"总请求: {s['requests']}  成功: {s['success']}  失败: {s['failed']}  重试: {s['retries']}\n"
        f"成功率: {rate:.1f}%\n"
        f"笔记已抓: {s['notes_scraped']}  图片已下载: {s['images_downloaded']}\n"
        f"当前连续失败: {s['consecutive_failures']}"
    )


@mcp.tool()
def search_notes(keyword: str, page: int = 1, sort: str = "general",
                 note_type: int = 0) -> str:
    """搜索小红书笔记。

    Args:
        keyword: 搜索关键词
        page: 页码，从1开始
        sort: general(综合) / time_descending(最新) / popularity_descending(最热)
        note_type: 0=全部 1=视频 2=图文
    """
    notes = _do_search(keyword, page, sort, note_type)
    if not notes:
        return f"未找到'{keyword}'相关笔记"

    # 持久化搜索快照
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_kw = _safe_name(keyword, 20)
    snap_dir = os.path.join(DATA_DIR, "search")
    os.makedirs(snap_dir, exist_ok=True)
    _save_json(os.path.join(snap_dir, f"{safe_kw}_p{page}_{ts}.json"), notes)

    lines = [f"搜索「{keyword}」第{page}页，共{len(notes)}条\n"]
    for i, n in enumerate(notes, 1):
        scraped = " [已抓]" if _is_scraped(n["note_id"]) else ""
        lines.append(f"{i}. {n['title']}{scraped}")
        lines.append(f"   作者: {n['author']}  👍{n['liked']}  ⭐{n['collected']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def get_user_notes(user_id: str, max_pages: int = 3) -> str:
    """获取博主的笔记列表。

    Args:
        user_id: 用户ID
        max_pages: 最多翻几页（每页30条）
    """
    user_url = f"https://edith.xiaohongshu.com/api/sns/web/v1/user/otherinfo?target_user_id={user_id}"
    user_info = proxy_request(user_url)
    basic = user_info.get("data", {}).get("basic_info", {})
    nickname = basic.get("nickname", "未知")

    profile = {
        "user_id": user_id, "nickname": nickname,
        "fans": basic.get("fans", ""), "liked": basic.get("liked", ""),
        "desc": basic.get("desc", ""),
    }
    _save_json(os.path.join(_user_dir(user_id, nickname), "profile.json"), profile)

    all_notes = []
    cursor = ""
    for _ in range(max_pages):
        url = (f"https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
               f"?num=30&cursor={cursor}&user_id={user_id}&image_formats=jpg,webp,avif")
        result = proxy_request(url)
        if "error" in result:
            break
        notes = result.get("data", {}).get("notes", [])
        if not notes:
            break
        for n in notes:
            interact = n.get("interact_info", {})
            all_notes.append({
                "note_id": n.get("note_id", ""),
                "title": n.get("display_title", ""),
                "type": "视频" if n.get("type") == "video" else "图文",
                "liked": interact.get("liked_count", "0"),
                "xsec_token": n.get("xsec_token", ""),
            })
        cursor = result.get("data", {}).get("cursor", "")
        if not result.get("data", {}).get("has_more", False):
            break

    udir = _user_dir(user_id, nickname)
    _save_json(os.path.join(udir, "notes_list.json"), all_notes)

    lines = [f"{nickname} 共{len(all_notes)}篇笔记\n"]
    for i, n in enumerate(all_notes, 1):
        scraped = " [已抓]" if _is_scraped(n["note_id"]) else ""
        lines.append(f"{i}. [{n['type']}] {n['title']}  👍{n['liked']}{scraped}")
    lines.append(f"\n已保存 → data/users/{os.path.basename(udir)}/")
    return "\n".join(lines)


@mcp.tool()
def get_note_detail(note_id: str, xsec_token: str = "") -> str:
    """获取单篇笔记完整内容。

    Args:
        note_id: 笔记ID
        xsec_token: 安全token
    """
    note_data = _do_get_detail(note_id, xsec_token)
    if not note_data:
        return f"获取失败: {note_id}"
    _do_save_note(note_data)

    title = note_data["title"] or "(无标题)"
    lines = [
        f"# {title}",
        f"作者: {note_data['author']}  👍{note_data['liked']}  ⭐{note_data['collected']}  💬{note_data['comments_count']}",
        "", note_data["desc"] or "(无正文)",
    ]
    if note_data["tags"]:
        lines.append(f"\n标签: {' '.join('#'+t for t in note_data['tags'])}")
    if note_data["image_urls"]:
        lines.append(f"\n共{len(note_data['image_urls'])}张图片 → download_images(\"{note_id}\")")
    return "\n".join(lines)


@mcp.tool()
def get_note_comments(note_id: str, xsec_token: str = "", max_pages: int = 2) -> str:
    """获取笔记评论。

    Args:
        note_id: 笔记ID
        xsec_token: 安全token
        max_pages: 最多几页
    """
    all_comments = []
    cursor = ""
    for _ in range(max_pages):
        xsec_part = f"&xsec_token={xsec_token}" if xsec_token else ""
        url = (f"https://edith.xiaohongshu.com/api/sns/web/v2/comment/page"
               f"?note_id={note_id}&cursor={cursor}&image_formats=jpg,webp,avif{xsec_part}")
        result = proxy_request(url)
        if "error" in result:
            return f"获取评论失败: {result['error']}"
        comments = result.get("data", {}).get("comments", [])
        if not comments:
            break
        all_comments.extend(comments)
        cursor = result.get("data", {}).get("cursor", "")
        if not result.get("data", {}).get("has_more", False):
            break

    if not all_comments:
        return f"笔记 {note_id} 暂无评论"

    structured = []
    for c in all_comments:
        u = c.get("user_info", {})
        entry = {"user": u.get("nickname", ""), "content": c.get("content", ""),
                 "likes": c.get("like_count", 0), "replies": []}
        for sub in c.get("sub_comments", []):
            su = sub.get("user_info", {})
            entry["replies"].append({"user": su.get("nickname", ""),
                                     "content": sub.get("content", ""), "likes": sub.get("like_count", 0)})
        structured.append(entry)

    note_json = _find_note_json(note_id)
    ndir = None
    if note_json:
        ndir = _note_dir(note_json["user_id"], note_id,
                         note_json.get("author", ""), note_json.get("title", ""))
        _save_json(os.path.join(ndir, "comments.json"), structured)

    lines = [f"共{len(structured)}条评论\n"]
    for i, c in enumerate(structured, 1):
        like_str = f" (👍{c['likes']})" if c["likes"] else ""
        lines.append(f"{i}. {c['user']}{like_str}: {c['content']}")
        for r in c["replies"][:3]:
            lines.append(f"   ↳ {r['user']}: {r['content']}")
    return "\n".join(lines)


@mcp.tool()
def download_images(note_id: str) -> str:
    """下载笔记图片。需要先调用 get_note_detail。

    Args:
        note_id: 笔记ID
    """
    note_json = _find_note_json(note_id)
    if not note_json:
        return f"未找到笔记 {note_id} 的数据，请先 get_note_detail"
    ok, fail = _do_download_images(note_json)
    ndir = _note_dir(note_json["user_id"], note_id,
                     note_json.get("author", ""), note_json.get("title", ""))
    ndir_rel = os.path.relpath(ndir, DATA_DIR)
    return f"下载完成: {ok}成功 {fail}失败 → data/{ndir_rel}/images/"


# ============================================================
# MCP 工具 — 批量抓取（高级编排）
# ============================================================

@mcp.tool()
def scrape_by_keyword(keyword: str, max_pages: int = 2,
                      sort: str = "popularity_descending",
                      download_imgs: bool = True,
                      min_likes: int = 0) -> str:
    """按关键词批量抓取笔记（搜索→详情→图片），内置延时防封和去重。

    Args:
        keyword: 搜索关键词，如"大厂agent面试"
        max_pages: 搜索几页（每页20条）
        sort: 排序 - popularity_descending(最热) / time_descending(最新) / general(综合)
        download_imgs: 是否同时下载图片
        min_likes: 最低点赞数过滤（0=不过滤）
    """
    _reset_stats()
    all_search = []
    lines = [f"开始抓取「{keyword}」，共{max_pages}页\n"]

    # 阶段1：搜索
    for page in range(1, max_pages + 1):
        notes = _do_search(keyword, page, sort)
        if not notes:
            lines.append(f"第{page}页：无结果，停止翻页")
            break
        all_search.extend(notes)
        lines.append(f"第{page}页：{len(notes)}条")
        if page < max_pages:
            _smart_sleep()

    if not all_search:
        return "搜索无结果"

    # 过滤：去重 + 点赞数
    to_scrape = []
    skipped_dup, skipped_likes = 0, 0
    for n in all_search:
        if not n["note_id"]:
            continue
        if _is_scraped(n["note_id"]):
            skipped_dup += 1
            continue
        try:
            likes = int(str(n.get("liked", "0")).replace(",", ""))
        except ValueError:
            likes = 0
        if min_likes > 0 and likes < min_likes:
            skipped_likes += 1
            continue
        to_scrape.append(n)

    lines.append(f"\n搜索到{len(all_search)}条，去重跳过{skipped_dup}，点赞过滤{skipped_likes}，待抓取{len(to_scrape)}条\n")

    # 阶段2：逐条抓详情+图片
    ok, fail = 0, 0
    for i, n in enumerate(to_scrape, 1):
        _smart_sleep()
        note_data = _do_get_detail(n["note_id"], n.get("xsec_token", ""))
        if not note_data:
            fail += 1
            lines.append(f"  [{i}/{len(to_scrape)}] ❌ {n['title'][:30]}")
            continue

        _do_save_note(note_data)
        ok += 1
        img_info = ""
        if download_imgs and note_data["image_urls"]:
            img_ok, img_fail = _do_download_images(note_data)
            img_info = f" 🖼{img_ok}/{len(note_data['image_urls'])}"

        lines.append(f"  [{i}/{len(to_scrape)}] ✅ {note_data['title'][:30]}  👍{note_data['liked']}{img_info}")

        # 连续失败过多，主动暂停
        if _stats["consecutive_failures"] >= 5:
            lines.append(f"\n⚠️ 连续失败{_stats['consecutive_failures']}次，暂停30s...")
            time.sleep(30)
            _stats["consecutive_failures"] = 0

    # 汇总
    elapsed = time.time() - _stats["start_time"]
    lines.append(f"\n{'='*40}")
    lines.append(f"完成: {ok}成功 {fail}失败 / {len(to_scrape)}条")
    lines.append(f"耗时: {elapsed:.0f}s | 请求: {_stats['requests']} | 成功率: {_stats['success']/_stats['requests']*100:.0f}%")
    lines.append(f"图片: {_stats['images_downloaded']}张 | 重试: {_stats['retries']}次")
    lines.append(f"\n数据目录: data/users/")

    # 保存搜索快照
    ts = time.strftime("%Y%m%d_%H%M%S")
    snap_dir = os.path.join(DATA_DIR, "search")
    os.makedirs(snap_dir, exist_ok=True)
    _save_json(os.path.join(snap_dir, f"{_safe_name(keyword, 20)}_batch_{ts}.json"), all_search)

    return "\n".join(lines)


@mcp.tool()
def scrape_by_user(user_id: str, max_notes: int = 30,
                   download_imgs: bool = True) -> str:
    """按博主批量抓取笔记（列表→详情→图片），内置延时防封和去重。

    Args:
        user_id: 用户ID
        max_notes: 最多抓几篇（0=全部）
        download_imgs: 是否同时下载图片
    """
    _reset_stats()

    # 获取用户信息
    user_url = f"https://edith.xiaohongshu.com/api/sns/web/v1/user/otherinfo?target_user_id={user_id}"
    user_info = proxy_request(user_url)
    basic = user_info.get("data", {}).get("basic_info", {})
    nickname = basic.get("nickname", "未知")

    profile = {"user_id": user_id, "nickname": nickname,
               "fans": basic.get("fans", ""), "liked": basic.get("liked", ""),
               "desc": basic.get("desc", "")}
    _save_json(os.path.join(_user_dir(user_id, nickname), "profile.json"), profile)

    # 拉笔记列表
    all_notes = []
    cursor = ""
    max_pages = (max_notes // 30 + 1) if max_notes > 0 else 10
    for _ in range(max_pages):
        _smart_sleep()
        url = (f"https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
               f"?num=30&cursor={cursor}&user_id={user_id}&image_formats=jpg,webp,avif")
        result = proxy_request(url)
        if "error" in result:
            break
        notes = result.get("data", {}).get("notes", [])
        if not notes:
            break
        for n in notes:
            interact = n.get("interact_info", {})
            all_notes.append({
                "note_id": n.get("note_id", ""),
                "title": n.get("display_title", ""),
                "type": "视频" if n.get("type") == "video" else "图文",
                "liked": interact.get("liked_count", "0"),
                "xsec_token": n.get("xsec_token", ""),
            })
        cursor = result.get("data", {}).get("cursor", "")
        if not result.get("data", {}).get("has_more", False):
            break

    _save_json(os.path.join(_user_dir(user_id, nickname), "notes_list.json"), all_notes)

    # 过滤去重
    to_scrape = [n for n in all_notes if n["note_id"] and not _is_scraped(n["note_id"])]
    if max_notes > 0:
        to_scrape = to_scrape[:max_notes]

    lines = [f"{nickname} 共{len(all_notes)}篇，去重后待抓{len(to_scrape)}篇\n"]

    # 逐条抓
    ok, fail = 0, 0
    for i, n in enumerate(to_scrape, 1):
        _smart_sleep()
        note_data = _do_get_detail(n["note_id"], n.get("xsec_token", ""))
        if not note_data:
            fail += 1
            lines.append(f"  [{i}/{len(to_scrape)}] ❌ {n['title'][:30]}")
            continue

        _do_save_note(note_data)
        ok += 1
        img_info = ""
        if download_imgs and note_data["image_urls"]:
            img_ok, img_fail = _do_download_images(note_data)
            img_info = f" 🖼{img_ok}/{len(note_data['image_urls'])}"

        lines.append(f"  [{i}/{len(to_scrape)}] ✅ {note_data['title'][:30]}  👍{note_data['liked']}{img_info}")

        if _stats["consecutive_failures"] >= 5:
            lines.append(f"\n⚠️ 连续失败{_stats['consecutive_failures']}次，暂停30s...")
            time.sleep(30)
            _stats["consecutive_failures"] = 0

    elapsed = time.time() - _stats["start_time"]
    lines.append(f"\n{'='*40}")
    lines.append(f"完成: {ok}成功 {fail}失败 / {len(to_scrape)}条")
    lines.append(f"耗时: {elapsed:.0f}s | 请求: {_stats['requests']} | 成功率: {_stats['success']/_stats['requests']*100:.0f}%")
    lines.append(f"图片: {_stats['images_downloaded']}张")
    return "\n".join(lines)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run()
