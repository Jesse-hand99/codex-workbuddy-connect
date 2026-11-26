---
description: 诊断 WorkBuddy 代理的流中断、502 与 response.completed 缺失
argument-hint: "[--limit 200]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# WorkBuddy 流中断诊断

用户参数：`$ARGUMENTS`

## 必须现场取数

```powershell
curl.exe --noproxy "*" "http://127.0.0.1:8787/wb/stream-diagnostics?limit=200"
curl.exe --noproxy "*" "http://127.0.0.1:8787/health"
```

诊断 JSONL 的实际路径见 `/health` 的 `stream_diagnostics.log_path`；默认：

```text
%USERPROFILE%\.codex\workbuddy-connect\wbproxy_events.jsonl
```

不要凭截图或上下文猜原因。代理只记录请求形状、SSE 帧计数、终止帧、错误类别与脱敏帧摘要，
不记录完整提示词、工具参数、令牌或 Cookie。

## 判定表

| verdict / category | 事实依据 | 结论 |
|---|---|---|
| `upstream_reset` (10054) | `upstream_error` 在 `open` 或 `read` 阶段；`side=upstream` | 上游 TCP 被重置，不是 localhost 8787 故障 |
| `dns_resolution_failed` (11001) | `phase=open`、`side=upstream`、`getaddrinfo failed` | 上游域名解析失败；检查 DNS / 网络 / 代理变量 |
| `upstream_timeout` (10060) | 建连或读流超时；`saw_done=false` | 上游 TCP/CDN 长时间无响应 |
| `upstream_tls_eof` | `SSL: UNEXPECTED_EOF_WHILE_READING` | TLS 连接被上游提前关闭 |
| `upstream_closed_without_terminal_frame` | `upstream_terminal_seen=false` | 上游 Chat SSE 没有 `[DONE]` 或 `finish_reason`；代理会补发 `response.failed` + `response.completed`，不会伪装成正常完成 |
| `client_disconnect` | `phase=write`、`side=downstream` | Codex 侧主动断开；不是上游错误 |
| `completed` | `response_completed_emitted=true` | 正常终止；结合 `elapsed_ms` 看是否超时 |

关键字段：

- `request_id`：把 `request_start`、`upstream_open`、`upstream_error`、`stream_end` 串起来。
- `upstream_sse_frames` / `upstream_data_frames` / `unparsed_data_frames`：判断上游发了多少、是否可疑 JSON 帧。
- `saw_done` / `finish_reason` / `last_finish_age_ms`：判断上游是否给了 Chat 层终止信号。
- `response_completed_emitted`：判断是否向 Codex 发出了 Responses 层终止事件。
- `excerpts`：前 40 帧的形状（内容长度、工具名、finish_reason），不含 delta 文本。

## 报告给用户时

1. 先报 `verdict` 与时间、模型、区域，再报 `error_categories` 计数。
2. 区分三层：Codex → 本地代理 → WorkBuddy 上游。`url: http://127.0.0.1:8787/v1/responses` 只是 Codex 的本地请求地址
   （旧配置可能显示 `localhost`；新版安装器固定使用回环 IP 以避免 `11001 getaddrinfo failed`），
   `URLError` 的 `side=upstream` 才说明真正失败点。
3. 如果仍有 `stream closed before response.completed`，核对那个请求是否 `response_completed_emitted=false`；
   升级代理后应不再出现（代理会补发 terminal events）。
4. 需要原始记录时读取 `wbproxy_events.jsonl`，不要公开完整请求 dump。
