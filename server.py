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
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# ============================================================
# 代理请求（通过社媒助手 Server 调用小红书 API）
# ============================================================

def proxy_request(url: str, method: str = "GET", data: dict | None = None) -> dict:
    """通过社媒助手 Server 的 /request 端点代理请求"""
    payload = {"url": url, "method": method}
    if data:
        payload["data"] = data

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{COPILOT_SERVER}/request",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 412:
            return {"error": "Chrome插件未连接。请确保：1)Chrome已打开小红书页面 2)开源版社媒助手插件已打开并显示'连接成功'"}
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


def _gen_search_id() -> str:
    import time, random
    return f"{int(time.time() * 1000)}{random.randint(100000, 999999)}"


def _to_int(val) -> int:
    try:
        return int(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0


# ============================================================
# MCP Server
# ============================================================

mcp = FastMCP("xhs-scraper")


@mcp.tool()
def check_status() -> str:
    """检查社媒助手连接状态。使用前先调用此工具。"""
    try:
        req = urllib.request.Request(f"{COPILOT_SERVER}/", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        return f"✅ 社媒助手 Server 已连接 ({COPILOT_SERVER})\n请确保 Chrome 开源版插件显示'连接成功'。"
    except Exception as e:
        return (
            f"❌ 无法连接 ({COPILOT_SERVER}): {e}\n\n"
            "请检查：\n"
            "1. 社媒助手 Node Server 是否已启动（开机自启或手动运行 start_copilot_server.bat）\n"
            "2. Chrome 是否已打开小红书页面\n"
            "3. 开源版社媒助手插件是否已打开并连接"
        )


@mcp.tool()
def search_notes(keyword: str, page: int = 1, sort: str = "general",
                 note_type: int = 0) -> str:
    """搜索小红书笔记。

    Args:
        keyword: 搜索关键词，如"Agent面试"、"大模型面经"
        page: 页码，从1开始
        sort: 排序 - general(综合), time_descending(最新), popularity_descending(最热)
        note_type: 0(全部), 1(视频), 2(图文)
    """
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
    data = {
        "keyword": keyword,
        "page": page,
        "page_size": 20,
        "search_id": _gen_search_id(),
        "sort": sort,
        "note_type": note_type,
        "image_formats": ["jpg", "webp", "avif"],
    }
    result = proxy_request(url, method="POST", data=data)
    if "error" in result:
        return f"搜索失败: {result['error']}"

    items = result.get("data", {}).get("items", [])
    if not items:
        return f"未找到'{keyword}'相关笔记"

    output = [f"## 搜索'{keyword}'结果（第{page}页，{len(items)}条）\n"]
    for i, item in enumerate(items, 1):
        nc = item.get("note_card", {})
        user = nc.get("user", {})
        interact = nc.get("interact_info", {})
        output.append(
            f"**{i}. {nc.get('display_title', '无标题')}**\n"
            f"  ID: `{nc.get('note_id', '')}` | 作者: {user.get('nickname', '')} (`{user.get('user_id', '')}`)\n"
            f"  👍{interact.get('liked_count', '0')} ⭐{interact.get('collected_count', '0')}\n"
        )
    output.append(f"\n> `get_note_detail(note_id)` 获取完整内容")
    output.append(f"> `get_user_notes(user_id)` 获取该作者全部笔记")
    return "\n".join(output)


@mcp.tool()
def get_note_detail(note_id: str, xsec_token: str = "") -> str:
    """获取笔记完整内容（文字+图片链接+标签）。

    Args:
        note_id: 笔记ID
        xsec_token: 安全token（可选）
    """
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
        return f"获取失败: {result['error']}"

    items = result.get("data", {}).get("items", [])
    if not items:
        return f"未找到笔记 {note_id}"

    note = items[0].get("note_card", {})
    user = note.get("user", {})
    interact = note.get("interact_info", {})

    output = [
        f"# {note.get('title', '无标题')}\n",
        f"**作者**: {user.get('nickname', '')} | "
        f"👍{interact.get('liked_count', '0')} ⭐{interact.get('collected_count', '0')} 💬{interact.get('comment_count', '0')}\n",
        f"\n## 正文\n\n{note.get('desc', '无内容')}\n",
    ]

    tags = note.get("tag_list", [])
    if tags:
        output.append(f"\n**标签**: {' '.join('#' + t.get('name', '') for t in tags)}\n")

    images = note.get("image_list", [])
    if images:
        output.append(f"\n## 图片（{len(images)}张）\n")
        img_urls = []
        for j, img in enumerate(images):
            url_val = ""
            for info in img.get("info_list", []):
                if info.get("url"):
                    url_val = info["url"]
                    break
            if not url_val:
                url_val = img.get("url_default", "")
            img_urls.append(url_val)
            output.append(f"- [{j+1}] {url_val}")

        # 保存图片链接到本地（方便后续下载）
        _save_note_cache(note_id, note, img_urls)
        output.append(f"\n> `download_images('{note_id}')` 下载全部图片")

    return "\n".join(output)


@mcp.tool()
def get_user_notes(user_id: str, max_pages: int = 3) -> str:
    """获取博主发布的全部笔记列表。

    Args:
        user_id: 用户ID
        max_pages: 最多几页（每页30条），默认3页
    """
    # 先获取用户信息
    user_url = f"https://edith.xiaohongshu.com/api/sns/web/v1/user/otherinfo?target_user_id={user_id}"
    user_info = proxy_request(user_url)
    basic = user_info.get("data", {}).get("basic_info", {})

    output = [
        f"# {basic.get('nickname', '未知')} 的笔记\n",
        f"粉丝: {basic.get('fans', '?')} | 获赞: {basic.get('liked', '?')}\n---\n",
    ]

    all_notes = []
    cursor = ""
    for _ in range(max_pages):
        url = f"https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?num=30&cursor={cursor}&user_id={user_id}&image_formats=jpg,webp,avif"
        result = proxy_request(url)
        if "error" in result:
            output.append(f"获取失败: {result['error']}")
            break
        notes = result.get("data", {}).get("notes", [])
        if not notes:
            break
        all_notes.extend(notes)
        cursor = result.get("data", {}).get("cursor", "")
        if not result.get("data", {}).get("has_more", False):
            break

    output.append(f"共 {len(all_notes)} 篇：\n")
    for i, n in enumerate(all_notes, 1):
        interact = n.get("interact_info", {})
        output.append(
            f"**{i}. {n.get('display_title', '无标题')}**\n"
            f"  ID: `{n.get('note_id', '')}` | 👍{interact.get('liked_count', '0')} | "
            f"{'视频' if n.get('type') == 'video' else '图文'}\n"
        )
    return "\n".join(output)


@mcp.tool()
def get_note_comments(note_id: str, max_pages: int = 2) -> str:
    """获取笔记评论。

    Args:
        note_id: 笔记ID
        max_pages: 最多几页
    """
    all_comments = []
    cursor = ""
    for _ in range(max_pages):
        url = f"https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id={note_id}&cursor={cursor}&image_formats=jpg,webp,avif"
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

    output = [f"## 评论（{len(all_comments)}条）\n"]
    for i, c in enumerate(all_comments, 1):
        user = c.get("user_info", {})
        output.append(
            f"**{i}. {user.get('nickname', '匿名')}** (👍{c.get('like_count', 0)})\n"
            f"  {c.get('content', '')}\n"
        )
        for sub in c.get("sub_comments", [])[:3]:
            su = sub.get("user_info", {})
            output.append(f"  ↳ **{su.get('nickname', '')}**: {sub.get('content', '')}\n")
    return "\n".join(output)


@mcp.tool()
def download_images(note_id: str) -> str:
    """下载已获取笔记的全部图片到本地。需要先调用 get_note_detail 获取图片链接。

    Args:
        note_id: 笔记ID
    """
    cache = _load_note_cache(note_id)
    if not cache:
        return f"未找到笔记 {note_id} 的缓存。请先调用 get_note_detail('{note_id}') 获取笔记内容。"

    img_urls = cache.get("image_urls", [])
    if not img_urls:
        return "该笔记没有图片"

    title = cache.get("title", note_id)[:30]
    title_safe = "".join(c for c in title if c not in r'\/:*?"<>|')
    save_dir = os.path.join(IMAGES_DIR, f"{note_id}_{title_safe}")
    os.makedirs(save_dir, exist_ok=True)

    results = []
    for i, url in enumerate(img_urls):
        filepath = os.path.join(save_dir, f"img_{i:02d}.jpg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(filepath, "wb") as f:
                    f.write(resp.read())
            results.append(f"✅ img_{i:02d}.jpg ({os.path.getsize(filepath)//1024}KB)")
        except Exception as e:
            results.append(f"❌ img_{i:02d}.jpg: {e}")

    return f"保存到: {save_dir}\n成功: {sum(1 for r in results if '✅' in r)}/{len(img_urls)}\n\n" + "\n".join(results)


# ============================================================
# 本地缓存（保存笔记图片链接，供 download_images 使用）
# ============================================================

def _save_note_cache(note_id: str, note: dict, img_urls: list):
    cache_file = os.path.join(DATA_DIR, f"{note_id}.json")
    cache = {
        "note_id": note_id,
        "title": note.get("title", ""),
        "image_urls": img_urls,
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _load_note_cache(note_id: str) -> dict | None:
    cache_file = os.path.join(DATA_DIR, f"{note_id}.json")
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run()
