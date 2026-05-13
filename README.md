# LanDrop

Single-file, zero-dependency LAN file & text sharing. Just run and share.

## Features

- **Zero setup** — Single Python file, no `pip install`, no config. Python 3.6+ is all you need.
- **File transfer** — Upload, download, and manage files between any devices on the same LAN.
- **Text sharing** — Send text snippets (links, notes, code) across devices in a chat-like interface.
- **QR code** — Mobile devices scan the on-screen QR to join instantly (optional `qrcode` package).
- **Image thumbnails** — Real image previews in file list, messages, and detail view.
- **File filtering** — Filter by type: images, videos, audio, documents, APK.
- **Multi-device** — Browser-based, works on Windows, macOS, Linux, iOS, Android.
- **Drag & paste** — Drag files or paste from clipboard to upload.

## Quick Start

```bash
python file_transfer.py
```

Open the printed URL (e.g. `http://192.168.1.5:8888`) on any device in the same LAN.

```
  ╔══════════════════════════════════════╗
  ║     局域网文件传输服务 v1.0          ║
  ╠══════════════════════════════════════╣
  ║  http://192.168.1.5:8888
  ║  上传目录: /path/to/uploads
  ╠══════════════════════════════════════╣
  ║  其他设备浏览器打开地址即可使用       ║
  ║  Ctrl+C 停止服务                     ║
  ╚══════════════════════════════════════╝
```

## Usage

```bash
python file_transfer.py              # Default port 8888
python file_transfer.py 9999         # Custom port
```

Enable QR code support (optional):
```bash
pip install qrcode
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/files` | List all files |
| POST | `/api/upload` | Upload file (multipart) |
| GET | `/api/download/<name>` | Download file |
| DELETE | `/api/delete/<name>` | Delete file |
| GET | `/api/messages?since=N` | Poll messages |
| POST | `/api/messages` | Send text message |

## Design

- Single `file_transfer.py`, zero third-party dependencies.
- Messages stored in-memory only (max 200), lost on restart.
- No authentication — designed for trusted LAN environments.
- Frontend is vanilla JS, no frameworks or build tools.
- File uploads go to `./uploads/`, same-name files are overwritten.

## License

MIT
