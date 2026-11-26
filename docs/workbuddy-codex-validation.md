# WorkBuddy × Codex 接入审查与 1.7.0（账号与积分）验收记录

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
- 1.6.0 在同一个代理、同一个 8787 端口上接入国际版与国内版：CN 的 `/v3/config`
  用 CLI UA 可拿到 20 个 CLI 模型，`/v2/chat/completions` 返回 200，`tool_choice`
  扁平化后工具调用正常；Codex 会把 `cn/...` 原样透传给本地代理。
- 1.7.0 增加账号与积分视图。Codex 插件清单只开放展示元数据、skills、MCP 与 hooks，
  没有插件设置卡片或聊天输入框插槽，因此不试图复刻 dsh 的界面，改为
  `/wb/account-status` + `setup.py status` 两个入口。两区各自读取自己的余额，
  一个区域失败不影响另一个区域。

## 2. 机制对比

| 环节 | dsh-workbuddy-connect | codex-workbuddy-connect（1.5.4 基线） |
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

## 4. 1.5.5 / 1.6.0 变更

- `wbproxy.py`：chat 请求生成三项会话 UUID；额度请求使用独立账户头，不带会话 UUID；
  未预期异常写入脱敏日志；
  代理自身默认忽略继承的失效代理变量（`WB_KEEP_PROXY=1` 可保留）。
- `wbresponses.py`：`tool_choice` 对象压平为上游接受的字符串；`none` 移除工具定义。
- `setup.py`：`wb.config.toml` 写入独立 `model_catalog_json`；status 检查目录引用和
  profile 模型是否存在于目录。
- `scripts/codex-wb.cmd` / `start-wbproxy.cmd`：默认清理继承的代理变量；保持 GBK + CRLF。
- `.codex-plugin/plugin.json`：1.5.4 → 1.5.5。
- `wbproxy.py` / `gen-catalog.py`：保留上游 `supportsImages` 与 `disabledMultimodal`，把支持图片的模型
  声明为 `input_modalities: ["text", "image"]`；未知能力仍默认纯文本。
- 新增 `tests/test_workbuddy_connect.py`，覆盖会话头、错误脱敏、工具转换、profile 目录和图片能力传播。
- 1.6.0：`wbdiscover.py` 按区域发现两个桌面端（`WorkBuddyAI.exe` / `WorkBuddy.exe`）；
  `wbproxy.py` 按 `ai/` / `cn/` 前缀路由、合并目录、按区域隔离凭据与刷新锁；
  `gen-catalog.py` 生成带区域后缀的合并目录；新增 `wb-model.py` / `wb-model.cmd`；
  `setup.py` 在重装前停止旧 `wbproxy` 进程，并在 Windows 上关闭地址复用。
- 1.6.0：`WB_FOLLOW_LAST_MODEL`（默认开）取代 `WB_SYNC_CONFIG`，同步 `config.toml`
  与 `wb.config.toml`，只改顶层 `model` 行并保留 CRLF。
- 1.6.0：`/wb/last-model` 的更新移到上游成功打开响应之后；429/6021 等失败不会改写
  「实际在用」与默认模型。

## 5. 测试环境

| 项 | 值 |
|---|---|
| OS | Windows 11 Professional，10.0.26300 |
| Codex CLI | 0.163.0-alpha.10.2 |
| 插件 | codex-workbuddy-connect 1.6.0 |
| WorkBuddy | 国际版 5.8.4（`WorkBuddyAI.exe`）与国内版 5.8.3（`WorkBuddy.exe`）均已登录；上游报告版本 37.10.3-42 |
| 代理 | `http://127.0.0.1:8787/v1` |
| 测试 profile | `wb` |
| 免费模型 | `ai/deepseek-v4.1-flash`、`ai/hy3`、`cn/hy3` |
| 收费模型 | `ai/deepseek-v4.1-flash-sg`（x0.03，实时目录最低价） |

## 6. 测试结果

| 测试 | 命令/路径 | 结果 |
|---|---|---|
| 单元测试 | `python -m unittest discover -s tests -v` | 51/51 通过（1.7.0） |
| 插件安装 | `codex plugin add codex-workbuddy-connect@workbuddy` | installed/enabled，1.6.0 |
| 模型目录 | `GET /v1/models` 与 `codex debug models` | 均为 48 个（国际 28 / 国内 20） |
| 非流式 Responses | `POST /v1/responses`，`cn/hy3`，`stream:false` | 200，`CN_OK` |
| Codex 免费流式 | `codex exec -m cn/hy3` / `-m ai/hy3` | `FINAL_CN_OK` / `AI_CODEX_OK`，provider=workbuddy |
| 启动器 | `scripts\codex-wb.cmd exec` | `WB_LAUNCHER_OK` |
| 已安装缓存复验 | 从 `plugins/cache/.../1.6.0` 运行启动器 | `WB_INSTALLED_OK` |
| 工具续轮 | shell 执行 `Write-Output WB_TOOL_OK` | 工具成功，最终 `WB_TOOL_OK` |
| 模型切换 | `-m cn/hy3` / `-m ai/hy3` | `WB_CN_OK` / `WB_AI_OK` |
| 无效模型 | `POST /v1/responses`，伪造 model | 400 / code 11102，可读错误 |
| 缺失凭据 | 临时不存在凭据路径 | 退出码 2，提示重新登录 |
| 收费调用 | `-m deepseek-v4.1-flash-sg`，禁用重试 | `PAID_OK`，退出码 0 |
| 日志脱敏 | 扫描 `wbproxy.log` / `wbproxy_watch.log` | 未发现 Bearer、accessToken、refreshToken、Cookie |
| 图片输入（免费） | `POST /v1/responses`，`ai/hy3`，32x32 红色 PNG | 200，回复 `Red`，credit=0 |
| 图片输入（当前 SG） | `POST /v1/responses`，`ai/deepseek-v4.1-flash-sg` | 200，回复 `Red`，credit=0 |
| 国内版非流式 | `POST /v1/responses`，`cn/hy3` | 200，`CN_OK` |
| 国内版工具调用 | `cn/deepseek-v4.1-flash` + `ping` | `function_call`，参数 `{"value": 42}` |
| 跨区隔离 | 停用国内版后请求 `cn/hy3` | HTTP 400 `region_unavailable`，未回退国际版 |
| 失败不污染状态 | `cn/hy3` 上游 429 / 6021 | 返回 429，`/wb/last-model` 保持原值 |
| 单实例 | 检查 8787 监听者 | 仅一个 `wbproxy` 进程 |
| 账号状态（实时） | `GET /wb/account-status` | 两区昵称、剩余 / 总额、令牌到期时间齐全 |
| 账号状态（缓存） | `GET /wb/account-status?refresh=0` | 60 秒内复用，不重复请求上游 |
| 单区未登录 | 停用一区后请求状态 | 该区 `available: false`，整体仍 HTTP 200 |
| 昵称回退 | 清空 `nickname` 后请求状态 | `display_name` 为 `UID <后8位>`，不输出完整 UID |
| 企业额度 | 构造 `enterpriseId` 响应 | 走 `get-enterprise-user-usage`；`limitNum=-1` 显示不限 |
| 状态 JSON | `setup.py status --json` | 含 `install` / `regions` / `last_model`，单区失败仍退出 0 |
| 隐私边界 | 扫描状态响应 | 无 accessToken、refreshToken、Authorization、完整 UID |

最终安装状态：

```text
provider 段已安装    : True
profile 文件        : True
模型目录            : True
profile 引用目录    : True
目录含 profile 模型 : True (ai/deepseek-v4.1-flash)
代理 8787 在跑       : True
插件激活             : True
```

收费调用前后 `/wb/credits` 均为 407.9，两位小数下增量为 0.00。该次调用确实使用了
非零倍率模型并成功返回；余额未显示变化可能是小额计费不足 0.01 或账单延迟，未做
额外收费调用去追查。

## 7. 未验证与限制

- 429 限流未主动制造；不通过高频请求验证。已从实际日志确认 `6004`（账号频率限制）与
  `14003`（模型饱和）。Codex 0.163 官方源码显示，429 由采样重试循环对同一请求退避重试；
  预算耗尽后返回错误，不会自动生成新的「继续任务」用户消息。
- 1.5.5 的限流分流已改为：代理对 `14003` 做最多 4 次短退避，`6004` 不重试并立即返回
  可操作提示；WorkBuddy provider 的 Codex 重试预算设为 0，避免叠加。模拟 429 的回归测试
  已覆盖重试次数、总尝试上限、错误映射和脱敏；未用真实账号制造 429。
- 图片能力以 WorkBuddy 实时目录的 `supportsImages` 为准。模型目录会随上游变化，
  后续若某个模型撤回该能力，应重新运行安装器，不要手工把全部模型改为多模态。
- 国内版 20 个模型来自 CLI UA 的 `/v3/config`；上游若改变名单或 UA 规则，需要重新实测。
- 两套安装当前恰好共用同一把 at-rest 密钥（key id `9127dea1b44020a7`），但代码按区域
  各自取密钥并按 `keyId` 校验，不依赖这个巧合。
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
