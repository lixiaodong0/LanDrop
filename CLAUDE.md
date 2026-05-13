# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

局域网文件传输服务 — 单文件 Python HTTP 服务，设备间通过浏览器互传文字和文件。零第三方依赖，Python 3.6+ 可用。

## 常用命令

```bash
python file_transfer.py              # 默认端口 8888
python file_transfer.py 9999         # 指定端口

# 语法检查
python -c "import py_compile; py_compile.compile('file_transfer.py', doraise=True)"

# API 手动测试
curl -s http://127.0.0.1:8888/api/files                          # 文件列表
curl -s -X POST http://127.0.0.1:8888/api/messages \
  -H "Content-Type: application/json" -d '{"text":"hello"}'       # 发送文字
curl -s "http://127.0.0.1:8888/api/messages?since=0"             # 轮询消息
curl -s -X POST http://127.0.0.1:8888/api/upload \
  -F "file=@/path/to/file"                                        # 上传文件
curl -s -X DELETE http://127.0.0.1:8888/api/delete/filename      # 删除文件
```

## 架构

整个项目就是 `file_transfer.py` 一个文件，约 1100 行。结构分四层：

### 配置层 (行 28-35)
`UPLOAD_DIR`（上传目录）、`BUFFER_SIZE`、消息内存存储（`_MESSAGES` 最多 200 条，`_MSG_ID` 自增）。

### 工具函数层
- `get_local_ips()` — UDP socket 获取本机局域网 IP
- `safe_filename()` — `os.path.basename` 防路径穿越
- `parse_multipart()` — 手动解析 multipart/form-data（位置查找，兼容二进制内容）
- `generate_qrcode_svg()` — 可选依赖 qrcode，未安装时优雅降级
- `build_html()` — 内嵌 HTML 模板（f-string），包含完整 CSS + JS，单页应用
- `format_size()` / `get_icon()` — 文件大小格式化、扩展名→emoji 映射

### HTTP 路由层 (Handler 类)
| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 返回 SPA 页面 |
| GET | `/api/files` | 文件列表 JSON |
| GET | `/api/download/<name>` | 下载文件 |
| GET | `/api/messages?since=N` | 增量轮询消息 |
| POST | `/api/messages` | 发送文字消息（上限 5000 字） |
| POST | `/api/upload` | 上传文件（multipart） |
| DELETE | `/api/delete/<name>` | 删除文件 |

### 前端层 (build_html 内嵌)
单页聊天布局：顶栏（文件按钮）+ 消息列表 + 进度条 + 底部输入区（textarea + 附件按钮 + 发送按钮）+ 文件管理弹窗。JS 原生无框架，1.5s 轮询消息，支持拖拽/粘贴上传，页面隐藏时自动暂停轮询。

## 关键设计约束

- **单文件零依赖** — 不引入任何第三方包（qrcode 可选），不拆模块
- **消息仅内存** — 服务重启后消息全部丢失，不持久化
- **无身份验证** — 设计目标是局域网可信环境
- **前端原生 JS** — 不引入任何前端框架或构建工具
- **HTML 模板是 Python f-string** — JS 代码中的 `{` 必须写成 `{{`，`}` 写成 `}}`，只有 Python 变量用单个 `{}`
- **文件同名处理** — 自动加 `_1`、`_2` 后缀，不覆盖
