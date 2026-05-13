#!/usr/bin/env python3
"""
局域网文件传输服务 — 单文件、零依赖
用法:
    python file_transfer.py            # 默认端口 8888
    python file_transfer.py 9999       # 指定端口
"""

import os
import sys
import json
import time
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

# ---- 兼容 ThreadingHTTPServer (Python 3.6 兼容) ----
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

# ---- 配置 ----
UPLOAD_DIR = Path("./uploads")
BUFFER_SIZE = 64 * 1024  # 64KB

# ---- 消息存储 (内存) ----
_MESSAGES = []
_MSG_ID = 0
_MAX_MESSAGES = 200


# ============================================================
# 网络工具
# ============================================================

def get_local_ips():
    """获取本机所有局域网 IP"""
    ips = []
    try:
        # 方法 1: 通过 UDP socket 获取首选 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.254.254.254", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    # 方法 2: 枚举所有非回环 IPv4 地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    if not ips:
        ips.append("127.0.0.1")
    return ips


def get_server_addr(request):
    """从 socket 获取服务器实际绑定的地址"""
    host, port = request.getsockname()
    return f"{host}:{port}"


# ============================================================
# 文件工具
# ============================================================

def safe_filename(name: str) -> str:
    """防路径穿越，只保留文件名"""
    result = os.path.basename(name)
    if not result or result in (".", ".."):
        raise ValueError(f"无效的文件名: {name!r}")
    return result


def format_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def format_mtime(ts: float) -> str:
    now = time.time()
    diff = now - ts
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)}分钟前"
    dt = time.localtime(ts)
    now_dt = time.localtime(now)
    if dt.tm_year == now_dt.tm_year and dt.tm_mon == now_dt.tm_mon and dt.tm_mday == now_dt.tm_mday:
        return f"今天 {dt.tm_hour:02d}:{dt.tm_min:02d}"
    yesterday = time.localtime(now - 86400)
    if dt.tm_year == yesterday.tm_year and dt.tm_mon == yesterday.tm_mon and dt.tm_mday == yesterday.tm_mday:
        return "昨天"
    return f"{dt.tm_year}/{dt.tm_mon:02d}/{dt.tm_mday:02d}"


# ============================================================
# 二维码生成 (可选依赖)
# ============================================================

_QRCODE_SVG = None
_QRCODE_AVAILABLE = False

try:
    import qrcode
    import qrcode.image.svg
    _QRCODE_AVAILABLE = True
except ImportError:
    pass


def generate_qrcode_svg(url: str) -> Optional[str]:
    """生成二维码 SVG 字符串，qrcode 未安装时返回 None"""
    global _QRCODE_SVG, _QRCODE_AVAILABLE
    if not _QRCODE_AVAILABLE:
        return None
    if _QRCODE_SVG is None:
        # SvgPathImage: path 元素无命名空间前缀，HTML5 内联 SVG 兼容
        qr = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        svg = qr.to_string().decode()
        # 固定 mm 尺寸改为百分比，让 SVG 自适应容器
        svg = svg.replace('width="33mm"', 'width="100%"', 1)
        svg = svg.replace('height="33mm"', 'height="100%"', 1)
        _QRCODE_SVG = svg
    return _QRCODE_SVG


# ============================================================
# Multipart 解析器
# ============================================================

def parse_multipart(body: bytes, boundary: str) -> list:
    """
    解析 multipart/form-data 请求体。
    使用位置查找而非字节分割，避免二进制内容与 boundary 冲突。

    返回: [{"name": str, "filename": str|None, "content_type": str, "body": bytes}, ...]
    """
    boundary_bytes = b"--" + boundary.encode()
    positions = []
    pos = 0

    while True:
        pos = body.find(boundary_bytes, pos)
        if pos == -1:
            break
        positions.append(pos)
        pos += len(boundary_bytes)

    parts = []
    for i in range(len(positions) - 1):
        start = positions[i] + len(boundary_bytes)
        if start + 1 < len(body) and body[start:start + 2] == b"\r\n":
            start += 2
        end = positions[i + 1]
        if end >= 2 and body[end - 2:end] == b"\r\n":
            end -= 2

        chunk = body[start:end]
        if not chunk:
            continue

        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            continue

        header_text = chunk[:header_end].decode("utf-8", errors="replace")
        part_body = chunk[header_end + 4:]

        headers = {}
        for line in header_text.split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        disp = headers.get("content-disposition", "")
        field_name = None
        filename = None
        for item in disp.split(";"):
            item = item.strip()
            if item.startswith("name="):
                field_name = item[5:].strip().strip('"')
            elif item.startswith("filename="):
                filename = item[9:].strip().strip('"')

        parts.append({
            "name": field_name,
            "filename": filename,
            "content_type": headers.get("content-type", "application/octet-stream"),
            "body": part_body,
        })

    return parts


# ============================================================
# HTML 页面 (内嵌模板)
# ============================================================

def build_html(server_addr: str, qrcode_svg: Optional[str] = None) -> str:
    """生成 HTML 页面：聊天布局 + 文件面板"""
    qr_section = ""
    if qrcode_svg:
        qr_section = f"""<div class="qr-code" id="qrCode" title="点击放大二维码">{qrcode_svg}</div>"""
    else:
        qr_section = '<div class="qr-code no-qr" id="qrCode" title="安装 qrcode 库以启用二维码">◇</div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>局域网文件传输</title>
<style>
    :root {{
        --bg: #f0f2f5;
        --card: #fff;
        --text: #1a1a2e;
        --muted: #888;
        --border: #e8e8e8;
        --primary: #4361ee;
        --primary-dim: #eef0ff;
        --danger: #e63946;
        --radius: 12px;
        --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
    }}
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{ height: 100%; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
        background: var(--bg); color: var(--text);
        display: flex; justify-content: center; padding: 16px;
    }}
    .container {{
        width: 100%; max-width: 680px; display: flex; flex-direction: column; height: 100%;
    }}

    /* 工具栏 */
    .toolbar {{
        display: flex; align-items: center; gap: 8px;
        padding: 6px 12px; background: var(--card);
        border-radius: var(--radius); box-shadow: var(--shadow);
        flex-shrink: 0; margin-bottom: 8px;
    }}
    .tb-left {{
        display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1;
    }}
    .tb-info {{
        display: flex; flex-direction: column; gap: 1px; min-width: 0;
    }}
    .tb-title {{
        font-size: 14px; font-weight: 700; white-space: nowrap;
    }}
    .tb-addr {{
        font-size: 11px; color: var(--muted); user-select: all; white-space: nowrap;
    }}
    .tb-actions {{
        display: flex; align-items: center; gap: 6px; flex-shrink: 0;
    }}
    .tb-actions button {{
        font-size: 11px; padding: 4px 10px; border: 1px solid var(--border);
        border-radius: 6px; background: transparent; cursor: pointer; color: var(--text);
        white-space: nowrap; font-weight: 500;
    }}
    .tb-actions button:hover {{ border-color: var(--primary); color: var(--primary); }}
    .qr-code {{
        width: 38px; height: 38px; border-radius: 6px;
        background: #fff; padding: 3px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08);
        display: flex; align-items: center; justify-content: center;
        flex-shrink: 0; cursor: pointer;
    }}
    .qr-code:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
    .qr-code svg {{ width: 100%; height: 100%; display: block; }}
    .qr-code.no-qr {{
        background: var(--primary-dim); color: var(--muted);
        font-size: 18px; cursor: default;
    }}

    /* ====== 聊天面板 ====== */
    #panel-chat {{
        flex: 1; display: flex; flex-direction: column; min-height: 0;
        background: var(--card); border-radius: var(--radius); box-shadow: var(--shadow);
        overflow: hidden; position: relative;
    }}
    .chat-list {{
        flex: 1; overflow-y: auto; padding: 8px 14px;
        display: flex; flex-direction: column; gap: 4px;
    }}
    .empty {{ text-align: center; color: var(--muted); font-size: 14px; padding: 40px 0; }}

    /* 文字消息 */
    .msg-row {{
        display: flex; flex-direction: column; gap: 4px; padding: 6px 0;
    }}
    .msg-bubble {{
        background: #f0f2f5; border-radius: 12px 12px 12px 4px;
        padding: 10px 14px; font-size: 14px; line-height: 1.55;
        word-break: break-word; white-space: pre-wrap;
        max-width: 85%; align-self: flex-start;
    }}
    .msg-row-meta {{
        display: flex; align-items: center; gap: 8px; padding-left: 4px;
    }}
    .msg-time {{ font-size: 11px; color: var(--muted); }}
    .msg-row-meta button {{
        font-size: 11px; padding: 2px 8px; border: none; border-radius: 4px;
        background: transparent; cursor: pointer; color: var(--muted);
    }}
    .msg-row-meta button:hover {{ color: var(--primary); }}

    /* 文件消息 */
    .msg-file {{
        display: flex; align-items: center; gap: 10px;
        background: var(--primary-dim); border-radius: 12px;
        padding: 12px 14px; align-self: flex-start; max-width: 90%;
    }}
    .msg-file-icon {{ font-size: 28px; flex-shrink: 0; }}
    .msg-thumb {{ font-size: 0; width: 44px; height: 44px; border-radius: 8px; overflow: hidden; background: #e8e8e8; }}
    .msg-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
    .msg-file-info {{ flex: 1; min-width: 0; }}
    .msg-file-name {{
        font-size: 14px; font-weight: 500; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis;
    }}
    .msg-file-size {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
    .msg-file-actions {{ display: flex; gap: 6px; flex-shrink: 0; }}

    /* 输入栏 */
    .chat-input-row {{
        display: flex; gap: 6px; padding: 8px 10px;
        border-top: 1px solid var(--border); background: #fafafa;
        align-items: center;
    }}
    .chat-input-row textarea {{
        flex: 1; padding: 8px 12px; border: 1px solid var(--border);
        border-radius: 12px; font-size: 14px; outline: none; background: var(--card);
        resize: none; max-height: 120px; line-height: 1.45;
        font-family: inherit; min-height: 38px;
    }}
    .chat-input-row textarea:focus {{ border-color: var(--primary); }}
    .btn {{ font-size: 13px; padding: 8px 16px; border: none; border-radius: 18px;
        cursor: pointer; font-weight: 500; transition: all .15s; white-space: nowrap;
        text-decoration: none; display: inline-flex; align-items: center;
    }}
    .btn-primary {{ background: var(--primary); color: #fff; }}
    .btn-primary:hover {{ opacity: .85; }}
    .btn-icon {{
        width: 38px; height: 38px; padding: 0; border: none;
        border-radius: 50%; cursor: pointer;
        background: #f2f2f2; color: var(--text);
        font-size: 18px; display: inline-flex;
        align-items: center; justify-content: center;
        flex-shrink: 0; transition: background .15s;
    }}
    .btn-icon:hover {{ background: #e0e0e0; }}
    .btn-sm {{ font-size: 11px; padding: 5px 10px; border-radius: 6px; }}

    /* 进度条 */
    .progress {{
        padding: 8px 14px; display: none; border-top: 1px solid var(--border); background: #fafafa;
    }}
    .progress.show {{ display: block; }}
    .bar-track {{ width: 100%; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: var(--primary); border-radius: 2px; width: 0%; transition: width .2s; }}
    .bar-info {{ display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 4px; }}

    /* ====== 文件弹窗 ====== */
    .modal-overlay {{
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0,0,0,.35); z-index: 100;
        display: flex; align-items: flex-end; justify-content: center;
    }}
    .modal-overlay.hidden {{ display: none; }}
    .modal-box {{
        background: var(--bg); border-radius: var(--radius) var(--radius) 0 0;
        width: 100%; max-width: 680px; max-height: 85vh; height: 75vh;
        display: flex; flex-direction: column; animation: slideUp .25s;
    }}
    @keyframes slideUp {{ from {{ transform: translateY(100%); }} to {{ transform: translateY(0); }} }}
    .modal-header {{
        display: flex; align-items: center; justify-content: space-between;
        padding: 14px 18px; flex-shrink: 0;
    }}
    .modal-header h2 {{ font-size: 17px; font-weight: 600; }}
    .modal-close {{
        font-size: 22px; padding: 4px 8px; border: none; background: transparent;
        cursor: pointer; color: var(--muted); line-height: 1;
    }}
    .filter-tabs {{
        display: flex; gap: 8px; padding: 0 18px 12px; flex-shrink: 0;
        overflow-x: auto; -webkit-overflow-scrolling: touch;
    }}
    .filter-tabs::-webkit-scrollbar {{ display: none; }}
    .filter-tab {{
        font-size: 12px; padding: 5px 14px; border: 1px solid var(--border);
        border-radius: 16px; background: var(--card); cursor: pointer;
        color: var(--text); white-space: nowrap; flex-shrink: 0;
        font-weight: 500; user-select: none;
    }}
    .filter-tab.active {{
        background: var(--primary); color: #fff; border-color: var(--primary);
    }}

    .modal-body {{
        flex: 1; overflow-y: auto; padding: 0 10px 20px;
        display: flex; flex-direction: column;
    }}
    .modal-body .empty {{
        flex: 1; display: flex; align-items: center; justify-content: center;
        padding: 0;
    }}
    .file-row {{
        display: flex; align-items: center; padding: 12px; gap: 12px;
        background: var(--card); border-radius: 8px; margin-bottom: 6px;
        box-shadow: var(--shadow);
    }}
    .file-row {{ cursor: pointer; }}
    .file-row:hover {{ background: #fafafa; }}
    .file-icon {{
        width: 36px; height: 36px; border-radius: 8px; background: var(--primary-dim);
        display: flex; align-items: center; justify-content: center;
        font-size: 18px; flex-shrink: 0; overflow: hidden;
    }}
    .file-thumb {{ background: #e8e8e8; padding: 0; }}
    .file-thumb img {{
        width: 100%; height: 100%; object-fit: cover; border-radius: 8px;
    }}
    .file-info {{ flex: 1; min-width: 0; }}
    .file-name {{
        font-size: 14px; font-weight: 500; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis;
    }}
    .file-meta {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
    .file-actions {{ display: flex; gap: 6px; flex-shrink: 0; }}

    /* 详情卡片 */
    .detail-overlay {{
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0,0,0,.4); z-index: 200;
        display: none; align-items: flex-end; justify-content: center;
    }}
    .detail-overlay.show {{ display: flex; }}
    .detail-card {{
        background: var(--card); border-radius: 16px 16px 0 0; padding: 24px 18px 28px;
        width: 100%; max-width: 680px; text-align: center;
        box-shadow: 0 -4px 20px rgba(0,0,0,.12);
        animation: slideUp .25s ease-out;
    }}
    .detail-icon {{
        font-size: 48px; margin-bottom: 12px;
    }}
    .detail-thumb {{ font-size: 0; width: 120px; height: 120px; margin: 0 auto 12px; border-radius: 12px; overflow: hidden; background: #e8e8e8; }}
    .detail-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
    .detail-name {{
        font-size: 15px; font-weight: 600; word-break: break-all;
        line-height: 1.5; margin-bottom: 16px; color: var(--text);
    }}
    .detail-info {{
        display: flex; justify-content: center; gap: 24px;
        margin-bottom: 20px; font-size: 13px; color: var(--muted);
    }}
    .detail-info span {{ display: flex; flex-direction: column; align-items: center; gap: 2px; }}
    .detail-info .val {{ color: var(--text); font-weight: 500; }}
    .detail-actions {{
        display: flex; justify-content: center; gap: 10px;
        padding-top: 20px; border-top: 1px solid var(--border);
    }}
    .detail-actions button, .detail-actions a {{
        font-size: 12px; padding: 6px 16px; border-radius: 8px;
        cursor: pointer; text-decoration: none; border: 1px solid var(--border);
        background: var(--card); color: var(--text); font-weight: 500;
    }}
    .detail-actions a {{ background: var(--primary); color: #fff; border-color: var(--primary); }}
    .detail-actions .btn-del {{
        background: transparent; color: var(--danger); border-color: transparent;
    }}

    /* Toast */
    .toast {{
        position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
        background: #1a1a2e; color: #fff; padding: 10px 24px; border-radius: 8px;
        font-size: 14px; z-index: 999; opacity: 0; transition: opacity .3s;
        pointer-events: none;
    }}
    .toast.show {{ opacity: 1; }}

    @media (max-width: 480px) {{
        body {{ padding: 8px; }}
        .toolbar {{ padding: 8px 12px; }}
        .qr-code {{ width: 48px; height: 48px; }}
        .tb-title {{ font-size: 15px; }}
        .tb-addr {{ font-size: 12px; }}
        .tb-actions button {{ font-size: 12px; padding: 5px 12px; }}
        .chat-input-row {{ gap: 4px; padding: 6px 8px; }}
        .chat-input-row textarea {{ font-size: 13px; padding: 8px 10px; }}
        .btn {{ font-size: 12px; padding: 7px 12px; }}
        .btn-icon {{ width: 42px; height: 42px; font-size: 20px; }}
    }}

    /* 拖拽上传 */
    .drag-over {{
        outline: 2px dashed var(--primary); outline-offset: -4px;
        background: var(--primary-dim);
    }}
    .drag-hint {{
        display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(67,97,238,.08); z-index: 10; align-items: center; justify-content: center;
        border-radius: var(--radius); pointer-events: none;
    }}
    .drag-hint.show {{ display: flex; }}
    .drag-hint span {{
        font-size: 15px; color: var(--primary); font-weight: 600;
    }}
</style>
</head>
<body>

<div class="container">
    <div class="toolbar">
        <div class="tb-left">
            {qr_section}
            <div class="tb-info">
                <span class="tb-title">文件传输</span>
                <span class="tb-addr">{server_addr}</span>
            </div>
        </div>
        <div class="tb-actions">
            <button id="copyAddrBtn">复制</button>
            <button id="btnShowFiles">📁 文件</button>
        </div>
    </div>

    <!-- 聊天面板 -->
    <div id="panel-chat">
        <div class="chat-list" id="msgList">
            <div class="empty" id="msgEmpty">发送一条消息吧</div>
        </div>
        <div class="progress" id="progress">
            <div class="bar-track"><div class="bar-fill" id="barFill"></div></div>
            <div class="bar-info"><span id="barName"></span><span id="barPct">0%</span></div>
        </div>
        <div class="chat-input-row">
            <textarea id="msgInput" rows="1" placeholder="输入文字，点击发送按钮发送" maxlength="5000" autocomplete="off"></textarea>
            <button class="btn-icon" id="btnAttach" title="发送文件">📁</button>
            <button class="btn btn-primary" id="msgSend">发送</button>
        </div>
        <input type="file" id="fileInput" multiple hidden>
        <div class="drag-hint" id="dragHint"><span>释放以上传文件</span></div>
    </div>

    <!-- 文件弹窗 -->
    <div class="modal-overlay hidden" id="modalFiles">
        <div class="modal-box">
            <div class="modal-header">
                <h2>所有文件</h2>
                <button class="modal-close" id="btnCloseModal">✕</button>
            </div>
            <div class="filter-tabs" id="filterTabs">
                <span class="filter-tab" data-cat="image">🖼 图片</span>
                <span class="filter-tab" data-cat="video">🎬 视频</span>
                <span class="filter-tab" data-cat="audio">🎵 音频</span>
                <span class="filter-tab" data-cat="doc">📄 文档</span>
                <span class="filter-tab" data-cat="apk">📱 APK</span>
            </div>
            <div class="modal-body" id="fileList">
                <div class="empty" id="emptyHint">暂无文件</div>
            </div>
        </div>
    </div>
</div>

<!-- 文件详情卡片 -->
<div class="detail-overlay" id="detailOverlay">
    <div class="detail-card">
        <div class="detail-icon" id="detailIcon"></div>
        <div class="detail-name" id="detailName"></div>
        <div class="detail-info">
            <span>大小<span class="val" id="detailSize"></span></span>
            <span>时间<span class="val" id="detailTime"></span></span>
        </div>
        <div class="detail-actions">
            <button id="detailCopy">复制链接</button>
            <a id="detailDownload" download>下载</a>
            <button class="btn-del" id="detailDelete">删除</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
const serverAddr = "{server_addr}";
const $ = (s) => document.querySelector(s);

// ---- 工具 ----
function esc(s) {{
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}}

let _tid;
const toast = $("#toast");
function toastMsg(msg) {{
    clearTimeout(_tid);
    toast.textContent = msg;
    toast.classList.add("show");
    _tid = setTimeout(() => toast.classList.remove("show"), 2000);
}}

function copyText(text) {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).catch(() => {{}});
    }} else {{
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;left:-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
    }}
}}

// ---- 文件弹窗 ----
const modalFiles = $("#modalFiles");
const btnShowFiles = $("#btnShowFiles");
const btnCloseModal = $("#btnCloseModal");

function openFilesModal() {{
    modalFiles.classList.remove("hidden");
    activeFilters.clear();
    $("#filterTabs").querySelectorAll(".filter-tab").forEach(t => t.classList.remove("active"));
    setTimeout(refreshFiles, 250);
}}

function closeFilesModal() {{
    modalFiles.classList.add("hidden");
}}

btnShowFiles.addEventListener("click", openFilesModal);
btnCloseModal.addEventListener("click", closeFilesModal);
modalFiles.addEventListener("click", (e) => {{
    if (e.target === modalFiles) closeFilesModal();
}});

// ---- 二维码放大 ----
const qrCode = $("#qrCode");
if (qrCode && !qrCode.classList.contains("no-qr")) {{
    qrCode.addEventListener("click", () => {{
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:999;display:flex;align-items:center;justify-content:center;cursor:pointer;";
        const bigQr = document.createElement("div");
        bigQr.style.cssText = "background:#fff;padding:16px;border-radius:12px;width:260px;height:260px;display:flex;align-items:center;justify-content:center;";
        bigQr.innerHTML = qrCode.innerHTML;
        const svg = bigQr.querySelector("svg");
        if (svg) {{ svg.setAttribute("width", "100%"); svg.setAttribute("height", "100%"); }}
        overlay.appendChild(bigQr);
        overlay.addEventListener("click", () => overlay.remove());
        document.body.appendChild(overlay);
    }});
}}

// ---- 复制地址 ----
$("#copyAddrBtn").addEventListener("click", () => {{
    copyText(serverAddr);
    const b = $("#copyAddrBtn");
    b.textContent = "已复制";
    b.style.borderColor = "#27ae60";
    b.style.color = "#27ae60";
    setTimeout(() => {{ b.textContent = "复制"; b.style.borderColor = ""; b.style.color = ""; }}, 1500);
}});

// ============ 文件详情卡片 ============

let detailFile = null;
const detailOverlay = $("#detailOverlay");

function showDetail(f) {{
    detailFile = f;
    const detailIcon = $("#detailIcon");
    detailIcon.innerHTML = "";
    if (f.category === "image") {{
        const dImg = document.createElement("img");
        dImg.src = "/api/download/" + encodeURIComponent(f.name);
        dImg.onerror = () => {{ detailIcon.classList.remove("detail-thumb"); detailIcon.textContent = f.type_icon; }};
        detailIcon.appendChild(dImg);
        detailIcon.classList.add("detail-thumb");
    }} else {{
        detailIcon.textContent = f.type_icon;
        detailIcon.classList.remove("detail-thumb");
    }}
    $("#detailName").textContent = f.name;
    $("#detailSize").textContent = f.size_fmt;
    $("#detailTime").textContent = f.mtime_fmt;
    $("#detailDownload").href = "/api/download/" + encodeURIComponent(f.name);
    detailOverlay.classList.add("show");
}}

detailOverlay.addEventListener("click", (e) => {{
    if (e.target === detailOverlay) detailOverlay.classList.remove("show");
}});

$("#detailCopy").addEventListener("click", () => {{
    if (!detailFile) return;
    copyFileLink(detailFile.name);
    detailOverlay.classList.remove("show");
}});

$("#detailDelete").addEventListener("click", () => {{
    if (!detailFile) return;
    delFile(detailFile.name);
    detailOverlay.classList.remove("show");
}});

// 委托 file-row 点击（renderFileRow 生成的按钮用 stopPropagation 阻止冒泡）
$("#fileList").addEventListener("click", (e) => {{
    // 检查点击的是否是 file-row 或内部非按钮元素
    const row = e.target.closest(".file-row");
    if (!row) return;
    // 如果点击的是按钮或链接，不触发详情
    if (e.target.closest("button") || e.target.closest("a")) return;
    const name = row.querySelector(".file-name").textContent;
    const meta = row.querySelector(".file-meta").textContent;
    const icon = row.querySelector(".file-icon").textContent;
    const file = allFiles.find(f => f.name === name);
    if (file) showDetail(file);
}});

// ============ 文件上传 ============

const fileInput = $("#fileInput");
const progress = $("#progress");
const barFill = $("#barFill");
const barName = $("#barName");
const barPct = $("#barPct");

// ---- 拖拽上传 ----
const panelChat = $("#panel-chat");
const dragHint = $("#dragHint");
let dragCounter = 0;

panelChat.addEventListener("dragenter", (e) => {{
    e.preventDefault();
    dragCounter++;
    panelChat.classList.add("drag-over");
    dragHint.classList.add("show");
}});
panelChat.addEventListener("dragleave", (e) => {{
    dragCounter--;
    if (dragCounter <= 0) {{
        dragCounter = 0;
        panelChat.classList.remove("drag-over");
        dragHint.classList.remove("show");
    }}
}});
panelChat.addEventListener("dragover", (e) => {{ e.preventDefault(); }});
panelChat.addEventListener("drop", (e) => {{
    e.preventDefault();
    dragCounter = 0;
    panelChat.classList.remove("drag-over");
    dragHint.classList.remove("show");
    if (e.dataTransfer.files.length) {{
        for (const f of e.dataTransfer.files) uploadFile(f);
    }}
}});

// ---- 粘贴上传 ----
document.addEventListener("paste", (e) => {{
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {{
        if (item.kind === "file") {{
            const file = item.getAsFile();
            if (file) uploadFile(file);
        }}
    }}
}});

$("#btnAttach").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {{
    for (const f of fileInput.files) uploadFile(f);
    fileInput.value = "";
}});

function uploadFile(file) {{
    const fd = new FormData();
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = e => {{
        if (!e.lengthComputable) return;
        const pct = Math.round(e.loaded / e.total * 100);
        barFill.style.width = pct + "%";
        barPct.textContent = pct + "%";
    }};
    xhr.onloadstart = () => {{
        progress.classList.add("show");
        barName.textContent = file.name;
        barPct.textContent = "0%";
        barFill.style.width = "0%";
    }};
    xhr.onload = () => {{
        if (xhr.status === 200) {{
            const r = JSON.parse(xhr.responseText);
            toastMsg(r.ok ? "上传成功" : "上传失败");
        }} else {{
            toastMsg("上传失败 HTTP " + xhr.status);
        }}
        setTimeout(() => {{ progress.classList.remove("show"); pollMessages(); }}, 500);
    }};
    xhr.onerror = () => {{ progress.classList.remove("show"); toastMsg("网络错误"); }};
    xhr.send(fd);
}}

// ============ 文件列表 ============

const fileList = $("#fileList");
const emptyHint = $("#emptyHint");
let allFiles = [];
let activeFilters = new Set();

function renderFileRow(f) {{
    const dl = "/api/download/" + encodeURIComponent(f.name);

    const isImage = f.category === "image";
    const iconHtml = isImage
        ? `<img src="${{dl}}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><span style="display:none">${{f.type_icon}}</span>`
        : f.type_icon;    return `<div class="file-row">
        <div class="file-icon ${{isImage ? 'file-thumb' : ''}}">${{iconHtml}}</div>
        <div class="file-info">
            <div class="file-name" title="${{esc(f.name)}}">${{esc(f.name)}}</div>
            <div class="file-meta">${{esc(f.mtime_fmt)}} - ${{f.size_fmt}}</div>
        </div>
        <div class="file-actions">
            <button class="btn btn-sm" onclick="event.stopPropagation();copyFileLink('${{esc(f.name)}}')" style="font-size:11px;padding:5px 10px;border:1px solid #ddd;border-radius:6px;background:#f0f0f0;cursor:pointer;">复制</button>
            <a class="btn btn-sm btn-primary" href="${{dl}}" download onclick="event.stopPropagation()">下载</a>
        </div>
    </div>`;
}}

function refreshFiles() {{
    fetch("/api/files")
        .then(r => r.json())
        .then(files => {{
            allFiles = files;
            applyFilter();
        }});
}}

function applyFilter() {{
    const filtered = activeFilters.size === 0
        ? allFiles
        : allFiles.filter(f => activeFilters.has(f.category));

    if (!filtered.length) {{
        fileList.innerHTML = "";
        const msg = activeFilters.size === 0 ? "暂无文件" : "该分类暂无文件";
        emptyHint.innerHTML = `<div>${{msg}}</div><button class=\"btn btn-sm btn-primary\" onclick=\"fileInput.click()\" style=\"margin-top:12px;font-size:12px;padding:7px 20px;cursor:pointer;\">📁 上传文件</button>`;
        emptyHint.style.display = "flex";
        emptyHint.style.flexDirection = "column";
        fileList.appendChild(emptyHint);
        return;
    }}
    emptyHint.style.display = "none";
    fileList.innerHTML = filtered.map(renderFileRow).join("");
}}

// ---- 筛选标签 ----
$("#filterTabs").addEventListener("click", (e) => {{
    const tab = e.target.closest(".filter-tab");
    if (!tab) return;
    const cat = tab.dataset.cat;
    if (activeFilters.has(cat)) {{
        activeFilters.delete(cat);
        tab.classList.remove("active");
    }} else {{
        activeFilters.add(cat);
        tab.classList.add("active");
    }}
    applyFilter();
}});

function copyFileLink(name) {{
    copyText(location.protocol + "//" + serverAddr + "/api/download/" + encodeURIComponent(name));
    toastMsg("下载链接已复制");
}}

function delFile(name) {{
    if (!confirm("确定删除 " + name + "？")) return;
    fetch("/api/delete/" + encodeURIComponent(name), {{ method: "DELETE" }})
        .then(r => r.json())
        .then(r => {{ toastMsg(r.ok ? "已删除" : "失败"); refreshFiles(); }});
}}

// ============ 消息功能 ============

let lastMsgId = 0;
let pollTimer = null;

function pollMessages() {{
    fetch("/api/messages?since=" + lastMsgId)
        .then(r => r.json())
        .then(msgs => {{
            if (!msgs.length) return;
            for (const m of msgs) {{
                appendMsg(m);
                lastMsgId = Math.max(lastMsgId, m.id);
            }}
            scrollMsgList();
        }});
}}

function startPoll() {{
    if (pollTimer) return;
    pollMessages();
    pollTimer = setInterval(pollMessages, 1500);
}}

function stopPoll() {{
    clearInterval(pollTimer);
    pollTimer = null;
}}

document.addEventListener("visibilitychange", () => {{
    if (document.hidden) stopPoll();
    else startPoll();
}});

function appendMsg(m) {{
    const empty = $("#msgEmpty");
    if (empty) empty.style.display = "none";

    const list = $("#msgList");
    const row = document.createElement("div");
    row.className = "msg-row";


    if (m.type === "text") {{
        const bubble = document.createElement("div");
        bubble.className = "msg-bubble";
        bubble.textContent = m.text;
        row.appendChild(bubble);

        const meta = document.createElement("div");
        meta.className = "msg-row-meta";
        const timeEl = document.createElement("span");
        timeEl.className = "msg-time";
        timeEl.textContent = m.time;
        const copyBtn = document.createElement("button");
        copyBtn.textContent = "复制";
        copyBtn.onclick = () => {{ copyText(m.text); toastMsg("已复制"); }};
        meta.appendChild(timeEl);
        meta.appendChild(copyBtn);
        row.appendChild(meta);
    }} else if (m.type === "file") {{
        const fileCard = document.createElement("div");
        fileCard.className = "msg-file";

        const icon = document.createElement("span");
        icon.className = "msg-file-icon";
        if (m.file_category === "image") {{
            icon.classList.add("msg-thumb");
            const tImg = document.createElement("img");
            tImg.src = "/api/download/" + encodeURIComponent(m.file_name);
            tImg.onerror = () => {{ icon.classList.remove("msg-thumb"); icon.textContent = m.file_icon || "📎"; }};
            icon.appendChild(tImg);
        }} else {{
            icon.textContent = m.file_icon || "📎";
        }}

        const info = document.createElement("div");
        info.className = "msg-file-info";
        const nameEl = document.createElement("div");
        nameEl.className = "msg-file-name";
        nameEl.textContent = m.file_name;
        const sizeEl = document.createElement("div");
        sizeEl.className = "msg-file-size";
        sizeEl.textContent = m.file_size_fmt;
        info.appendChild(nameEl);
        info.appendChild(sizeEl);

        const actions = document.createElement("div");
        actions.className = "msg-file-actions";
        const dlBtn = document.createElement("a");
        dlBtn.className = "btn btn-primary btn-sm";
        dlBtn.href = "/api/download/" + encodeURIComponent(m.file_name);
        dlBtn.download = "";
        dlBtn.textContent = "下载";
        actions.appendChild(dlBtn);

        fileCard.appendChild(icon);
        fileCard.appendChild(info);
        fileCard.appendChild(actions);
        row.appendChild(fileCard);

        const meta = document.createElement("div");
        meta.className = "msg-row-meta";
        meta.style.paddingLeft = "4px";
        const timeEl = document.createElement("span");
        timeEl.className = "msg-time";
        timeEl.textContent = m.time;
        meta.appendChild(timeEl);
        row.appendChild(meta);
    }}

    list.appendChild(row);
}}

function scrollMsgList() {{
    const list = $("#msgList");
    list.scrollTop = list.scrollHeight;
}}

const msgInput = $("#msgInput");

function autoResize() {{
    msgInput.style.height = "auto";
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + "px";
}}

msgInput.addEventListener("input", autoResize);

function sendMessage() {{
    const text = msgInput.value.trim();
    if (!text) return;
    fetch("/api/messages", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ text: text }})
    }})
    .then(r => r.json())
    .then(resp => {{
        if (resp.ok) {{
            msgInput.value = "";
            msgInput.style.height = "auto";
            pollMessages();
        }} else {{
            toastMsg("发送失败: " + (resp.error || ""));
        }}
    }})
    .catch(() => toastMsg("网络错误，请检查连接"));
}}


$("#msgSend").addEventListener("click", sendMessage);
// 移动端键盘无 Shift 键，统一用按钮发送，Enter 正常换行

// ---- 启动 ----
startPoll();
</script>
</body>
</html>"""


# ============================================================
# 图标映射
# ============================================================

ICON_MAP = {
    # 图片
    ".jpg": "🖼", ".jpeg": "🖼", ".png": "🖼", ".gif": "🖼", ".webp": "🖼",
    ".svg": "🖼", ".bmp": "🖼", ".ico": "🖼",
    # 视频
    ".mp4": "🎬", ".mkv": "🎬", ".avi": "🎬", ".mov": "🎬", ".wmv": "🎬",
    ".flv": "🎬", ".webm": "🎬",
    # 音频
    ".mp3": "🎵", ".wav": "🎵", ".flac": "🎵", ".aac": "🎵", ".ogg": "🎵",
    ".wma": "🎵", ".m4a": "🎵",
    # 文档
    ".pdf": "📕", ".doc": "📄", ".docx": "📄", ".xls": "📊", ".xlsx": "📊",
    ".ppt": "📽", ".pptx": "📽", ".txt": "📝", ".md": "📝", ".csv": "📊",
    # 压缩
    ".zip": "📦", ".rar": "📦", ".7z": "📦", ".tar": "📦", ".gz": "📦",
    ".bz2": "📦", ".xz": "📦",
    # 代码
    ".py": "💻", ".js": "💻", ".ts": "💻", ".java": "💻", ".c": "💻",
    ".cpp": "💻", ".go": "💻", ".rs": "💻", ".html": "💻", ".css": "💻",
    ".json": "💻", ".xml": "💻", ".yaml": "💻", ".yml": "💻", ".sh": "💻",
    # APK
    ".apk": "📱", ".aab": "📱",
}


CATEGORY_MAP = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".bmp": "image", ".ico": "image",
    ".mp4": "video", ".mkv": "video", ".avi": "video", ".mov": "video",
    ".wmv": "video", ".flv": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".aac": "audio",
    ".ogg": "audio", ".wma": "audio", ".m4a": "audio",
    ".pdf": "doc", ".doc": "doc", ".docx": "doc", ".xls": "doc",
    ".xlsx": "doc", ".ppt": "doc", ".pptx": "doc", ".txt": "doc",
    ".md": "doc", ".csv": "doc",
    ".zip": "archive", ".rar": "archive", ".7z": "archive",
    ".tar": "archive", ".gz": "archive", ".bz2": "archive", ".xz": "archive",
    ".py": "code", ".js": "code", ".ts": "code", ".java": "code",
    ".c": "code", ".cpp": "code", ".go": "code", ".rs": "code",
    ".html": "code", ".css": "code", ".json": "code", ".xml": "code",
    ".yaml": "code", ".yml": "code", ".sh": "code",
    ".apk": "apk", ".aab": "apk",
}

def get_category(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return CATEGORY_MAP.get(ext, "other")

def get_icon(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return ICON_MAP.get(ext, "📎")


# ============================================================
# HTTP 处理器
# ============================================================

class Handler(BaseHTTPRequestHandler):
    server_addr = ""  # 由 main() 设置

    @staticmethod
    def _add_message(msg: dict):
        """msg: {"type": "text", "text": "..."} 或 {"type": "file", "file_name": ...}"""
        global _MSG_ID, _MESSAGES, _MAX_MESSAGES
        _MSG_ID += 1
        msg["id"] = _MSG_ID
        msg["time"] = time.strftime("%H:%M:%S")
        _MESSAGES.append(msg)
        if len(_MESSAGES) > _MAX_MESSAGES:
            _MESSAGES = _MESSAGES[-_MAX_MESSAGES:]

    def log_message(self, fmt, *args):
        if args[1] not in ("200", "304"):
            sys.stderr.write(f"[{self.log_date_time_string()}] {args[0]} {args[1]}\n")

    # ---- 响应辅助 ----
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content, status=200):
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, path: Path):
        if not path.is_file():
            self._json({"ok": False, "error": "文件不存在"}, 404)
            return
        size = path.stat().st_size
        encoded = urllib.parse.quote(path.name, safe="")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", size)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(BUFFER_SIZE):
                self.wfile.write(chunk)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            qr = generate_qrcode_svg(f"http://{self.server_addr}")
            self._html(build_html(self.server_addr, qr))

        elif path == "/api/files":
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            items = []
            for f in sorted(UPLOAD_DIR.iterdir(),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file():
                    st = f.stat()
                    items.append({
                        "name": f.name,
                        "size": st.st_size,
                        "size_fmt": format_size(st.st_size),
                        "mtime": st.st_mtime,
                        "mtime_fmt": format_mtime(st.st_mtime),
                        "type_icon": get_icon(f.name),
                        "category": get_category(f.name),
                    })
            self._json(items)

        elif path.startswith("/api/download/"):
            name = urllib.parse.unquote(path.split("/api/download/", 1)[1])
            try:
                self._download(UPLOAD_DIR / safe_filename(name))
            except ValueError:
                self._json({"ok": False, "error": "无效的文件名"}, 400)

        elif path == "/api/messages":
            qs = urllib.parse.parse_qs(parsed.query)
            since = int(qs.get("since", [0])[0])
            msgs = [m for m in _MESSAGES if m["id"] > since]
            self._json(msgs)

        else:
            self._json({"ok": False, "error": "Not Found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path == "/api/messages":
            cl = int(self.headers.get("Content-Length", 0))
            if cl <= 0 or cl > 65536:
                self._json({"ok": False, "error": "无效的请求体"}, 400)
                return
            try:
                data = json.loads(self.rfile.read(cl))
                text = data.get("text", "").strip()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"ok": False, "error": "无效的 JSON"}, 400)
                return
            if not text or len(text) > 5000:
                self._json({"ok": False, "error": "消息为空或过长"}, 400)
                return
            self._add_message({"type": "text", "text": text})
            self._json({"ok": True})

        elif path == "/api/upload":
            ct = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ct:
                self._json({"ok": False, "error": "需要 multipart/form-data"}, 400)
                return

            boundary = None
            for item in ct.split(";"):
                item = item.strip()
                if item.startswith("boundary="):
                    boundary = item.split("=", 1)[1].strip().strip('"')
                    break

            if not boundary:
                self._json({"ok": False, "error": "缺少 boundary"}, 400)
                return

            cl = int(self.headers.get("Content-Length", 0))
            if cl <= 0:
                self._json({"ok": False, "error": "空的请求体"}, 400)
                return

            body = self.rfile.read(cl)
            parts = parse_multipart(body, boundary)

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            saved = []

            for p in parts:
                if p["name"] == "file" and p["filename"]:
                    try:
                        orig = safe_filename(p["filename"])
                    except ValueError:
                        continue

                    dest = UPLOAD_DIR / orig

                    dest.write_bytes(p["body"])
                    saved.append({
                        "name": dest.name,
                        "size": len(p["body"]),
                        "size_fmt": format_size(len(p["body"])),
                        "type_icon": get_icon(dest.name),
                    })
                    # 追加文件消息到聊天流
                    self._add_message({
                        "type": "file",
                        "file_name": dest.name,
                        "file_size": len(p["body"]),
                        "file_size_fmt": format_size(len(p["body"])),
                        "file_icon": get_icon(dest.name),
                        "file_category": get_category(dest.name),
                    })

            self._json({"ok": True, "files": saved})

        else:
            self._json({"ok": False, "error": "Not Found"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path.startswith("/api/delete/"):
            name = urllib.parse.unquote(path.split("/api/delete/", 1)[1])
            try:
                fp = UPLOAD_DIR / safe_filename(name)
            except ValueError:
                self._json({"ok": False, "error": "无效的文件名"}, 400)
                return

            if not fp.is_file():
                self._json({"ok": False, "error": "文件不存在"}, 404)
                return

            try:
                fp.unlink()
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"ok": False, "error": "Not Found"}, 404)


# ============================================================
# 入口
# ============================================================

def main():
    port = 8888
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"错误: 端口号必须是数字，收到了: {sys.argv[1]}")
            sys.exit(1)

    ips = get_local_ips()
    primary_addr = f"{ips[0]}:{port}"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    Handler.server_addr = primary_addr
    # 绑定 0.0.0.0，接受来自所有网络接口的连接
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║     局域网文件传输服务 v1.0          ║")
    print("  ╠══════════════════════════════════════╣")
    for ip in ips:
        print(f"  ║  http://{ip}:{port}")
    print(f"  ║  上传目录: {UPLOAD_DIR.absolute()}")
    print("  ╠══════════════════════════════════════╣")
    print("  ║  其他设备浏览器打开地址即可使用       ║")
    if _QRCODE_AVAILABLE:
        print("  ║  二维码已就绪，手机扫码直接访问       ║")
    else:
        print("  ║  pip install qrcode 即可启用二维码    ║")
    print("  ║  Ctrl+C 停止服务                     ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
