# WorkBuddy × Codex 接入审查与 1.5.5 验收记录

日期：2026-10-20

工作区：`D:\dev\plugs`
修改对象：`codex-workbuddy-connect`（`dsh-workbuddy-connect` 仅作只读参照）

## 1. 结论摘要

- DSH 的插件机制可以直接注册模型 provider；Codex plugin 清单没有同等的 provider
  注册字段，Codex 侧必须使用 `[model_providers.*]` 配置加本地 Responses 代理。
- 1.5.4 调用失败的确定性根因是上游 `400 / code 11128 / request illegal`：chat 请求缺少
  `X-Conversation-ID`、`X-Conversation-Request-ID`、`X-Conversation-Message-ID`。
- 1.5.4 另有一个模型目录问题：安装器生成了 `workbuddy-catalog.json`，但没有把
  `model_catalog_json` 写入 `wb.config.toml`，Codex 会回退到默认模型元数据。
- 本机测试还发现：失效的 `HTTP(S)_PROXY` 会导致 Codex 对本地 provider 返回 502；
  清除代理变量后请求才能到达 127.0.0.1:8787。`NO_PROXY=*` 在当前 Codex 客户端上
  不能替代清除代理变量。
- 1.5.5 已通过免费模型流式/非流式、工具续轮、模型切换、错误路径和一次收费模型调用。

## 2. 机制对比

| 环节 | dsh-workbuddy-connect | codex-workbuddy-connect 1.5.4 |
|---|---|---|
| 安装/注册 | `package.json` 的 `dsh.bundle.patch` 引入 `cordis.patch.yml`，再插入 `llm-workbuddy` | Codex marketplace 复制插件；`setup.py` 写 provider/profile |
| Provider 暴露 | `src/index.ts` 的 `ctx.llm.registerAdapter([id], adapter)` | Codex 内置 `[model_providers.workbuddy]`，插件本身不注册 provider |
| 请求协议 | pi-ai 直接走 OpenAI Chat Completions | Codex 只发 Responses，代理用 `wbresponses.py` 转成 Chat |
| 认证 | 桌面端凭据 -> 解密 -> OAuth access token | 相同，但凭据逻辑由 Python 代理实现 |
| 上游 chat 头 | `src/upstream.ts` 的 `chatHeaders()` 带三项 `X-Conversation-*` | 1.5.4 缺失；1.5.5 已补齐 |
| 工具选择 | `prepareChatBody()` 把对象形式压平为字符串 | 1.5.5 在 `wbresponses.py` 中补齐同样规则 |
| 模型目录 | 动态 catalog + provider 适配器返回模型 | 本地 `/v1/models` + `workbuddy-catalog.json` |
| 流式 | 上游 SSE 直接透传 | 上游 Chat SSE 回译成 Responses SSE |
| 错误映射 | 分类 auth/credit/rate/server/client | 保留上游状态码，Codex 对 429 做退避重试 |
| 本地鉴权 | shim 共享 bearer，校验 loopback Host/Origin | 代理绑定 loopback，但本地请求不鉴权 |

关键源码位置：

- `dsh-workbuddy-connect/src/index.ts:616`：`registerAdapter`
- `dsh-workbuddy-connect/src/adapter.ts:326`：provider/adapter 构造
- `dsh-workbuddy-connect/src/upstream.ts:456`：三项会话头
- `dsh-workbuddy-connect/src/upstream.ts:557`：Chat 请求归一化
- `codex-workbuddy-connect/plugins/codex-workbuddy-connect/bin/wbproxy.py`：
  `Credentials.chat_headers()`、`conversation_headers()`
- `codex-workbuddy-connect/plugins/codex-workbuddy-connect/bin/wbresponses.py`：
  Responses→Chat 与工具调用转换

## 3. 根因证据

### 3.1 上游 11128

1.5.4 代理日志中的脱敏摘要：

```text
[wbproxy] 上游错误 400: code=11128 / request illegal
这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。
```

受控实验：

```text
旧 UA，无会话头                  -> HTTP 400 / code 11128
旧 UA，仅加三项 X-Conversation-* -> HTTP 200 / text/event-stream
```

因此 1.5.5 只补三项会话头，保留原 UA，避免无关行为变化。

### 3.2 模型目录未绑定

1.5.4 生成的 `wb.config.toml` 只有 provider/model/effort，没有
`model_catalog_json`。Codex 0.163 启动时提示：

```text
WARN codex_models_manager::model_info: Unknown model deepseek-v4.1-flash is used.
WARNING: Model metadata for `deepseek-v4.1-flash` not found.
```

显式绑定 `workbuddy-catalog.json` 后，该警告消失，模型目录返回 28 个模型。

### 3.3 本地代理环境导致 502

本机父进程环境包含：

```text
HTTP_PROXY=http://127.0.0.1:9
HTTPS_PROXY=http://127.0.0.1:9
ALL_PROXY=http://127.0.0.1:9
```

Codex 对 `http://127.0.0.1:8787/v1/responses` 返回 502，且请求没有到达代理。
清除上述变量后请求到达代理。`NO_PROXY=*` 未解决该问题；显式设置
`NO_PROXY=127.0.0.1,localhost,::1` 并使用修复后的启动器后成功。

## 4. 1.5.5 变更

- `wbproxy.py`：chat 请求生成三项会话 UUID；额度请求使用独立账户头，不带会话 UUID；
  未预期异常写入脱敏日志；
  代理自身默认忽略继承的失效代理变量（`WB_KEEP_PROXY=1` 可保留）。
- `wbresponses.py`：`tool_choice` 对象压平为上游接受的字符串；`none` 移除工具定义。
- `setup.py`：`wb.config.toml` 写入独立 `model_catalog_json`；status 检查目录引用和
  profile 模型是否存在于目录。
- `scripts/codex-wb.cmd` / `start-wbproxy.cmd`：默认清理继承的代理变量；保持 GBK + CRLF。
- `.codex-plugin/plugin.json`：1.5.4 → 1.5.5。
- 新增 `tests/test_workbuddy_connect.py`，覆盖会话头、错误脱敏、工具转换和 profile 目录。

## 5. 测试环境

| 项 | 值 |
|---|---|
| OS | Windows 11 Professional，10.0.26300 |
| Codex CLI | 0.163.0-alpha.10.2 |
| 插件 | codex-workbuddy-connect 1.5.5 |
| WorkBuddy | 桌面端已登录，应用版本 37.10.3-42 |
| 代理 | `http://127.0.0.1:8787/v1` |
| 测试 profile | `wb` |
| 免费模型 | `deepseek-v4.1-flash`、`hy3` |
| 收费模型 | `deepseek-v4.1-flash-sg`（x0.03，实时目录最低价） |

## 6. 测试结果

| 测试 | 命令/路径 | 结果 |
|---|---|---|
| 单元测试 | `python -m unittest discover -s tests -v` | 6/6 通过 |
| 插件安装 | `codex plugin add codex-workbuddy-connect@workbuddy` | installed/enabled，1.5.5 |
| 模型目录 | `GET /v1/models` | 28 个模型 |
| 非流式 Responses | `POST /v1/responses`，`stream:false` | 200，`WB_NONSTREAM_OK`，credit=0 |
| Codex 免费流式 | `codex exec --profile wb` | `WB_OK`，provider=workbuddy |
| 启动器 | `scripts\codex-wb.cmd exec` | `WB_LAUNCHER_OK` |
| 已安装缓存复验 | 从 `plugins/cache/.../1.5.5` 运行启动器 | `WB_INSTALLED_OK` |
| 工具续轮 | shell 执行 `Write-Output WB_TOOL_OK` | 工具成功，最终 `WB_TOOL_OK` |
| 模型切换 | `-m hy3` | `WB_HY3_OK` |
| 无效模型 | `POST /v1/responses`，伪造 model | 400 / code 11102，可读错误 |
| 缺失凭据 | 临时不存在凭据路径 | 退出码 2，提示重新登录 |
| 收费调用 | `-m deepseek-v4.1-flash-sg`，禁用重试 | `PAID_OK`，退出码 0 |
| 日志脱敏 | 扫描 `wbproxy.log` / `wbproxy_watch.log` | 未发现 Bearer、accessToken、refreshToken、Cookie |

最终安装状态：

```text
provider 段已安装    : True
profile 文件        : True
模型目录            : True
profile 引用目录    : True
目录含 profile 模型 : True (deepseek-v4.1-flash)
代理 8787 在跑       : True
插件激活             : True
```

收费调用前后 `/wb/credits` 均为 407.9，两位小数下增量为 0.00。该次调用确实使用了
非零倍率模型并成功返回；余额未显示变化可能是小额计费不足 0.01 或账单延迟，未做
额外收费调用去追查。

## 7. 未验证与限制

- 429 限流未主动制造；不通过高频请求验证。
- 600 秒上游超时未做真实等待测试；仅确认现有超时与错误路径存在。
- Codex 桌面端模型选择器已实际加载 WorkBuddy 模型并完成切换；桌面端最终请求链路
  以 `/wb/last-model` 为准，未保留可公开的界面截图。
- 官方 OpenAI 文档站点在当前网络返回 403；Codex 机制以本机 CLI 帮助、配置和实测为准。
- 本地代理绑定 127.0.0.1 但不校验本地 bearer，这是现有兼容性行为；同机任意进程可调用
  本地代理，属于残余风险。
- 上游是私有接口，11128、429、模型目录和计费规则可能再次变化。

## 8. 回滚与安全

- 1.5.4 源码快照：`D:\dev\plugs\.artifacts\codex-workbuddy-connect-1.5.4-20261020-014045`
- 安装器已在 `%USERPROFILE%\.codex` 生成 `config.toml`、`wb.config.toml` 和
  `AGENTS.md` 的 `.bak-cwbc-*` 备份。
- 回滚：停止本插件代理/看护的精确 PID，恢复上述源码与配置备份，重装 1.5.4。
- 不保存原始凭据、Authorization 或完整 HTTP dump；测试 dump 仅在临时目录使用。
