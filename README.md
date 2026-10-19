# codex-workbuddy-connect

把 **WorkBuddy AI 桌面端**已登录的模型接进 **Codex**（CLI 与桌面端），
无需 OpenAI 订阅或 Codex 套餐。本仓库同时是标准的 Codex plugin marketplace。

当前版本为 **1.5.5**，修复了 1.5.4 安装后仍无法调用 WorkBuddy 模型的问题：

- 补齐上游要求的三个 `X-Conversation-*` 会话头，解决
  `400 / code 11128 / request illegal`。
- 将 `workbuddy-catalog.json` 显式绑定到 `wb` profile，解决
  `Unknown model` 和模型选择器缺项。
- 启动脚本默认绕开失效的 `HTTP(S)_PROXY`，解决本地 provider 的 502。
- 新增 Codex 桌面端启动器、`tool_choice` 兼容处理、脱敏错误日志和回归测试。

完整的源码证据、测试命令和未验证事项见
[`docs/workbuddy-codex-validation.md`](docs/workbuddy-codex-validation.md)。

```text
Codex Desktop / CLI
        │  Responses API
        ▼
127.0.0.1:8787（本地代理）
        │  解密桌面端登录态；Responses → Chat Completions
        ▼
WorkBuddy 上游 ──► 真实模型
```

---

## 1.5.5 升级说明

### 1.5.4 的三个独立故障

1. **上游拒绝请求**：WorkBuddy 要求每次 chat 请求携带
   `X-Conversation-ID`、`X-Conversation-Request-ID` 和
   `X-Conversation-Message-ID`。1.5.4 缺失这些头，因此返回
   `400 / code 11128 / request illegal`。
2. **Codex 不认识模型**：1.5.4 生成了模型目录，但 `wb.config.toml` 没有
   `model_catalog_json`。Codex 因此报 `Unknown model`，模型元数据与工具定义
   回退到默认值，桌面端模型列表也可能不完整。
3. **请求到不了本地代理**：继承的失效 `HTTP_PROXY`、`HTTPS_PROXY` 或
   `ALL_PROXY` 会让 Codex 对 `127.0.0.1:8787` 返回 502。

### 1.5.5 的修复

- 每个 chat 请求生成新的三个 `X-Conversation-*` UUID；账户和额度请求不携带这些头。
- `wb.config.toml` 写入绝对路径的 `model_catalog_json`；`setup.py status`
  检查目录是否存在、profile 是否引用目录、目录是否包含当前模型。
- Responses → Chat 转换支持对象形式的 `tool_choice`；`tool_choice = "none"`
  时不再向上游发送工具定义。
- `codex-wb.cmd`、`start-wbproxy.cmd` 和代理默认清理失效的代理环境变量。
  需要企业代理访问上游时设置 `WB_KEEP_PROXY=1`。
- 新增 `scripts/start-codex-app-wb.ps1`，只给本次桌面端启动注入 WorkBuddy
  provider、模型目录和默认模型，不修改全局默认 provider。
- 未预期异常写入脱敏日志，不记录 Authorization、访问令牌、刷新令牌、Cookie、
  完整请求体或凭据文件内容。
- 新增请求头、错误脱敏、工具转换、模型目录、桌面端启动器和跨平台路径测试。

### 实测结果

本机验证环境为 Windows、Codex CLI `0.163.0-alpha.10.2`、WorkBuddy AI 桌面端
`37.10.3-42`，插件 1.5.5 显示为 `installed, enabled`。

| 项目 | 结果 |
|---|---|
| 单元测试 | 7/7 通过 |
| 免费模型 | `deepseek-v4.1-flash`、`hy3` 端到端成功 |
| 流式 / 非流式 | 均完成验证 |
| 工具续轮 | shell 工具执行成功并继续最终回复 |
| 模型切换 | 桌面端模型选择器可加载并切换 WorkBuddy 模型 |
| 异常路径 | 无效模型返回可读 4xx；缺失凭据提示重新登录 |
| 收费模型 | `deepseek-v4.1-flash-sg` 完成一次最小调用 |
| 日志安全 | 未发现 Bearer、访问令牌、刷新令牌或 Cookie 泄露 |

429 限流和 600 秒真实超时没有主动制造；详细边界见验收记录。

---

## 安装（已发布）

从本仓库安装：

```cmd
codex plugin marketplace add Jesse-hand99/codex-workbuddy-connect
codex plugin add codex-workbuddy-connect@workbuddy
```

从原项目安装：

```cmd
codex plugin marketplace add DuuU525/codex-workbuddy-connect
codex plugin add codex-workbuddy-connect@workbuddy
```

装插件后还需要执行一次安装器，写入 provider、profile 和模型目录：

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"

python "<插件根>\bin\setup.py" doctor
python "<插件根>\bin\setup.py" install
python "<插件根>\bin\setup.py" status
```

安装器是幂等的，只更新 WorkBuddy 的 provider/profile、模型目录、技能和插件规则，
不会覆盖 `config.toml` 中其他 provider。

### 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Windows | — | 依赖 `%LOCALAPPDATA%` 和 `.exe`，当前不支持跨平台运行 |
| WorkBuddy AI 桌面端 | 已安装且已登录 | 凭据来源；不要求常驻运行 |
| Python | 3.10+，含 `cryptography` | 用于解密登录凭据 |
| Codex | 0.163.0-alpha.10.2（已验证） | 0.153+ 使用 Responses API |

缺项先运行 `doctor.cmd`；它会说明如何补齐 Python、依赖、登录状态和 Codex。

### 升级到 1.5.5

Codex 按版本号物化插件缓存，必须让插件重新安装：

```cmd
codex plugin marketplace upgrade workbuddy
codex plugin add codex-workbuddy-connect@workbuddy
python "<新版插件根>\bin\setup.py" install
python "<新版插件根>\bin\setup.py" status
```

升级后要**重启本地代理**，否则旧进程仍会使用 1.5.4 的请求代码。升级前如遇到缓存占用：

```text
failed to back up plugin cache entry: 拒绝访问 (os error 5)
```

请退出停留在插件目录中的终端，只停止本插件代理的精确 PID，再重试；不要按进程名批量结束
Python 进程，也不要递归删除 Codex 缓存目录。

---

## 在 Codex CLI 中使用

```cmd
codex --profile wb                          新会话使用 WorkBuddy
<插件>\scripts\codex-wb.cmd                等价，且自动拉起代理
<插件>\scripts\codex-wb.cmd -m hy3         切换模型
```

`codex-wb.cmd` 默认清空失效的代理变量。直接运行 `codex --profile wb` 时，如本机
代理变量不可用，先在 PowerShell 中清理：

```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
$env:http_proxy=''; $env:https_proxy=''; $env:all_proxy=''
codex --profile wb
```

需要企业代理访问 WorkBuddy 上游时，在启动器环境中设置 `WB_KEEP_PROXY=1`。

---

## 在 Codex 桌面端中使用

provider 与模型按会话绑定，旧聊天不会跟随新配置；改配置或切换模型后要**新建聊天**。
`codex app` 也有单实例行为：如果桌面端已经在运行，新启动参数可能被忽略。

1. 完全退出 Codex 桌面端。
2. 确认本地代理健康：

   ```powershell
   curl.exe --noproxy "*" http://127.0.0.1:8787/health
   ```

3. 从 PowerShell 启动桌面端：

   ```powershell
   powershell -ExecutionPolicy Bypass -File `
     "<插件根>\scripts\start-codex-app-wb.ps1" `
     D:\your-workspace
   ```

4. 新建聊天，选择 WorkBuddy 模型并发送消息。
5. 查询实际转发给上游的模型：

   ```powershell
   curl.exe --noproxy "*" http://127.0.0.1:8787/wb/last-model
   ```

`model` 是当前请求实际使用的模型；`config_model` 只是新会话的默认值。模型选择器中
显示某个名称不等于请求已经使用该模型，应以 `/wb/last-model` 为准。

---

## 模型与计费

实测免费档为 `hy3`、`deepseek-v4.1-flash` 和 `step-5-preview`。
`hy4-preview` 虽然名字带 `preview`，但实测收费（0.02），不要凭名称判断。

代理把用量记到 `%TEMP%\wb_usage.jsonl`，账号剩余额度可通过以下接口查看：

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8787/wb/credits
```

收费模型只做一次最小验证，不启用自动重试。上游属于私有接口，费率与可用模型可能变化；
调用前以实时模型目录为准。

---

## 代理、hook 与看护

代理绑定 `127.0.0.1:8787`，主要端点如下：

| 路径 | 用途 |
|---|---|
| `GET /health` | 账号、令牌状态、最近模型与请求数 |
| `GET /v1/models` | OpenAI 形状和 Codex 形状的模型目录 |
| `POST /v1/responses` | Codex Responses 请求，支持流式与非流式 |
| `POST /v1/chat/completions` | Chat Completions 兼容入口 |
| `GET /wb/last-model` | 代理实际转发的最近模型（权威值） |
| `GET /wb/credits` | 剩余额度 |

代理不需要单独启动，有两层保障：

1. **SessionStart hook**：新开会话时若代理没运行就拉起。
2. **登录看护**：按插件启用状态开关代理；插件停用或卸载时停止代理。

Codex 默认不执行未信任 hook。桌面端进入 **设置 → Hooks → From Plugins**，找到本插件
的 `SessionStart`，选择 **Trust all**。TUI 新会话时选择
**Trust all and continue**；自动化场景可使用
`codex exec --dangerously-bypass-hook-trust`。

hook 内容变化后需要重新信任。看护状态与手动控制：

```cmd
<插件>\scripts\watch-plugin.cmd --status
<插件>\scripts\watch-plugin.cmd --once
<插件>\scripts\watch-plugin.cmd --remove-autostart
```

日志位于 `%TEMP%\wbproxy.log` 和 `%TEMP%\wbproxy_watch.log`。

---

## 故障排查

| 症状 | 原因与处理 |
|---|---|
| `400 / code 11128 / request illegal` | 1.5.4 缺少三个 `X-Conversation-*` 头。升级 1.5.5 并重启代理 |
| 本地 provider 返回 `502` | 进程继承了失效代理变量。使用 `codex-wb.cmd` 或桌面端启动器 |
| 模型选择器只有 DeepSeek 或内置模型 | `model_catalog_json` 未绑定或文件不存在。重跑 `setup.py install`，新建聊天 |
| `Unknown model deepseek-v4.1-flash` | profile 未引用 `workbuddy-catalog.json`。检查 `wb.config.toml` 后重跑安装 |
| 选了模型但没有响应 | 先查 `/health` 与 `/wb/last-model`；再确认使用的是新建聊天 |
| `exceeded retry limit, last status: 429` | 上游限流。降低频率，避免同时运行 WorkBuddy agent，稍后再试 |
| 代理起不来 | 确认 WorkBuddy 桌面端已登录、Python 含 `cryptography`、端口未被占用 |
| hook 没有启动代理 | 在桌面端 Hooks 页面重新信任本插件的 SessionStart |
| 重装时报 `os error 5` | 有进程占用旧插件目录。退出相关终端，停止本插件代理的精确 PID 后重试 |

排查日志时不要公开 Authorization、Cookie、令牌或完整 HTTP 请求转储。正式配置不要启用
`WB_DUMP`；临时诊断 dump 应放在临时目录并在完成后删除。

---

## 仓库结构

```text
codex-workbuddy-connect/
├─ .agents/plugins/marketplace.json
├─ README.md
├─ docs/workbuddy-codex-validation.md
└─ plugins/codex-workbuddy-connect/
   ├─ .codex-plugin/plugin.json
   ├─ bin/                      代理、安装器、模型目录生成器
   ├─ commands/                /workbuddy-setup、/workbuddy-status
   ├─ hooks/hooks.json         SessionStart
   ├─ scripts/                 CLI 与桌面端启动器
   ├─ skills/                  使用说明与当前模型查询
   ├─ templates/               provider/profile 模板
   └─ tests/                   回归测试
```

插件内部的完整排错、模型费率和历史坑见
[`plugins/codex-workbuddy-connect/README.md`](plugins/codex-workbuddy-connect/README.md)。
发布流程见 [`PUBLISHING.md`](PUBLISHING.md)。

---

## 卸载与回滚

卸载插件和安装器写入的配置：

```cmd
codex plugin remove codex-workbuddy-connect@workbuddy
codex plugin marketplace remove workbuddy
python "<插件根>\bin\setup.py" uninstall
```

回滚到 1.5.4：先停止本插件代理和看护的精确 PID，再恢复安装器生成的
`config.toml.bak-cwbc-*`、`wb.config.toml.bak-cwbc-*` 和 `AGENTS.md.bak-cwbc-*`，
最后重新安装 1.5.4。不要按进程名批量结束 Python 进程，也不要递归删除整个 Codex 缓存目录。

---

## 安全与限制

- 凭据由 WorkBuddy 桌面端解密，只在代理内存中使用；仓库不包含账号、令牌或 Cookie。
- 本地代理绑定 `127.0.0.1`，但没有额外的本地 bearer 鉴权；同机进程理论上可以调用。
- WorkBuddy 上游是私有接口，会话头、429 限流、模型目录和计费规则都可能变化。
- 429 限流和 600 秒真实超时没有主动制造；相关测试仍标记为未验证。
- 免费模型不应被高频批量调用；不要公开分发凭据或完整请求转储。
- 本插件绕过上游 system prompt 指纹属于主动规避风控；使用者应自行评估账号与合规风险。
