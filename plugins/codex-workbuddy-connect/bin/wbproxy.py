#!/usr/bin/env python3
"""WorkBuddy（国际版 + 国内版）→ OpenAI 兼容本地代理。

把两个 WorkBuddy 桌面端的登录态同时变成 Codex 可用的模型源：

  - 国际版 WorkBuddy AI → https://www.workbuddy.ai
  - 国内版 WorkBuddy    → https://copilot.tencent.com

模型 ID 带区域前缀（``ai/...`` / ``cn/...``），代理按前缀选择账号与上游；
不带前缀的旧 ID 继续按国际版处理，历史会话不受影响。

上游要点（实测得出，非推测）：
  - 凭据在 %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\ 下，
    两个产品各一个文件；accessToken/refreshToken 是 AES-256-GCM 信封，
    密钥要跑对应产品的 Electron 二进制（ELECTRON_RUN_AS_NODE=1）取
    workbuddyStorage 绑定。
  - 对话端点 **必须 stream=true**（否则 400 code 11101），且**首条消息必须是
    system**（否则 400 code 11128）。
  - 国际版模型目录 GET /v3/config，UA 必须带 WorkBuddyAI/<版本>。
  - 国内版模型目录同样 GET /v3/config，但必须用 CLI UA
    （CLI/2.63.2 CodeBuddy/2.63.2），否则拿不到 cli 模型名单。
  - 额度 POST /v2/billing/meter/get-user-resource（国际版）。

用法：
    python wbproxy.py [--port 8787] [--host 127.0.0.1] [--verbose]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import collections
import http.client
import json
import os
import random
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import wbdiscover
import wbresponses
import wbsettings
import wbspawn

def _read_plugin_version() -> str:
    """从 .codex-plugin/plugin.json 读版本，避免与清单各说各话。

    缓存目录里的清单是安装时复制过去的，直接读它就能反映当前真正装载的版本。
    """
    try:
        manifest = Path(__file__).resolve().parents[1] / ".codex-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "unknown")
    except Exception:
        return "unknown"


PLUGIN_VERSION = _read_plugin_version()

# ─────────────────────────── 区域配置 ───────────────────────────

AI_BASE = "https://www.workbuddy.ai"
CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"

GLOBAL_BASE = AI_BASE          # 兼容旧引用

HELPER_JS = (
    'process.stdout.write(String(process._linkedBinding('
    '"electron_browser_workbuddy_storage").loggerGet()))'
)
DEFAULT_KEY = "workbuddy-local"          # 客户端随便填，本地代理不校验
TOKEN_SKEW_SEC = 120                     # 提前 2 分钟视为过期
CN_CLI_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

_VERBOSE = False


class RegionSpec:
    """One upstream region's wire identity."""

    def __init__(self, region_id: str, label: str, chat_base: str,
                 billing_base: str, origin: str, ua_style: str) -> None:
        self.id = region_id
        self.label = label
        self.chat_base = chat_base
        self.billing_base = billing_base
        self.origin = origin
        self.ua_style = ua_style      # "app"（国际版）| "desktop"（国内版）

    @property
    def prefix(self) -> str:
        return self.id + "/"


REGIONS: dict[str, RegionSpec] = {
    "ai": RegionSpec("ai", "国际版", AI_BASE, AI_BASE, AI_BASE, "app"),
    "cn": RegionSpec("cn", "国内版", CN_CHAT_BASE, CN_BILLING_BASE, CN_BILLING_BASE, "desktop"),
}
REGION_ORDER = ("ai", "cn")

# 状态文件只在显式查看时短时缓存，避免一次状态命令重复打上游；
# 不落盘、不跨进程，账号一变就按 uid/enterpriseId 失效。
ACCOUNT_STATUS_CACHE: dict[str, dict] = {}
ACCOUNT_STATUS_TTL_SEC = 60.0
HEALTH_PATH_TTL_SEC = 300.0

# 实际生效的模型追踪：代理是唯一看到真实 model 字段的地方，
# /wb/last-model 据此回答「当前到底在用哪个模型」。
LAST_MODEL: dict = {
    "model": None, "upstream_model": None, "region": None, "ts": 0, "count": 0,
}

# ── 模型同步：桌面端在选择器里切换模型时只作用于当前会话，不会写回
# config.toml，导致「配置默认」与「实际在用」长期脱节（读配置的人会报错模型）。
# 代理既然每次都看到真实 model，就顺手把顶层 model 同步回配置，让两处一致。
# WB_FOLLOW_LAST_MODEL=0 可完全关闭；旧的 WB_SYNC_CONFIG 不再参与判断。
CODEX_CONFIG = os.environ.get(
    "WB_CODEX_CONFIG",
    os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
)
PROFILE_CONFIG = os.environ.get(
    "WB_PROFILE_CONFIG",
    os.path.join(os.path.dirname(CODEX_CONFIG), "wb.config.toml"),
)
FOLLOW_LAST_MODEL = os.environ.get("WB_FOLLOW_LAST_MODEL", "1") not in ("0", "false", "no")
_SYNC_LOCK = threading.Lock()


def _config_top_level(path: str, key: str) -> str | None:
    """Read one top-level TOML assignment before the first section."""
    try:
        # newline="" 保留原换行符；文本模式读取会把 CRLF 折成 LF，
        # 之后写回就整份文件被改写（diff 里「整个文件都变了」）。
        with open(path, encoding="utf-8", newline="") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("["):
                    break
                if re.match(rf"^{re.escape(key)}\s*=", s):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _config_model(path: str = CODEX_CONFIG) -> str | None:
    return _config_top_level(path, "model")


def _config_provider(path: str = CODEX_CONFIG) -> str | None:
    return _config_top_level(path, "model_provider")


def _rewrite_model(path: str, model: str) -> bool:
    """把顶层 model 改成目标值；只动那一行，其余原样保留。"""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    changed = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            break                     # 只改顶层键，不碰任何 [section]
        if s.startswith("model ") or s.startswith("model="):
            if "=" not in s:
                continue
            old = s.split("=", 1)[1].strip().strip('"').strip("'")
            if old == model:
                return False
            nl = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f'model = "{model}"{nl}'
            changed = True
            break
    if not changed:
        return False
    try:
        tmp = path + ".wb-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"同步模型失败（写不到配置 {path}）: {e}")
        return False


def _sync_config_model(model: str) -> None:
    """Sync the slug only into Codex configs that select the WorkBuddy provider."""
    if not FOLLOW_LAST_MODEL or not model:
        return
    with _SYNC_LOCK:
        for path in (CODEX_CONFIG, PROFILE_CONFIG):
            if not path or not os.path.isfile(path):
                continue
            provider = _config_provider(path)
            if provider != "workbuddy":
                # Mutating model without model_provider would leave a global
                # config such as provider=custom + model=cn/hy3, which breaks
                # the user's next session outside this plugin.
                log(f"同步模型：{path} 的 provider={provider or '未设置'}，跳过")
                continue
            if _config_model(path) is None:
                log(f"同步模型：{path} 里没有顶层 model 键，跳过")
                continue
            if _rewrite_model(path, model):
                log(f"已同步 {os.path.basename(path)} 默认模型 → {model}")


def _note_model(slug: str, upstream_model: str, region: str) -> None:
    LAST_MODEL["model"] = slug
    LAST_MODEL["upstream_model"] = upstream_model
    LAST_MODEL["region"] = region
    LAST_MODEL["ts"] = int(time.time())
    LAST_MODEL["count"] = LAST_MODEL.get("count", 0) + 1
    # Called only for real conversation traffic (chat/completions and
    # responses); catalog, health and credits probes never reach this path.
    # A non-stream client is still a real turn, so it syncs too. This is
    # deliberate: the endpoint tracks actual use, not probe requests.
    _sync_config_model(slug)


# Structured per-request diagnostics. This is separate from the stderr proxy
# log: one JSON line per request at start/end/failure, plus bounded SSE frame
# excerpts. It exists to answer "which side closed a Responses stream and why".
WB_DIAG_DIR = os.environ.get(
    "WB_DIAG_DIR",
    str(wbsettings.codex_home() / "workbuddy-connect"),
)
WB_DIAG_FRAME_LIMIT = 40
WB_DIAG_EXCERPT_BYTES = 320
_diag_lock = threading.Lock()


def _wire_error(exc: BaseException, *, phase: str = "") -> dict:
    """Classify transport errors without ever copying tokens or payload bodies."""
    text = str(exc)
    name = type(exc).__name__
    code = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, urllib.error.HTTPError):
        side, category = "upstream", f"http_{exc.code}"
        return {"type": type(exc).__name__, "message": str(exc.reason)[:300],
                "errno": None, "winerror": None, "side": side, "category": category}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        text = str(reason)
        name = type(reason).__name__
        code = getattr(reason, "errno", None)
        winerror = getattr(reason, "winerror", None)
    n = str(code or winerror or "")
    side, category = "unknown", name
    if phase in ("write", "downstream"):
        side = "downstream"
        category = "client_disconnect"
    elif n in ("10054", "10053", "10060", "11001", "10061") or isinstance(exc, (ConnectionError, TimeoutError)):
        side = "upstream"
        category = {
            "10054": "upstream_reset",
            "10053": "upstream_abort",
            "10060": "upstream_timeout",
            "11001": "dns_resolution_failed",
            "10061": "upstream_connection_refused",
        }.get(n, name)
    elif isinstance(exc, OSError):
        side = "downstream"
        category = "client_disconnect" if n in ("10054", "10053") else name
    elif "EOF occurred in violation of protocol" in text or name in ("SSLEOFError", "SSLError"):
        side, category = "upstream", "upstream_tls_eof"
    return {"type": name, "message": text[:300], "errno": code, "winerror": winerror,
            "side": side, "category": category}


def _redact_json(obj, depth: int = 0):
    """Diagnostics only: keep shape and sizes, never full user/tool content."""
    if depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(x in lk for x in ("authorization", "token", "cookie", "secret", "api_key", "apikey")):
                out[k] = "<redacted>"
            else:
                out[k] = _redact_json(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact_json(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, str):
        return {"_len": len(obj), "_head": obj[:WB_DIAG_EXCERPT_BYTES]}
    return obj


def _diag_path() -> Path:
    path = Path(WB_DIAG_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path / "wbproxy_events.jsonl"


def _diag(event: str, **fields) -> None:
    row = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
           "pid": os.getpid(), "event": event}
    row.update(fields)
    try:
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        path = _diag_path()
        with _diag_lock:
            try:
                if path.is_file() and path.stat().st_size > 5 * 1024 * 1024:
                    path.replace(path.with_suffix(".jsonl.1"))
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _diag_request_summary(payload: dict, chat_payload: dict | None = None) -> dict:
    input_items = payload.get("input") or []
    messages = (chat_payload or {}).get("messages") or []
    text_bytes = sum(len(str(m.get("content") or "")) for m in messages)
    roles = collections.Counter(str(m.get("role")) for m in messages)
    return {
        "input_items": len(input_items) if isinstance(input_items, list) else 1,
        "messages": len(messages),
        "roles": dict(roles),
        "message_text_bytes": text_bytes,
        "tools": len(payload.get("tools") or []),
        "reasoning_effort": (payload.get("reasoning") or {}).get("effort") if isinstance(payload.get("reasoning"), dict) else None,
        "top_level_keys": sorted(str(k) for k in payload),
    }


def _diagnostics_summary(rows: list[dict]) -> dict:
    verdicts = _analyze_diagnostics(rows)["requests"]
    counts = collections.Counter(v["verdict"] for v in verdicts)
    errors = collections.Counter(c for v in verdicts for c in v["error_categories"])
    return {
        "diagnostics_enabled": True,
        "log_path": str(_diag_path()),
        "requests_seen": len(verdicts),
        "verdicts": dict(counts),
        "error_categories": dict(errors),
        "last_diagnostic_ts": rows[-1].get("ts") if rows else None,
        "last_incomplete": next((v for v in reversed(verdicts)
                                 if v["verdict"] != "completed"), None),
    }


def recent_diagnostics(limit: int = 200) -> list[dict]:
    path = _diag_path()
    if not path.is_file():
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-max(1, min(limit, 1000)):]


def _diag_sse_excerpt(line: str) -> dict:
    """Keep SSE frame shape/counts, never delta text or tool arguments."""
    if not line:
        return {"kind": "blank"}
    kind, _, rest = line.partition(":")
    if kind.strip() != "data":
        return {"kind": kind.strip()[:24], "bytes": len(line)}
    chunk = rest.strip()
    if chunk == "[DONE]":
        return {"kind": "data", "done": True}
    try:
        doc = json.loads(chunk)
    except json.JSONDecodeError:
        return {"kind": "data", "unparsed": True, "bytes": len(chunk)}
    if not isinstance(doc, dict):
        return {"kind": "data", "json_type": type(doc).__name__}
    choices = []
    for ch in doc.get("choices") or []:
        delta = ch.get("delta") or {}
        tool_calls = delta.get("tool_calls") or []
        choices.append({
            "finish_reason": ch.get("finish_reason"),
            "content_bytes": len(str(delta.get("content") or "")),
            "reasoning_bytes": len(str(delta.get("reasoning_content") or "")),
            "tool_call_count": len(tool_calls),
            "tool_names": [str((t.get("function") or {}).get("name") or "")
                           for t in tool_calls[:5]],
        })
    return {
        "kind": "data",
        "keys": sorted(str(k) for k in doc),
        "id": doc.get("id"),
        "created": doc.get("created"),
        "has_usage": bool(doc.get("usage")),
        "choices": choices,
    }


class StreamDiagnostics:
    """Wrap the upstream SSE iterator and record why a Responses stream ended.

    The key question for `stream closed before response.completed` is whether
    the upstream Chat SSE sent a finish_reason/[DONE] before Codex saw a
    `response.completed`. These counters answer it with timestamps and bounded
    frame excerpts; no complete prompts or tokens are persisted.
    """

    def __init__(self, request_id: str, *, endpoint: str, slug: str, region: str,
                 upstream_model: str, payload: dict, chat_payload: dict | None = None):
        self.request_id = request_id
        self.endpoint = endpoint
        self.started = time.time()
        self.slug = slug
        self.region = region
        self.upstream_model = upstream_model
        self.frames = 0
        self.data_frames = 0
        self.unparsed = 0
        self.finish_reason = None
        self.saw_done = False
        self.last_finish_at = None
        self.excerpts = []
        self.error = None
        self.completed = False
        _diag("request_start", request_id=request_id, endpoint=endpoint, slug=slug,
              region=region, upstream_model=upstream_model,
              **_diag_request_summary(payload, chat_payload))
        _diag("upstream_open", request_id=request_id, t_ms=0)

    def wrap(self, resp):
        """Read upstream SSE; turn a mid-stream transport failure into a terminal frame.

        Codex reports `stream closed before response.completed` when the HTTP
        connection ends without a terminal Responses event. A reset DNS/TCP
        error while reading the upstream socket must not propagate out of the
        SSE generator: it would abort `stream_responses_events()` before its
        final `response.completed`. Instead emit a synthetic failed finish so
        the same generator closes open items and sends a terminal event.
        """
        try:
            for raw in resp:
                self.frames += 1
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:
                    line = ""
                if self.frames <= WB_DIAG_FRAME_LIMIT:
                    self.excerpts.append(_diag_sse_excerpt(line))
                if not line.startswith("data:"):
                    yield raw
                    continue
                chunk = line[5:].strip()
                self.data_frames += 1
                if chunk == "[DONE]":
                    self.saw_done = True
                    self.last_finish_at = time.time()
                elif chunk:
                    try:
                        ev = json.loads(chunk)
                    except json.JSONDecodeError:
                        self.unparsed += 1
                    else:
                        for ch in ev.get("choices") or []:
                            if ch.get("finish_reason"):
                                self.finish_reason = ch["finish_reason"]
                                self.last_finish_at = time.time()
                yield raw
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            self.upstream_error(exc, phase="read")
            self.finish_reason = "upstream_transport_error"
            self.last_finish_at = time.time()
            synthetic = {"choices": [{"index": 0, "delta": {}, "finish_reason": "failed"}]}
            yield ("data: " + json.dumps(synthetic) + "\n\n").encode("utf-8")

    def upstream_error(self, exc: BaseException, *, phase: str) -> None:
        info = _wire_error(exc, phase=phase)
        self.error = info
        _diag("upstream_error", request_id=self.request_id, phase=phase,
              elapsed_ms=int((time.time() - self.started) * 1000),
              **info)

    def upstream_terminal_seen(self) -> bool:
        """Whether Chat SSE supplied either a finish_reason or `[DONE]`."""
        return self.saw_done or self.finish_reason is not None

    def end(self, *, completed: bool, error: BaseException | None = None,
            phase: str = "stream") -> None:
        row = {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "slug": self.slug,
            "region": self.region,
            "elapsed_ms": int((time.time() - self.started) * 1000),
            "upstream_sse_frames": self.frames,
            "upstream_data_frames": self.data_frames,
            "unparsed_data_frames": self.unparsed,
            "saw_done": self.saw_done,
            "finish_reason": self.finish_reason,
            "last_finish_age_ms": int((time.time() - self.last_finish_at) * 1000)
            if self.last_finish_at else None,
            "upstream_terminal_seen": self.upstream_terminal_seen(),
            "response_completed_emitted": completed,
            "excerpts": self.excerpts[:WB_DIAG_FRAME_LIMIT],
        }
        if error is not None:
            row["error"] = _wire_error(error, phase=phase)
            row["phase"] = phase
        _diag("stream_end" if completed else "stream_incomplete", **row)

def log(*a):
    """请求流水默认静音（--verbose 才打开）。"""
    if _VERBOSE:
        print("[wbproxy]", *a, file=sys.stderr, flush=True)


def upstream_error_text(detail) -> str:
    """把上游错误压成一行可读摘要。

    直接把上游原始 JSON 塞给用户既不友好也不安全（可能带双份字段），
    这里只取 code / msg / displayTips.zh 等拼成一行，其余一律不回显。
    """
    if isinstance(detail, dict):
        o, s = detail, json.dumps(detail, ensure_ascii=False)
    else:
        s = detail if isinstance(detail, str) else str(detail)
        try:
            o = json.loads(s)
        except Exception:
            return s[:700]
        if not isinstance(o, dict):
            return s[:700]

    parts = []
    if o.get("code") is not None:
        parts.append(f"code={o['code']}")
    if o.get("msg"):
        parts.append(str(o["msg"]))

    tip = None
    for key in ("displayTips", "displayMsg"):
        v = o.get(key)
        if isinstance(v, dict):
            tip = v.get("zh") or v.get("en")
        elif isinstance(v, str):
            tip = v
        if tip:
            break

    # 上游有时把内层 JSON 放在 error.message 字符串里。
    if not tip:
        err = o.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            try:
                return upstream_error_text(json.loads(err["message"]))
            except Exception:
                tip = err["message"]

    if tip:
        parts.append(tip)
    if o.get("requestId"):
        parts.append(f"requestId={o['requestId']}")
    return " / ".join(parts) if parts else s[:700]


def log_err(*a):
    """错误事件永远记录，不依赖 --verbose。"""
    print("[wbproxy]", *a, file=sys.stderr, flush=True)

# WorkBuddy returns HTTP 429 for two materially different conditions:
#   - 14003: the selected model is temporarily saturated. A bounded retry is
#     useful because the same request frequently succeeds seconds later.
#   - 6004: the account's usage window is exhausted. The upstream publishes a
#     recovery timestamp, so retrying immediately only burns requests.
# Keep the retry policy local to the proxy. Codex's provider budget is set to
# zero in setup.py so the two layers cannot multiply into a retry storm.
RATE_LIMIT_MODEL_BUSY = 14003
RATE_LIMIT_ACCOUNT_QUOTA = 6004
RATE_LIMIT_RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0)


def _rate_limit_code(detail) -> int | None:
    if isinstance(detail, dict):
        value = detail.get("code")
    else:
        try:
            value = json.loads(detail).get("code")
        except Exception:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _retry_delays() -> tuple[float, ...]:
    """Return the bounded 14003 retry schedule.

    Four retries means five total attempts: the original request plus four
    retries. Delays total at most 37 seconds before jitter. A malformed
    override falls back to the safe default instead of retrying forever.
    """
    raw = os.environ.get("WB_14003_RETRIES")
    if raw is None:
        return RATE_LIMIT_RETRY_DELAYS
    try:
        count = int(raw)
    except ValueError:
        return RATE_LIMIT_RETRY_DELAYS
    if count < 0 or count > len(RATE_LIMIT_RETRY_DELAYS):
        return RATE_LIMIT_RETRY_DELAYS
    return RATE_LIMIT_RETRY_DELAYS[:count]


def _retry_delay(attempt: int) -> float:
    base = _retry_delays()[attempt]
    return base * random.uniform(0.85, 1.15)


def _rate_limit_message(status: int, detail) -> str:
    code = _rate_limit_code(detail)
    if code == RATE_LIMIT_ACCOUNT_QUOTA:
        return (
            "WorkBuddy account rate limit (code 6004). "
            "Switch to another model or wait for the reset time in the upstream message."
        )
    if code == RATE_LIMIT_MODEL_BUSY:
        return (
            "WorkBuddy model is busy (code 14003). "
            "Switch to another model or retry after a short wait."
        )
    return "WorkBuddy upstream rate limit (HTTP 429). Switch models or retry later."


def configure_upstream_network() -> None:
    """Ignore inherited proxy variables for the loopback-to-WorkBuddy hop.

    The Codex process and this proxy can inherit a dead local proxy
    (`HTTP_PROXY=http://127.0.0.1:9` in the measured environment). urllib
    would then route the upstream request through it and fail before reaching
    WorkBuddy. `WB_KEEP_PROXY=1` preserves the old behavior for environments
    that genuinely require an outbound proxy.
    """
    if os.environ.get("WB_KEEP_PROXY") == "1":
        return
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )


# ─────────────────────── 凭据：解密与刷新 ───────────────────────

def _aad(key_id: str, suite: int) -> bytes:
    """WorkBuddy 的 AAD 构造（照抄其 buildAuthenticatedContextAad）。"""
    def lp(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack(">I", len(b)) + b

    return (b"WB-AAD\x00" + b"\x01" + lp("WBEV1") + lp("sym-v1")
            + struct.pack(">I", suite) + lp(key_id) + b"\x02" + b"\x00" + b"\x00")


_KEY_CACHE: dict[tuple[str, str], bytes] = {}
_KEY_LOCK = threading.Lock()


def _electron_payload(app: str) -> dict:
    """跑指定 WorkBuddy 的 Electron 取 at-rest 密钥。"""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    r = wbspawn.run_hidden(
        [app, "-e", HELPER_JS], capture_output=True, text=True, timeout=60, env=env,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f"WorkBuddy 密钥助手失败（exit={r.returncode}）；确认 App 已安装：{app}"
        )
    return json.loads(r.stdout.strip())


def _envelope_key_id(wrapped: dict) -> str:
    inner = json.loads(base64.b64decode(wrapped["envelope"]))
    return str(inner.get("keyId") or "")


def _protector_key_for(region: str, key_id: str) -> bytes:
    """Resolve the at-rest key for one envelope id.

    The region's own desktop app is tried first; the other product is only a
    fallback, because the two installs may share a key today but are separate
    products and must not be assumed to.
    """
    with _KEY_LOCK:
        cached = _KEY_CACHE.get((region, key_id))
        if cached is not None:
            return cached
        order = [region] + [r for r in REGION_ORDER if r != region]
        errors = []
        for candidate in order:
            app = wbdiscover.find_app(candidate)
            if not app or not os.path.isfile(app):
                errors.append(f"{candidate}: 找不到桌面端")
                continue
            try:
                payload = _electron_payload(app)
                if payload.get("version") != 1 or not payload.get("atRestSecretKey"):
                    raise RuntimeError("密钥 payload 版本不符")
                key = hashlib.sha256(payload["atRestSecretKey"].encode("utf-8")).digest()
                derived = hashlib.sha256(key).hexdigest()[:16]
                if derived != key_id:
                    errors.append(f"{candidate}: keyId 不匹配（{derived} != {key_id}）")
                    continue
                _KEY_CACHE[(region, key_id)] = key
                return key
            except Exception as e:
                errors.append(f"{candidate}: {type(e).__name__}: {e}")
        raise RuntimeError("无法取得凭据密钥；" + "；".join(errors))


def _decrypt_field(wrapped: dict, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    inner = json.loads(base64.b64decode(wrapped["envelope"]))
    if inner.get("suite") != 1:
        raise RuntimeError(f"未知信封 suite={inner.get('suite')}")
    pt = AESGCM(key).decrypt(
        base64.b64decode(inner["nonce"]),
        base64.b64decode(inner["ciphertext"]) + base64.b64decode(inner["authTag"]),
        _aad(inner["keyId"], inner["suite"]),
    )
    return pt.decode("utf-8")


class RegionUnavailable(Exception):
    """The selected region has no usable sign-in; never fall back elsewhere."""

    def __init__(self, region: str, detail: str) -> None:
        super().__init__(detail)
        self.region = region
        self.detail = detail

class Credentials:
    """一个区域的 WorkBuddy 凭据：读桌面端 auth 文件 → 解密 → 到期自动刷新。"""

    def __init__(self, region: str = "ai") -> None:
        self.region = region
        self.spec = REGIONS[region]
        self._lock = threading.Lock()
        self.app = wbdiscover.find_app(region) or ""
        self.auth_file = wbdiscover.find_auth_file(region) or ""
        self.access_token = ""
        self.refresh_token = ""
        self.uid = ""
        self.enterprise_id = ""
        self.nickname = ""
        self.domain = wbdiscover.REGIONS[region].default_domain
        self.expires_at = 0.0          # epoch 秒
        self.app_version = "5.5.2"
        self._load_app_version()
        self.reload()

    def _load_app_version(self) -> None:
        """UA 里要带 App 版本，网关按它分流 /v3/config。"""
        try:
            vpath = os.path.join(os.path.dirname(self.app), "version")
            v = open(vpath, encoding="utf-8").read().strip()
            if v:
                self.app_version = v
        except OSError:
            pass
        log(f"{self.region} app version:", self.app_version)

    def reload(self) -> None:
        with self._lock:
            if not self.auth_file or not os.path.isfile(self.auth_file):
                raise FileNotFoundError(
                    f"找不到{self.spec.label}凭据文件；先启动并登录{self.spec.label}桌面端"
                )
            with open(self.auth_file, encoding="utf-8") as fh:
                doc = json.loads(fh.read())
            auth = doc.get("auth", {})
            acct = doc.get("account", {})

            def read_value(v) -> str:
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and v.get("$wbEncrypted") == 1:
                    key = _protector_key_for(self.region, _envelope_key_id(v))
                    return _decrypt_field(v, key)
                return ""

            def read(name: str) -> str:
                return read_value(auth.get(name))

            self.access_token = read("accessToken")
            self.refresh_token = read("refreshToken")
            self.uid = str(acct.get("uid") or "") if isinstance(acct.get("uid"), str) else ""
            self.enterprise_id = str(acct.get("enterpriseId") or auth.get("enterpriseId") or "")
            # 昵称可能和 token 一样以 $wbEncrypted 信封保存；解不开时留空，
            # 由状态层回退到 UID 尾号，绝不把密文当昵称显示。
            raw_nickname = acct.get("nickname")
            try:
                self.nickname = read_value(raw_nickname).strip()
            except Exception as e:
                log(f"{self.region} 昵称读取失败（继续使用账号）：{type(e).__name__}")
                self.nickname = ""
            if self.nickname.startswith("$wbEncrypted"):
                self.nickname = ""
            self.domain = auth.get("domain", "") or wbdiscover.REGIONS[self.region].default_domain
            # expiresAt 是毫秒
            raw_exp = auth.get("expiresAt")
            if isinstance(raw_exp, (int, float)) and raw_exp > 0:
                self.expires_at = raw_exp / 1000.0
            else:
                exp_in = auth.get("expiresIn") or 0
                self.expires_at = time.time() + float(exp_in)
            log(f"{self.region} 凭据已加载 uid={self.uid[:8]}… "
                f"到期 {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.expires_at))}")

    # ── 请求头 ──
    def desktop_ua(self) -> str:
        if self.spec.ua_style == "app":
            return f"WorkBuddyAI/{self.app_version}"
        return f"WorkBuddy/{self.app_version} WorkBuddy/{self.app_version}"

    def catalog_ua(self) -> str:
        # 国内版必须用 CLI UA 才会返回 cli 模型名单（实测 20 个 vs 0 个）。
        return CN_CLI_UA if self.spec.id == "cn" else f"WorkBuddyAI/{self.app_version}"

    def _identity(self, enterprise: bool = True) -> dict:
        h: dict = {}
        if self.uid:
            h["X-User-Id"] = self.uid
        else:
            h["X-No-User-Id"] = "1"
        if enterprise:
            if self.enterprise_id:
                h["X-Enterprise-Id"] = self.enterprise_id
            else:
                h["X-No-Enterprise-Id"] = "1"
        if self.domain:
            h["X-Domain"] = self.domain
        return h

    def _common(self, ua: str | None = None) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.spec.origin,
            "Referer": self.spec.origin + "/",
            "User-Agent": ua or self.desktop_ua(),
            **self._identity(),
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Version": self.app_version,
            "X-Product": "SaaS",
        }

    def catalog_headers(self) -> dict:
        return {
            **self._common(self.catalog_ua()),
            "Authorization": "Bearer " + self.access_token,
        }

    def account_headers(self) -> dict:
        """Authenticated JSON headers shared by quota and other account APIs."""
        h = self._common()
        h["Content-Type"] = "application/json"
        h["Authorization"] = "Bearer " + self.access_token
        return h

    def chat_headers(self) -> dict:
        h = self.account_headers()
        h.update(conversation_headers())
        return h

    def expired(self) -> bool:
        return time.time() >= (self.expires_at - TOKEN_SKEW_SEC)

    def ensure_fresh(self) -> None:
        if self.expired():
            self.refresh()

    def refresh(self) -> None:
        """POST /v2/plugin/auth/token/refresh（X-Refresh-Token 只出现在这里）。"""
        with self._lock:
            if not self.refresh_token:
                raise RuntimeError("没有 refreshToken")
            h = {
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.spec.origin, "Referer": self.spec.origin + "/",
                # The refresh endpoint is a CLI endpoint for both regions; the
                # international token is minted by this same CLI UA as well.
                "User-Agent": CN_CLI_UA,
                "X-Refresh-Token": self.refresh_token,
                "X-Auth-Refresh-Source": "workbuddy",
            }
            if self.enterprise_id:
                h["X-Enterprise-Id"] = self.enterprise_id
            req = urllib.request.Request(
                self.spec.chat_base + "/v2/plugin/auth/token/refresh",
                data=b"", headers=h, method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            if body.get("code") != 0:
                raise RuntimeError(f"刷新失败: {body.get('msg')}")
            d = body["data"]
            self.access_token = d["accessToken"]
            if d.get("refreshToken"):
                self.refresh_token = d["refreshToken"]
            exp_in = d.get("expiresIn") or 0
            self.expires_at = time.time() + float(exp_in)
            log_err(f"{self.spec.label}令牌已刷新，新到期",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(self.expires_at)))

    # ── 目录 / 额度 ──
    def catalog(self) -> list[dict]:
        """Return normalized model rows for this region.

        Both regions answer /v3/config, but the document shape and the roster
        differ: the international app lists exactly its cli models, while the
        CN product document carries all models plus an `agents` roster that
        names the cli subset. The subset is the wire contract this proxy
        serves, so it is applied whenever present.
        """
        req = urllib.request.Request(self.spec.chat_base + "/v3/config",
                                     headers=self.catalog_headers())
        with urllib.request.urlopen(req, timeout=45) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        if doc.get("code") not in (0, None):
            raise RuntimeError(f"目录请求失败: {doc.get('msg')}")
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        return normalize_catalog(data)

    def credits(self) -> dict:
        if self.region == "cn" and self.enterprise_id:
            return self._enterprise_credits()
        body = json.dumps({"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca"}).encode()
        req = urllib.request.Request(
            self.spec.billing_base + "/v2/billing/meter/get-user-resource",
            data=body, headers=self.account_headers(), method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        accts = (((d.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        total_remain = sum(float(a.get("CapacityRemainPrecise") or a.get("CapacityRemain") or 0) for a in accts)
        total_size = sum(float(a.get("CapacitySizePrecise") or a.get("CapacitySize") or 0) for a in accts)
        return {
            "region": self.region,
            "remaining": round(total_remain, 2),
            "total": round(total_size, 2),
            "packages": [
                {
                    "name": a.get("PackageName"), "remain": a.get("CapacityRemain"),
                    "size": a.get("CapacitySize"),
                    "cycleEnd": a.get("CycleEndTime"),
                }
                for a in accts
            ],
        }

    def _enterprise_credits(self) -> dict:
        """国内版企业账号：周期额度接口，个人版接口对这些账号返回空列表。"""
        headers = self.account_headers()
        headers["X-Tenant-Id"] = self.enterprise_id
        req = urllib.request.Request(
            self.spec.billing_base + "/v2/billing/meter/get-enterprise-user-usage",
            data=b"{}", headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("code") not in (0, None):
            raise RuntimeError(f"企业额度请求失败: {d.get('msg')}")
        data = d.get("data") if isinstance(d.get("data"), dict) else d
        sources = []
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            sources.append(data["data"])
        if isinstance(data, dict):
            sources.append(data)
        limit = used = None
        cycle_reset = None
        for source in sources:
            candidate = source.get("limitNum", source.get("limit_num"))
            if not isinstance(candidate, (int, float)):
                continue
            limit = float(candidate)
            credit = source.get("credit", source.get("used_num"))
            used = float(credit) if isinstance(credit, (int, float)) else None
            if isinstance(source.get("cycleResetTime"), str):
                cycle_reset = source["cycleResetTime"]
            break
        if limit is None:
            raise RuntimeError("企业额度响应里没有可识别的 limitNum/limit_num")
        if limit == -1:
            return {
                "region": self.region, "remaining": 0, "total": 0,
                "unlimited": True, "packages": [
                    {"name": "enterprise", "remain": 0, "size": 0, "unlimited": True},
                ],
                **({"cycleResetTime": cycle_reset} if cycle_reset else {}),
            }
        if used is None:
            raise RuntimeError("企业额度响应里有额度上限但没有可识别的 credit/used_num")
        remain = max(limit - used, 0.0)
        return {
            "region": self.region,
            "remaining": round(remain, 2),
            "total": round(limit, 2),
            "packages": [
                {"name": "enterprise", "remain": remain, "size": limit},
            ],
            **({"cycleResetTime": cycle_reset} if cycle_reset else {}),
        }

# ─────────────────────── 目录解析与模型命名 ───────────────────────

# WorkBuddy 桌面端把「快速 / 均衡 / 极致」做成档位快捷项（fast/balanced/primary
# model 三个别名），它们只是同一批模型的不同思考预算，不是可单独选择的模型。
# Codex 选择器只列真实模型，所以这三个别名（以及 deep-model）一律不进目录。
TIER_ALIASES = frozenset({
    "fast-model", "balanced-model", "primary-model", "deep-model",
})

# 上游用 tags 标注多模态生成模型（出图/出视频），它们不是对话模型。
NON_CHAT_TAGS = frozenset({
    "text-to-image", "image-to-image", "text-to-video", "image-to-video",
})


def _positive(v) -> bool:
    return isinstance(v, (int, float)) and v > 0


def normalize_catalog(data: dict) -> list[dict]:
    """Turn one /v3/config data object into normalized model rows."""
    raw_models = data.get("models") if isinstance(data.get("models"), list) else []
    agents = data.get("agents") if isinstance(data.get("agents"), list) else []
    cli_ids: list[str] = []
    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == "cli":
            cli_ids = [x for x in (agent.get("models") or []) if isinstance(x, str)]
            break

    by_id: dict[str, dict] = {}
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid or m.get("disabled") is True:
            continue
        if mid in TIER_ALIASES:
            continue
        tags = {t for t in (m.get("tags") or []) if isinstance(t, str)}
        if tags & NON_CHAT_TAGS or isinstance(m.get("videoGeneration"), dict):
            continue
        cw = m.get("contextWindow") if isinstance(m.get("contextWindow"), dict) else {}
        context = (cw.get("defaultLength") if _positive(cw.get("defaultLength")) else None)
        if context is None and _positive(m.get("maxInputTokens")):
            context = m["maxInputTokens"]
        reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
        efforts = [e for e in (reasoning.get("supportedEfforts") or []) if isinstance(e, str)]
        default_effort = reasoning.get("defaultEffort") or reasoning.get("effort")
        by_id[mid] = {
            "id": mid,
            "name": m.get("name") if isinstance(m.get("name"), str) and m.get("name") else mid,
            "context_window": context,
            "max_output_tokens": m.get("maxOutputTokens") if _positive(m.get("maxOutputTokens")) else None,
            "supports_images": m.get("supportsImages") is True and m.get("disabledMultimodal") is not True,
            "reasoning_efforts": efforts,
            "default_effort": default_effort if isinstance(default_effort, str) else None,
            "can_disable_thinking": reasoning.get("canDisableThinking") is True,
            "credits": m.get("credits") if isinstance(m.get("credits"), str) else "",
        }

    ordered = [by_id[i] for i in cli_ids if i in by_id] if cli_ids else list(by_id.values())
    return disambiguate_names(ordered)


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while prefix and not value.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def disambiguate_names(rows: list[dict]) -> list[dict]:
    """Make display names unique inside one region.

    Upstream reuses names: CN ships ``hy3`` and ``hy3-x`` both called "Hy3",
    and the international roster has two "Deepseek-V4.1-Flash" ids. The id's
    distinguishing tail is the honest disambiguator, so ``hy3-x`` reads
    "Hy3 (X)" and ``deepseek-v4.1-flash-sg`` reads "Deepseek-V4.1-Flash (SG)".
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["name"], []).append(row)
    out: list[dict] = []
    for row in rows:
        group = groups[row["name"]]
        if len(group) > 1:
            prefix = _common_prefix([r["id"] for r in group])
            tail = row["id"][len(prefix):].strip("-_ .")
            row = dict(row)
            if tail:
                row["name"] = f'{row["name"]} ({tail.upper() if len(tail) <= 3 else tail})'
            # The row whose id *is* the shared prefix keeps the plain name; only
            # its siblings carry a suffix, which is what makes the pair readable.
        out.append(row)
    return out


def supports_images(model: dict) -> bool:
    """Whether the upstream explicitly permits image input for one model."""
    return model.get("supportsImages") is True and model.get("disabledMultimodal") is not True


def reasoning_levels(row: dict) -> tuple[list[str], str]:
    """Return (levels, default) for the Codex picker.

    Declared upstream efforts win; models that declare none keep the previous
    permissive fallback. `none` is only offered when the upstream says thinking
    can actually be disabled.
    """
    levels = list(row.get("reasoning_efforts") or [])
    if levels:
        if row.get("can_disable_thinking") and "none" not in levels:
            levels.insert(0, "none")
    else:
        levels = ["none", "low", "medium", "high"]
    default = row.get("default_effort")
    if default not in levels:
        default = "low" if "low" in levels else levels[0]
    return levels, default


# ─────────────────────────── 上游转发 ───────────────────────────

CHAT_PATH = "/v2/chat/completions"


def conversation_headers() -> dict:
    """Fresh channel identifiers required by the WorkBuddy chat gateway.

    Since 2026-10-16 the international endpoint rejects chat requests that
    omit all three headers with HTTP 400 / code 11128. The upstream does not
    key conversation history by these ids, so fresh UUIDs per request are the
    measured behavior (also used by dsh-workbuddy-connect). The CN gateway
    accepts the same trio.
    """
    return {
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
    }


def _prepare_payload(payload: dict) -> dict:
    """上游硬约束（实测）：

    - 首条消息必须是 ``system``（否则 400 code 11128）；
    - 完全不接受 ``developer`` 角色（任何位置都报同一个错）；
    - 必须 ``stream=true``（否则 400 code 11101）。

    Hermes 对 OpenAI 系模型会把 system 改写成 ``developer``，所以这里统一
    折回 ``system``，再补一条兜底 system。
    """
    payload = dict(payload)
    msgs = []
    for m in payload.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "developer":
            m = {**m, "role": "system"}
        msgs.append(m)
    if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": "You are a helpful assistant."})
    payload["messages"] = msgs
    payload["stream"] = True
    return payload


class UpstreamTransportError(Exception):
    """The upstream network connection failed before a response was established."""

    def __init__(self, exc: BaseException, attempts: int) -> None:
        self.exc = exc
        self.attempts = attempts
        self.info = _wire_error(exc)
        super().__init__(str(exc))


def _transport_retry_delays() -> tuple[float, ...]:
    raw = os.environ.get("WB_TRANSPORT_RETRIES", "2")
    try:
        count = max(0, min(3, int(raw)))
    except ValueError:
        count = 2
    return (0.4, 1.2, 2.5)[:count]


def _is_transient_transport_error(exc: BaseException) -> bool:
    info = _wire_error(exc)
    return info["category"] in {
        "upstream_reset", "upstream_abort", "upstream_timeout",
        "dns_resolution_failed", "upstream_connection_refused",
        "upstream_tls_eof",
        "TimeoutError", "RemoteDisconnected", "IncompleteRead",
        "ConnectionResetError", "socket.gaierror",
    }


def open_upstream(creds: Credentials, payload: dict):
    """Open the WorkBuddy chat stream with bounded, classified retries.

    401/403 refreshes credentials once. HTTP 429 with code 14003 is retried at
    most four times with short backoff. HTTP 429 with code 6004 is terminal and
    surfaced immediately because its recovery window is usually much longer.
    """
    creds.ensure_fresh()
    data = json.dumps(_prepare_payload(payload)).encode("utf-8")
    delays = _retry_delays()
    transport_delays = _transport_retry_delays()
    refreshed = False
    retry_index = 0
    transport_index = 0
    while True:
        try:
            req = urllib.request.Request(
                creds.spec.chat_base + CHAT_PATH,
                data=data, headers=creds.chat_headers(), method="POST",
            )
            return urllib.request.urlopen(req, timeout=600), payload
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (401, 403) and not refreshed:
                refreshed = True
                log_err(f"{creds.spec.label}上游 401/403，刷新令牌后重试")
                creds.refresh()
                continue
            code = _rate_limit_code(detail)
            if e.code == 429 and code == RATE_LIMIT_MODEL_BUSY and retry_index < len(delays):
                delay = _retry_delay(retry_index)
                log_err(
                    "upstream 429 code=14003 model busy; "
                    f"retry {retry_index + 1}/{len(delays)} after {delay:.1f}s"
                )
                time.sleep(delay)
                retry_index += 1
                continue
            if e.code == 429:
                reason = "account quota" if code == RATE_LIMIT_ACCOUNT_QUOTA else "upstream rate limit"
                log_err(f"upstream 429 code={code or 'unknown'} {reason}; retries exhausted or disabled")
            raise UpstreamError(e.code, detail)
        except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as e:
            # These are *upstream* failures, not localhost/proxy failures. Codex
            # only knows its URL to 127.0.0.1, so the old raw URLError in a 502
            # was actively misleading. Retry a bounded number of times, then
            # return a classified error that says which side failed.
            if not _is_transient_transport_error(e) or transport_index >= len(transport_delays):
                raise UpstreamTransportError(e, transport_index + 1)
            delay = transport_delays[transport_index]
            transport_index += 1
            log_err(f"上游连接失败（{_wire_error(e)['category']}），"
                    f"重试 {transport_index}/{len(transport_delays)} after {delay:.1f}s")
            time.sleep(delay)
            continue


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def iter_sse(resp):
    """逐行产出上游 SSE 的 data 部分。"""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            return
        if chunk:
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                log("跳过无法解析的 SSE 帧:", chunk[:120])


def aggregate(events: list[dict], model: str) -> dict:
    """把 SSE 事件聚合成一个非流式 chat.completion。"""
    content, reasoning, tool_calls, finish = [], [], {}, "stop"
    cid, created, usage = "", int(time.time()), None
    for ev in events:
        cid = ev.get("id") or cid
        created = ev.get("created") or created
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    msg: dict = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return {
        "id": cid or f"wb-{int(time.time()*1000)}",
        "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage or {},
    }

# ─────────────────────────── 区域注册表 ───────────────────────────

class RegionRuntime:
    """One region's credential state plus its last failure, for /health."""

    def __init__(self, region: str) -> None:
        self.region = region
        self.spec = REGIONS[region]
        self.creds: Credentials | None = None
        self.error: str | None = None
        disabled_env = "WORKBUDDY_CN_DISABLED" if region == "cn" else "WORKBUDDY_AI_DISABLED"
        if os.environ.get(disabled_env, "").strip().lower() in ("1", "true", "yes"):
            self.error = f"已在设置里停用（{disabled_env}）"
            log_err(f"{self.spec.label}已停用（{disabled_env}）")
            return
        try:
            self.creds = Credentials(region)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log_err(f"{self.spec.label}不可用：{self.error}")

    @property
    def available(self) -> bool:
        return self.creds is not None

    @property
    def hidden(self) -> bool:
        """Hidden regions stay usable in principle but are kept out of the picker."""
        return self.region in wbsettings.hidden_regions()

    def require(self, allow_hidden: bool = False) -> Credentials:
        """Return credentials, or raise a precise reason.

        ``allow_hidden`` is for account-level endpoints (credits/status): a
        region hidden from the model picker still has a readable account.
        Model traffic must not use it, so a stale selector entry fails loudly
        instead of silently burning another region's quota.
        """
        if self.hidden and not allow_hidden:
            raise RegionUnavailable(
                self.region,
                f"{self.spec.label}已在设置里隐藏（workbuddy-settings.json → hidden_regions）；"
                f"要恢复请运行 wb-model.cmd show {self.region} 并重启代理",
            )
        if self.creds is None:
            raise RegionUnavailable(
                self.region,
                f"{self.spec.label}未就绪（{self.error}）；"
                f"请启动并登录{self.spec.label}桌面端，或设置 "
                f"{wbdiscover.REGIONS[self.region].app_env} / "
                f"{wbdiscover.REGIONS[self.region].auth_env}",
            )
        return self.creds

    def health(self) -> dict:
        c = self.creds
        if c is None:
            paths = self._health_paths()
            return {
                "available": False, "hidden": self.hidden,
                "label": self.spec.label, "error": self.error,
                "app": paths["app"],
                "auth": paths["auth"],
            }
        return {
            "available": True,
            "hidden": self.hidden,
            "label": self.spec.label,
            "uid": c.uid,
            "nickname": c.nickname,
            "enterprise_id": c.enterprise_id,
            "app_version": c.app_version,
            "token_expires_at": c.expires_at,
            "token_valid": not c.expired(),
            "app": c.app,
            "auth": c.auth_file,
        }

    def _health_paths(self) -> dict:
        """Cache /health path discovery; the watcher polls every 5 seconds.

        Desktop install/auth paths are effectively static. Cache both successful
        and failed lookups so an unavailable region never starts a fresh
        PowerShell registry probe on every health check.
        """
        now = time.monotonic()
        cached = getattr(self, "_paths_cache", None)
        if cached and now - float(cached[0]) < HEALTH_PATH_TTL_SEC:
            return dict(cached[1])
        paths = {
            "app": wbdiscover.find_app(self.region),
            "auth": wbdiscover.find_auth_file(self.region),
        }
        self._paths_cache = (now, paths)
        return dict(paths)


REGISTRY: dict[str, RegionRuntime] = {}


def _redact_error(e: BaseException) -> str:
    """Error type plus a short reason for status output.

    Upstream exceptions can embed response bodies (and those have echoed
    request material before). Only the class name and a token-free first line
    may leave the proxy; anything resembling a JSON body is reduced to its
    status, never its contents.
    """
    raw = str(e).replace("\r", " ").replace("\n", " ").strip()
    if "body=" in raw or raw.lstrip().startswith("{"):
        status = re.search(r'\b(?:HTTP\s*)?(\d{3})\b', raw)
        raw = f"上游返回 {status.group(1)}" if status else "上游返回错误响应"
        return f"{type(e).__name__}: {raw}"
    raw = re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer <redacted>', raw)
    raw = re.sub(r'(?i)(access|refresh)[_-]?token\s*[=:]\s*\S+', r'\1 token <redacted>', raw)
    if len(raw) > 120:
        raw = raw[:117] + "..."
    return f"{type(e).__name__}: {raw}" if raw else type(e).__name__


def _uid_tail(uid: str) -> str:
    """Stable, non-reversible shorthand: the last 8 characters, never the full id."""
    text = str(uid or "")
    return text[-8:] if len(text) > 8 else text


def _account_status_one(region: str, refresh: bool) -> dict:
    """One region's account + credit answer. Never raises: failures ride `error`."""
    label = REGIONS[region].label
    document = {
        "region": region,
        "label": label,
        "available": False,
        "nickname": None,
        "display_name": None,
        "uid_tail": None,
        "enterprise_id": None,
        "enterprise": False,
        "credits": None,
        "read_at": int(time.time()),
    }
    rt = REGISTRY.get(region)
    if rt is None:
        document["error"] = f"{label}未初始化"
        return document
    creds = rt.creds
    if creds is None:
        document["error"] = rt.error or f"{label}未登录"
        return document

    document["available"] = True
    nickname = str(getattr(creds, "nickname", "") or "").strip()
    uid_tail = _uid_tail(getattr(creds, "uid", ""))
    enterprise_id = str(getattr(creds, "enterprise_id", "") or "")
    document["nickname"] = nickname or None
    document["uid_tail"] = uid_tail or None
    document["display_name"] = nickname or (f"UID {uid_tail}" if uid_tail else label)
    document["enterprise_id"] = enterprise_id or None
    document["enterprise"] = enterprise_id != ""
    document["token_expires_at"] = getattr(creds, "expires_at", None)
    document["token_valid"] = not creds.expired()

    # `refresh=0` may reuse a still-fresh answer, but only for the same account.
    identity = (uid_tail, enterprise_id)
    cached = ACCOUNT_STATUS_CACHE.get(region)
    if (not refresh and cached is not None
            and cached.get("identity") == identity
            and time.time() - float(cached.get("ts") or 0) < ACCOUNT_STATUS_TTL_SEC):
        document["credits"] = cached.get("credits")
        document["read_at"] = cached.get("read_at")
        if cached.get("error"):
            document["error"] = cached["error"]
        return document

    try:
        credits = creds.credits()
    except Exception as e:
        # Deliberately no `remaining: 0` fallback: a failed read must never
        # look like an exhausted account.
        detail = _redact_error(e)
        document["error"] = detail
        ACCOUNT_STATUS_CACHE[region] = {
            "identity": identity, "ts": time.time(), "credits": None,
            "read_at": document["read_at"], "error": detail,
        }
        return document
    document["credits"] = {
        "remaining": credits.get("remaining"),
        "total": credits.get("total"),
        "unlimited": credits.get("unlimited") is True,
        "cycleResetTime": credits.get("cycleResetTime"),
    }
    ACCOUNT_STATUS_CACHE[region] = {
        "identity": identity, "ts": time.time(), "credits": document["credits"],
        "read_at": document["read_at"], "error": None,
    }
    return document


def account_status(region: str, refresh: bool = True) -> dict:
    """One region, with every field a status renderer needs."""
    if region not in REGIONS:
        raise KeyError(f"未知区域 {region}")
    return _account_status_one(region, refresh)


def account_status_document(refresh: bool = True) -> dict:
    """Both regions, always HTTP-200 shaped: one failure never hides the other."""
    return {
        "ok": True,
        "checked_at": int(time.time()),
        "refresh": bool(refresh),
        "regions": {r: _account_status_one(r, refresh) for r in REGION_ORDER},
    }


def runtime(region: str) -> RegionRuntime:
    if region not in REGISTRY:
        REGISTRY[region] = RegionRuntime(region)
    return REGISTRY[region]


def resolve_slug(slug: str) -> tuple[str, str]:
    """Map a selector slug to (region, upstream model id).

    Bare ids are the pre-1.6 international form and stay valid forever so old
    sessions and configs keep working. Only the two known prefixes are special;
    anything else is passed through to the international upstream untouched.
    """
    text = (slug or "").strip()
    if text.startswith("cn/"):
        return "cn", text[3:]
    if text.startswith("ai/"):
        return "ai", text[3:]
    return "ai", text


def build_models() -> list[dict]:
    """Merge both regions' catalogs into selector entries.

    A region listed in ``workbuddy-settings.json`` → ``hidden_regions`` stays
    signed in (health/credits still work) but is left out of the picker.
    """
    out: list[dict] = []
    hidden = wbsettings.hidden_regions()
    for region in REGION_ORDER:
        rt = REGISTRY.get(region)
        if rt is None or rt.creds is None or region in hidden:
            continue
        try:
            rows = rt.creds.catalog()
        except Exception as e:
            log_err(f"{rt.spec.label}目录读取失败：{type(e).__name__}: {e}")
            rt.error = f"catalog: {type(e).__name__}: {e}"
            continue
        for row in rows:
            levels, default = reasoning_levels(row)
            out.append({
                "slug": f"{region}/{row['id']}",
                "region": region,
                "region_label": rt.spec.label,
                "upstream_model": row["id"],
                "name": row["name"],
                "context_window": row.get("context_window"),
                "max_output_tokens": row.get("max_output_tokens"),
                "supports_images": bool(row.get("supports_images")),
                "credits": row.get("credits") or "",
                "reasoning_levels": levels,
                "default_reasoning_level": default,
            })
    return dedupe_display_names(out)


def dedupe_display_names(models: list[dict]) -> list[dict]:
    """Keep picker names identical to WorkBuddy's own list.

    The desktop app shows plain names ("Hy4 flash", "GLM-5.3"). Adding a region
    suffix to every row made the Codex picker look different from the app, so
    the suffix is now only applied where it is actually needed: two regions
    exposing the same display name.
    """
    counts: dict[str, int] = {}
    for m in models:
        counts[m["name"]] = counts.get(m["name"], 0) + 1
    if all(n == 1 for n in counts.values()):
        return models
    out: list[dict] = []
    for m in models:
        if counts[m["name"]] > 1:
            m = dict(m)
            base = m["name"]
            cross_region = len({x["region"] for x in models if x["name"] == base}) > 1
            if cross_region and m["region"] in wbdiscover.REGIONS:
                m["name"] = f'{base}（{wbdiscover.REGIONS[m["region"]].label}）'
            else:
                m["name"] = f'{base} ({m["upstream_model"]})'
        out.append(m)
    return out

# ────────────────────── Codex 目录形状与落盘 ──────────────────────

# Codex 0.16x 的 Model 结构里有、而 OpenAI 形状没有的字段。
# 缺 tool_mode / apply_patch_tool_type 时新版本会认为「没有可用工具」而发 0 个工具。
CODEX_EXTRA_FIELDS: dict = {
    "tool_mode": "parameters",
    "apply_patch_tool_type": "freeform",
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "use_responses_lite": False,
    "supports_reasoning_effort_updates": False,
    "node_repl_disabled": True,
    "include_permissions_instructions": False,
    "include_environment_context": False,
}


def codex_catalog_entries(models: list[dict]) -> list[dict]:
    """Turn proxy rows into the exact entries Codex's model picker wants.

    Codex 0.15x 的目录要求 {"models":[...]}，且条目字段名与 OpenAI 形状不同
    （slug/display_name/base_instructions…）。缺了它会报
    "Model metadata for X not found. Defaulting to fallback metadata"，
    TUI 的模型名也会一直停在 loading。
    """
    entries: list[dict] = []
    for i, m in enumerate(models):
        entry = {
            "slug": m["slug"],
            "display_name": m["name"],
            # 描述里不带区域后缀：Codex 把 description 显示在选择器副标题，
            # 和桌面端不一致会让人以为换了模型。
            "description": m["name"],
            "context_window": m["context_window"] or 200000,
            "max_output_tokens": m["max_output_tokens"] or 24000,
            "base_instructions": "default",
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": i,
            "supports_reasoning_summaries": False,
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "truncation_policy": {"mode": "bytes", "limit": 10000},
            "experimental_supported_tools": [],
            "supports_parallel_tool_calls": False,
            "input_modalities": ["text", "image"] if m["supports_images"] else ["text"],
            "default_reasoning_level": m["default_reasoning_level"],
            "supported_reasoning_levels": [
                {"effort": e, "description": e} for e in m["reasoning_levels"]
            ],
            "region": m["region"],
            "upstream_model": m["upstream_model"],
        }
        entry.update(CODEX_EXTRA_FIELDS)
        entries.append(entry)
    return entries


CATALOG_PATH = os.environ.get(
    "WB_CATALOG",
    os.path.join(os.path.dirname(CODEX_CONFIG), "workbuddy-catalog.json"),
)
_CATALOG_LOCK = threading.Lock()


def write_catalog(models: list[dict], path: str | None = None) -> bool:
    """Mirror the live roster into ``model_catalog_json``.

    Codex 桌面端只在新会话启动时读这个文件；以前只有 setup / wb-model 会写，
    上游增删模型后选择器就一直停在旧名单（Hy3 下线了还挂着）。这里在每次
    /v1/models 之后按内容哈希比对，名单变了才落盘，让选择器自动跟上。
    """
    target = path or CATALOG_PATH
    try:
        doc = {"models": codex_catalog_entries(models)}
        text = json.dumps(doc, ensure_ascii=False, indent=1)
        with _CATALOG_LOCK:
            try:
                with open(target, encoding="utf-8") as fh:
                    if fh.read() == text:
                        return False
            except OSError:
                pass
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            tmp = f"{target}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, target)
        log(f"模型目录已更新：{target}（{len(models)} 个模型）")
        return True
    except OSError as e:
        log_err(f"写模型目录失败（{target}）：{type(e).__name__}: {e}")
        return False


# ─────────────────────────── HTTP 服务 ───────────────────────────

def _log_usage(model: str, usage: dict, region: str | None = None) -> None:
    """把每次请求的 token / 积分记到 wb_usage.jsonl，便于核算成本。"""
    path = os.environ.get("WB_USAGE_LOG")
    if not path or not usage:
        return
    try:
        def g(*keys, default=None):
            for k in keys:
                if usage.get(k) is not None:
                    return usage[k]
            return default

        details_in = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        details_out = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        row = {
            "ts": int(time.time()), "model": model, "region": region,
            "in": g("prompt_tokens", "input_tokens", default=0),
            "cached": g("prompt_cache_hit_tokens", "cached_tokens", default=0)
                      or details_in.get("cached_tokens") or 0,
            "out": g("completion_tokens", "output_tokens", default=0),
            "reasoning": details_out.get("reasoning_tokens") or 0,
            "credit": g("credit"),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _analyze_diagnostics(rows: list[dict]) -> dict:
    """Group recent events into per-request verdicts for root-cause analysis."""
    requests: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        rid = str(row.get("request_id") or "-")
        if rid not in requests:
            requests[rid] = {"request_id": rid, "events": [], "errors": [], "end": None}
            order.append(rid)
        requests[rid]["events"].append(row.get("event"))
        if row.get("event") == "upstream_error":
            requests[rid]["errors"].append({k: row.get(k) for k in
                ("phase", "category", "side", "type", "message", "errno", "winerror", "elapsed_ms")})
        if row.get("event") in ("stream_end", "stream_incomplete"):
            requests[rid]["end"] = row
    result = []
    for rid in order[-min(100, len(order)):]:
        item = requests[rid]
        end = item["end"] or {}
        errors = item["errors"]
        categories = sorted({str(e.get("category")) for e in errors if e.get("category")})
        verdict = "in_progress"
        if end:
            if end.get("upstream_terminal_seen") is False:
                verdict = "upstream_closed_without_terminal_frame"
            elif end.get("response_completed_emitted"):
                verdict = "completed"
            elif errors:
                verdict = errors[-1].get("category") or "transport_error"
            elif not end.get("saw_done") and end.get("finish_reason") is None:
                verdict = "upstream_closed_without_terminal_frame"
            else:
                verdict = "stream_ended_without_response_completed"
        result.append({
            "request_id": rid,
            "verdict": verdict,
            "slug": end.get("slug"),
            "region": end.get("region"),
            "elapsed_ms": end.get("elapsed_ms"),
            "upstream_sse_frames": end.get("upstream_sse_frames"),
            "upstream_data_frames": end.get("upstream_data_frames"),
            "unparsed_data_frames": end.get("unparsed_data_frames"),
            "saw_done": end.get("saw_done"),
            "finish_reason": end.get("finish_reason"),
            "response_completed_emitted": end.get("response_completed_emitted"),
            "error_categories": categories,
            "errors": errors[-3:],
            "events": item["events"][:12],
        })
    return {"ok": True, "log_path": str(_diag_path()), "requests": result}


def _safe_responses_events(chat_events, slug: str, diag: "StreamDiagnostics"):
    """Guarantee a terminal Responses event even if translation raises."""
    completed = False
    try:
        for name, ev in wbresponses.stream_responses_events(chat_events, slug):
            if name == "response.completed":
                completed = True
                response = ev.get("response") or {}
                if response.get("status") != "failed" and not diag.upstream_terminal_seen():
                    response["status"] = "failed"
                    response["error"] = {
                        "code": "upstream_closed_without_terminal_frame",
                        "message": "上游 Chat SSE 在未发送 finish_reason 或 [DONE] 的情况下结束",
                        "type": "upstream_error",
                    }
                    response["incomplete_details"] = {"reason": "upstream_error"}
                if response.get("status") == "failed":
                    error = response.get("error") or {
                        "code": "upstream_transport_error",
                        "message": ("上游流中途断开：" +
                                    str((diag.error or {}).get("category") or "unknown")),
                        "type": "upstream_error",
                    }
                    response["error"] = error
                    response["incomplete_details"] = {"reason": "upstream_error"}
                    yield ("response.failed", {"type": "response.failed",
                                               "response": response, "error": error})
            yield name, ev
    except GeneratorExit:
        raise
    except Exception as exc:
        diag.upstream_error(exc, phase="translate")
    if not completed:
        message = "上游 Responses 流中断：" + (str((diag.error or {}).get("message"))
                                             if diag.error else "未收到终止帧")
        for name, ev in _responses_terminal_error(slug, message):
            if name == "response.completed":
                completed = True
            yield name, ev
    diag.completed = completed


def _responses_terminal_error(slug: str, message: str, code: str = "upstream_transport_error") -> list[tuple[str, dict]]:
    """Terminal Responses events for a stream that cannot recover.

    Codex treats a connection closed before `response.completed` as the generic
    `stream closed before response.completed`. Emitting a failed terminal event
    gives it a real error to display and preserves the SSE terminal invariant.
    """
    rid = f"resp_{uuid.uuid4().hex}"
    response = {
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": "failed", "model": slug, "output": [],
        "usage": wbresponses.empty_usage(),
        "incomplete_details": {"reason": "upstream_error"},
        "error": {"code": code, "message": message, "type": "upstream_error"},
    }
    return [
        ("response.failed", {"type": "response.failed", "response": response,
                             "error": response["error"]}),
        ("response.completed", {"type": "response.completed", "response": response}),
    ]


class _Server(ThreadingHTTPServer):
    """只做一件事：客户端断线不打堆栈。

    断线是常态——用户取消、Codex 在 429 重试风暴里放弃、桌面端切会话——
    默认 handle_error 会把整片 traceback 刷到 stderr，把真正的错误埋掉。

    Windows 上必须独占端口：SO_REUSEADDR 在那里允许第二个进程绑同一端口，
    实测会留下三个代理同时监听 8787，请求随机落到不同账号。
    """
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionError, OSError)):
            log(f"连接中断（{et.__name__}），忽略")
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        log(fmt % args)

    # ── 工具 ──
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError) as e:      # 客户端已断开（如 429 重试途中取消）
            log(f"响应写入失败，客户端已断开（{type(e).__name__}）")

    def _err(self, status, message, code="upstream_error", **extra):
        payload = {"error": {"message": message, "type": code, "code": code, **extra}}
        self._json(payload, status)

    def _upstream_error(self, err: UpstreamError) -> None:
        """Render a classified upstream error for Codex.

        6004 is an account-level condition, not a transient transport failure.
        Returning 400 prevents Codex from spending its own retry budget on a
        request that cannot succeed until the upstream reset window passes.
        14003 is handled by bounded retries in open_upstream(). If it still
        fails, keep 429 so the client can report a real rate-limit status.
        """
        summary = upstream_error_text(err.detail)
        code = _rate_limit_code(err.detail)
        if err.status == 429 and code == RATE_LIMIT_ACCOUNT_QUOTA:
            message = f"{_rate_limit_message(err.status, err.detail)} Upstream: {summary}"
            self._json(
                {"error": {"message": message, "type": "rate_limit_account", "code": 6004}},
                400,
            )
            return
        if err.status == 429 and code == RATE_LIMIT_MODEL_BUSY:
            message = f"{_rate_limit_message(err.status, err.detail)} Upstream: {summary}"
            self._json(
                {"error": {"message": message, "type": "rate_limit_model", "code": 14003}},
                429,
            )
            return
        self._err(err.status, err.detail)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _resolve(self, slug: str):
        region, upstream = resolve_slug(slug)
        rt = runtime(region)
        return rt, region, upstream

    # ── 路由 ──
    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/health", "/"):
                hidden = wbsettings.hidden_regions()
                mirror = LAST_MODEL.get("region")
                if not mirror or mirror in hidden:
                    mirror = next(
                        (r for r in REGION_ORDER
                         if r not in hidden and REGISTRY.get(r) and REGISTRY[r].creds),
                        next((r for r in REGION_ORDER if r not in hidden), REGION_ORDER[0]),
                    )
                base = REGISTRY.get(mirror) or runtime(mirror)
                c = base.creds
                self._json({
                    "ok": True,
                    "provider": "workbuddy-ai",
                    "region": mirror,
                    "uid": c.uid if c else None,
                    "app_version": c.app_version if c else None,
                    "token_expires_at": c.expires_at if c else None,
                    "token_valid": (not c.expired()) if c else False,
                    "last_model": LAST_MODEL.get("model"),
                    "last_model_ts": LAST_MODEL.get("ts"),
                    "requests_seen": LAST_MODEL.get("count", 0),
                    "regions": {r: runtime(r).health() for r in REGION_ORDER},
                    "pid": os.getpid(),
                    "version": PLUGIN_VERSION,
                    "root": str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "stream_diagnostics": _diagnostics_summary(recent_diagnostics(300)),
                })
            elif path == "/wb/stream-diagnostics":
                try:
                    limit = int(next((p.split("=", 1)[1] for p in query.split("&")
                                      if p.startswith("limit=")), "200"))
                except ValueError:
                    limit = 200
                self._json(_analyze_diagnostics(recent_diagnostics(limit)))
            elif path == "/wb/last-model":
                # 实际生效的模型只能由这里判定：代理是唯一看到真实 model 字段的一方。
                # Codex 会话内切换模型不会写回 config.toml，读配置必然读到旧的默认值。
                d = dict(LAST_MODEL)
                d["config_model"] = _config_model()
                d["config_provider"] = _config_provider()
                d["config_synced"] = (
                    d["config_provider"] == "workbuddy"
                    and d["config_model"] == LAST_MODEL.get("model")
                )
                d["follow_last_model"] = FOLLOW_LAST_MODEL
                d["config_path"] = CODEX_CONFIG
                d["profile_path"] = PROFILE_CONFIG
                self._json(d)
            elif path == "/v1/models":
                models = build_models()
                data = [
                    {"id": m["slug"], "object": "model", "created": 0,
                     "owned_by": f"workbuddy-{m['region']}",
                     "name": m["name"],
                     "region": m["region"],
                     "upstream_model": m["upstream_model"],
                     "credits": m["credits"],
                     "context_window": m["context_window"],
                     "max_output_tokens": m["max_output_tokens"],
                     "supports_images": m["supports_images"]}
                    for m in models
                ]
                codex_models = codex_catalog_entries(models)
                # 目录文件跟着实时名单走，Codex 新建会话时就能看到最新模型：
                # 上游下线/改名/新增都不必重跑 setup（旧名单里还挂着已下线的 Hy3）。
                write_catalog(models)
                self._json({"object": "list", "data": data, "models": codex_models,
                            "hidden_regions": sorted(wbsettings.hidden_regions())})
            elif path == "/wb/credits":
                region = "ai"
                for part in query.split("&"):
                    if part.startswith("region="):
                        region = part.split("=", 1)[1] or "ai"
                self._json(runtime(region).require(allow_hidden=True).credits())
            elif path == "/wb/account-status":
                refresh = True
                for part in query.split("&"):
                    if part in ("refresh=0", "refresh=false"):
                        refresh = False
                self._json(account_status_document(refresh=refresh))
            else:
                self._err(404, f"未知路径 {path}", "not_found")
        except RegionUnavailable as e:
            self._err(400, e.detail, "region_unavailable", region=e.region)
        except UpstreamError as e:
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            self._err(e.status, e.detail)
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/responses":
            self._responses()
            return
        if path not in ("/v1/chat/completions", "/v1/completions"):
            self._err(404, f"未知路径 {path}", "not_found")
            return
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"请求体不是合法 JSON: {e}", "invalid_request")
            return

        slug = payload.get("model") or "ai/default-model"
        try:
            rt, region, upstream = self._resolve(slug)
            creds = rt.require()
        except RegionUnavailable as e:
            self._err(400, e.detail, "region_unavailable", region=e.region)
            return
        payload["model"] = upstream
        want_stream = bool(payload.get("stream"))
        if os.environ.get("WB_DUMP"):
            try:
                d = dict(payload)
                m = d.get("messages") or []
                for msg in m:
                    c = msg.get("content")
                    if isinstance(c, str) and len(c) > 160:
                        msg["content"] = c[:160] + f"...<{len(c)} 字>"
                if d.get("tools"):
                    d["tools"] = [f"<{len(d['tools'])} 个工具>"]
                with open(os.environ["WB_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d, ensure_ascii=False)[:4000] + "\n")
            except Exception:
                pass
        log("请求 region=%s model=%s stream=%s 顶层字段=%s" % (
            region, upstream, want_stream, sorted(k for k in payload if k != "messages")))
        log("消息数=%d 首条 role=%s" % (
            len(payload.get("messages") or []),
            (payload.get("messages") or [{}])[0].get("role")))

        try:
            resp, _ = open_upstream(creds, payload)
        except UpstreamError as e:
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            self._upstream_error(e)
            return
        except UpstreamTransportError as e:
            info = _wire_error(e.exc, phase="open")
            message = f"上游连接失败（{info['category']}），已重试 {e.attempts} 次；不是本地 8787 代理故障。"
            log_err(f"上游连接失败（chat）: {message}")
            self._json({"error": {"message": message, "type": "upstream_transport_error",
                                  "code": info["category"], "side": info["side"]}}, 502)
            return
        except Exception as e:
            log_err(f"代理内部错误（chat）: {type(e).__name__}: {str(e)[:300]}")
            self._err(502, f"代理内部错误: {type(e).__name__}: {e}")
            return
        _note_model(f"{region}/{upstream}", upstream, region)

        rid = f"r-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
        diag2 = StreamDiagnostics(rid, endpoint="/v1/chat/completions", slug=slug,
                                  region=region, upstream_model=upstream,
                                  payload=payload)
        events = iter_sse(diag2.wrap(resp))
        if want_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for ev in events:
                    if ev.get("usage"):
                        _log_usage(slug, ev["usage"], region)
                    out = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {out}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                diag2.end(completed=True)
            # Windows 上客户端取消/断开会抛 ConnectionAbortedError(WinError 10053)。
            except (ConnectionError, OSError) as e:
                diag2.upstream_error(e, phase="write")
                diag2.end(completed=False, error=e, phase="write")
                log(f"客户端断开（{type(e).__name__}）")
            finally:
                resp.close()
            return

        # 非流式：本地聚合
        try:
            chat_events = list(events)
        finally:
            resp.close()
        agg = aggregate(chat_events, slug)
        _log_usage(slug, agg.get("usage") or {}, region)
        diag2.end(completed=True)
        self._json(agg)

    def do_PUT(self):
        self.do_POST()

    # ── Responses API（Codex 0.153+ 唯一支持的 wire_api）──
    def _responses(self) -> None:
        request_id = f"r-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"请求体不是合法 JSON: {e}", "invalid_request")
            return

        slug = payload.get("model") or "ai/default-model"
        try:
            rt, region, upstream = self._resolve(slug)
            creds = rt.require()
        except RegionUnavailable as e:
            self._err(400, e.detail, "region_unavailable", region=e.region)
            return
        payload["model"] = upstream
        want_stream = payload.get("stream", True) is not False
        if os.environ.get("WB_DUMP"):
            try:
                d = dict(payload)
                d["input"] = f"<{len(payload.get('input') or [])} items>"
                if d.get("tools"):
                    d["tools"] = f"<{len(d['tools'])} 个工具>"
                with open(os.environ["WB_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write("[responses] " + json.dumps(d, ensure_ascii=False)[:3000] + "\n")
            except Exception:
                pass

        try:
            chat_payload = wbresponses.request_to_chat(payload)
        except Exception as e:
            self._err(400, f"请求转换失败: {type(e).__name__}: {e}", "invalid_request")
            return
        log("responses→chat region=%s model=%s 消息数=%d 工具=%d stream=%s" % (
            region, upstream, len(chat_payload["messages"]),
            len(chat_payload.get("tools") or []), want_stream))
        if os.environ.get("WB_DUMP"):
            try:
                roles = [(m.get("role"), len(str(m.get("content") or ""))) for m in chat_payload["messages"]]
                extra = sorted(k for k in chat_payload if k not in ("model", "messages", "stream"))
                with open(os.environ["WB_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write("[chat] keys=%s roles=%s\n" % (extra, roles[:12]))
            except Exception:
                pass
        if os.environ.get("WB_DUMP_CHAT"):
            try:
                with open(os.environ["WB_DUMP_CHAT"], "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(chat_payload, ensure_ascii=False))
            except Exception:
                pass

        diag = StreamDiagnostics(request_id, endpoint="/v1/responses", slug=slug,
                                 region=region, upstream_model=upstream,
                                 payload=payload, chat_payload=chat_payload)
        try:
            resp, _ = open_upstream(creds, chat_payload)
        except UpstreamError as e:
            diag.upstream_error(e, phase="open_http")
            diag.end(completed=False, error=e, phase="open")
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self._write_responses_events(_responses_terminal_error(
                        slug, upstream_error_text(e.detail), "upstream_http_error"))
                except (ConnectionError, OSError):
                    pass
                return
            self._upstream_error(e)
            return
        except UpstreamTransportError as e:
            diag.upstream_error(e.exc, phase="open")
            diag.end(completed=False, error=e.exc, phase="open")
            info = _wire_error(e.exc, phase="open")
            message = (f"上游连接失败（{info['category']}），已重试 {e.attempts} 次；"
                       f"这不是本地 8787 代理故障。")
            log_err(f"上游连接失败: {message}")
            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self._write_responses_events(_responses_terminal_error(slug, message))
                except (ConnectionError, OSError):
                    pass
                return
            payload = {"error": {"message": message, "type": "upstream_transport_error",
                                  "code": info["category"], "side": info["side"],
                                  "error_category": info["category"]}}
            self._json(payload, 502)
            return
        except Exception as e:
            diag.upstream_error(e, phase="open")
            diag.end(completed=False, error=e, phase="open")
            log_err(f"代理内部错误（responses）: {type(e).__name__}: {str(e)[:300]}")
            self._err(502, f"代理内部错误: {type(e).__name__}: {e}")
            return
        _note_model(f"{region}/{upstream}", upstream, region)

        upstream_events = iter_sse(diag.wrap(resp))
        events = _safe_responses_events(upstream_events, slug, diag)
        if not want_stream:
            try:
                final = None
                for name, ev in events:
                    if name == "response.completed":
                        final = ev["response"]
                if not final:
                    final = wbresponses.aggregate_response([], slug)
                _log_usage(slug, final.get("usage") or {}, region)
                diag.end(completed=diag.completed)
                self._json(final)
            except Exception as e:
                diag.upstream_error(e, phase="aggregate")
                diag.end(completed=False, error=e, phase="aggregate")
                self._err(502, f"响应聚合失败: {type(e).__name__}: {e}")
            finally:
                resp.close()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._write_responses_events(events, on_completed=lambda ev: _log_usage(
                slug, ev["response"].get("usage") or {}, region))
            diag.end(completed=diag.completed)
        except (ConnectionError, OSError) as e:
            diag.upstream_error(e, phase="write")
            diag.end(completed=diag.completed, error=e, phase="write")
            log(f"客户端断开（{type(e).__name__}）")
        finally:
            resp.close()

    def _write_responses_events(self, events, on_completed=None) -> None:
        for name, ev in events:
            if name == "response.completed" and on_completed:
                on_completed(ev)
            body = json.dumps(ev, ensure_ascii=False)
            self.wfile.write(f"event: {name}\ndata: {body}\n\n".encode("utf-8"))
            self.wfile.flush()

# ─────────────────────────── 启动 ───────────────────────────

def _listening_pids(port: int) -> list[str]:
    try:
        r = wbspawn.run_hidden(["netstat", "-ano", "-p", "TCP"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[str] = []
    needle = f":{port}"
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(needle) or parts[3].upper() != "LISTENING":
            continue
        if parts[4] not in pids:
            pids.append(parts[4])
    return pids


def _existing_proxy(port: int) -> dict | None:
    """Return the health document if the listener is already this proxy."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(doc, dict) and doc.get("provider") in ("workbuddy", "workbuddy-ai"):
            return doc
    except Exception:
        pass
    return None


def main() -> int:
    global _VERBOSE
    wbdiscover.console_encodings()
    configure_upstream_network()
    ap = argparse.ArgumentParser(description="WorkBuddy（国际版+国内版）→ OpenAI 兼容本地代理")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _VERBOSE = args.verbose

    # 预检：把「缺什么、怎么补」直接说清楚，别让新用户对着一条含糊报错猜。
    py = sys.executable
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        print(f"缺少依赖 cryptography（当前解释器：{py}）\n"
              f"请安装：\"{py}\" -m pip install cryptography", file=sys.stderr)
        return 3

    if _listening_pids(args.port):
        existing = _existing_proxy(args.port)
        if existing is not None:
            print(f"代理已在运行：pid={existing.get('pid')} "
                  f"http://{args.host}:{args.port}/v1（无需重复启动）", file=sys.stderr)
            return 0
        pids = ", ".join(_listening_pids(args.port))
        print(f"端口 {args.port} 已被其它程序占用（PID {pids}）；"
              f"请先关闭它，或改用 --port。", file=sys.stderr)
        return 2

    for region in REGION_ORDER:
        REGISTRY[region] = RegionRuntime(region)

    usable = [r for r in REGION_ORDER if REGISTRY[r].available]
    if not usable:
        print("两个 WorkBuddy 区域都不可用：", file=sys.stderr)
        for region in REGION_ORDER:
            rt = REGISTRY[region]
            spec = wbdiscover.REGIONS[region]
            print(f"  - {spec.label}：{rt.error}", file=sys.stderr)
            print(f"    app={wbdiscover.find_app(region)}", file=sys.stderr)
            print(f"    auth={wbdiscover.find_auth_file(region)}", file=sys.stderr)
            print(f"    可设置 {spec.app_env} / {spec.auth_env}", file=sys.stderr)
        return 2

    for region in REGION_ORDER:
        rt = REGISTRY[region]
        if rt.available:
            c = rt.creds
            print(f"{REGIONS[region].label}：{c.app}  ({c.app_version})", file=sys.stderr)
            print(f"  凭据：{c.auth_file}", file=sys.stderr)
        else:
            print(f"{REGIONS[region].label}：未启用（{rt.error}）", file=sys.stderr)

    print(f"就绪 → http://{args.host}:{args.port}/v1   "
          f"（模型列表 /v1/models，状态 /health，额度 /wb/credits?region=ai|cn）",
          file=sys.stderr)

    # 启动时后台拉一次上游目录并落盘：这样即使没人开 Codex，选择器也不会停在
    # 上一次的名单（Hy3 下线后仍在列表里挂了很久）。失败只记日志，不影响启动。
    def _warm_catalog() -> None:
        try:
            write_catalog(build_models())
        except Exception as e:  # noqa: BLE001 - 预热失败不该拖垮代理
            log_err(f"预热模型目录失败：{type(e).__name__}: {e}")

    threading.Thread(target=_warm_catalog, name="wb-catalog-warmup", daemon=True).start()
    try:
        srv = _Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"端口 {args.port} 绑定失败：{e}", file=sys.stderr)
        return 2
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
