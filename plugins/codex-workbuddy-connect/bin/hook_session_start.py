"""SessionStart hook：确保 WorkBuddy 本地代理在跑。

设计要点：
  - 无论成败都 exit 0 —— hook 失败不该让用户的会话起不来；
  - 代理已在跑就直接返回（探一次端口，很便宜）；
  - 拉起用分离进程 + 独立日志，不让它跟着会话退出。
"""
from __future__ import annotations

import os
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROXY = PLUGIN_ROOT / "bin" / "wbproxy.py"
sys.path.insert(0, str(PLUGIN_ROOT / "bin"))
import wbspawn  # noqa: E402  （无控制台启动）

DEFAULT_PORT = 8787


def port() -> int:
    try:
        return int(os.environ.get("WORKBUDDY_AI_PROXY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def port_open(p: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.4):
            return True
    except OSError:
        return False


def is_up(p: int) -> bool:
    """Return true only when the listener is this proxy and `/health` responds."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{p}/health", timeout=2) as response:
            doc = json.loads(response.read().decode("utf-8", "replace"))
        return isinstance(doc, dict) and doc.get("provider") in (
            "workbuddy", "workbuddy-ai",
        )
    except Exception:
        return False


CATALOG_TTL_SEC = 3600.0


def catalog_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return Path(os.environ.get("WB_CATALOG") or (home / "workbuddy-catalog.json"))


def refresh_catalog(p: int) -> None:
    """让选择器跟上上游的最新名单。

    代理在 ``/v1/models`` 里会把当前 roster 落盘到 ``model_catalog_json``，
    所以这里只需要探一次。文件还新（<1 小时）就跳过，别让每次开会话都等上游
    ``/v3/config`` 的往返。失败不影响会话启动 —— hook 尽力而为。
    """
    try:
        if time.time() - catalog_path().stat().st_mtime < CATALOG_TTL_SEC:
            return
    except OSError:
        pass
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{p}/v1/models", timeout=15):
            return
    except Exception as e:
        print(f"[workbuddy] 刷新模型目录失败：{type(e).__name__}: {e}", file=sys.stderr)


def _utf8_output() -> None:
    """输出目标是日志文件而非控制台，固定 UTF-8，避免受 PYTHONUTF8 / 代码页影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _utf8_output()
    p = port()
    if is_up(p):
        refresh_catalog(p)
        return 0
    if port_open(p):
        print(
            f"[workbuddy] 端口 {p} 已监听，但代理 /health 不健康；"
            "看护会识别本插件进程并恢复，foreign 进程不会被终止",
            file=sys.stderr,
        )
        return 0
    if not PROXY.is_file():
        print(f"[workbuddy] 找不到代理脚本 {PROXY}", file=sys.stderr)
        return 0

    log = Path(tempfile.gettempdir()) / "wbproxy.log"
    try:
        # 用 wbspawn 取「不会带控制台窗口」的解释器：venv 里的 python.exe 是 uv 存根，
        # 转发时不传 creationflags，真实进程会自己弹一个可见控制台（实测踩过）。
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(PROXY), "--port", str(p)],
                               log=log, cwd=str(PLUGIN_ROOT))
    except OSError as e:
        print(f"[workbuddy] 拉起代理失败：{e}", file=sys.stderr)
        return 0

    # 等它就绪，最多 20 秒
    for _ in range(40):
        if is_up(p):
            print(f"[workbuddy] 代理已就绪 http://127.0.0.1:{p}/v1")
            refresh_catalog(p)
            return 0
        time.sleep(0.5)

    print(f"[workbuddy] 代理未在 20 秒内就绪，日志见 {log}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
