# codex-workbuddy-connect

把 **WorkBuddy AI（国际版）** 与 **WorkBuddy（国内版）** 桌面端已登录的模型同时接进
**Codex**（CLI 与桌面端），无需 OpenAI 订阅或 Codex 套餐。本仓库同时是标准的 Codex
plugin marketplace。

当前版本为 **1.7.5**：在同一个代理、同一个 8787 端口上同时接入 **WorkBuddy AI（国际版）** 与
**WorkBuddy（国内版）**，两套模型合并进 Codex 模型选择器。选择器里的名字与 WorkBuddy 桌面端一致
（不再带 `（国内）` 后缀），档位快捷项「快速 / 均衡 / 极致」不进列表，模型名单随上游实时刷新。

同一个本地代理、同一个 `8787` 端口，按模型前缀把请求分发到两套上游：

- `ai/*` → WorkBuddy AI（国际版，`www.workbuddy.ai`）
- `cn/*` → WorkBuddy（国内版，`copilot.tencent.com`）
- 不带前缀的旧 ID → 国际版别名，历史会话与旧配置无需迁移

两个区域各自解密自己的登录态、各自刷新令牌、各自限流重试；某个区域没登录时只从目录里消失，
选它的模型会明确报 `region_unavailable`，**绝不跨区回退**。

保留 WorkBuddy 上游的图片能力声明；支持视觉的模型不会被 Codex 误判为纯文本。

完整的源码证据、测试命令和未验证事项见
[`docs/workbuddy-codex-validation.md`](docs/workbuddy-codex-validation.md)。

```text
Codex Desktop / CLI
        │  Responses API
        ▼
127.0.0.1:8787（本地代理）
        │  按 ai/ / cn/ 前缀选区域
        ├──► workbuddy.ai        ──► 国际版模型
        └──► copilot.tencent.com ──► 国内版模型
```

---

## 1.7.0 升级说明（账号与积分状态）

### 1.7.0 的变更

- 新增 `GET /wb/account-status`：按区域返回账号昵称（缺失时回退 UID 尾号）、
  剩余 / 总积分、令牌有效期与读取时间。单区未登录或额度读取失败只标记该区，
  另一区照常返回。
- `setup.py status` 与 `/workbuddy-status` 现在会显示两个区域分别绑定哪个账号、
  各有多少积分；`setup.py status --json` 输出 `install` / `regions` / `last_model`。
- 国内版企业账号改用 `get-enterprise-user-usage` 读取周期额度，
  无限额度显示为“企业额度 · 不限”。
- 积分默认实时读取；`/wb/account-status?refresh=0` 使用 60 秒进程内缓存，
  不落盘、不跨进程。
- **Codex 不提供** dsh 那样的插件设置卡片或输入框插槽，因此没有改模型选择器，
  也不做常驻聊天区 UI；账号与积分统一走状态命令。

### 1.6.0 双区域基础

### 双区域的工作方式

| 项 | 国际版 `ai` | 国内版 `cn` |
|---|---|---|
| 桌面端 | `WorkBuddyAI.exe` | `WorkBuddy.exe` |
| 凭据文件 | `workbuddy-desktop-ai.info` | `workbuddy-desktop.info` |
| 聊天上游 | `https://www.workbuddy.ai/v2/chat/completions` | `https://copilot.tencent.com/v2/chat/completions` |
| 模型目录 | `/v3/config`（App UA） | `/v3/config`（CLI UA） |
| Origin | `https://www.workbuddy.ai` | `https://www.codebuddy.cn` |
| 模型 slug | `ai/<上游 ID>` | `cn/<上游 ID>` |

- 一个代理进程、一个端口、一个 provider；模型选择器里直接切换区域。
- 两个区域各自解密自己的登录态、各自刷新令牌、各自限流重试；**绝不跨区回退**。
- 不带前缀的旧模型 ID 一律按国际版处理，历史会话与现有 `config.toml` 继续可用。
- 某个区域没装或没登录，只从目录里消失；`/health` 的 `regions` 会给出原因。

### 1.6.0 的变更

- 新增区域化凭据与发现：`WORKBUDDY_AI_APP` / `WORKBUDDY_AI_AUTH_FILE` 表示国际版，
  `WORKBUDDY_CN_APP` / `WORKBUDDY_CN_AUTH_FILE` 表示国内版；未设置时自动探测。
- 代理按 `ai/` / `cn/` 前缀路由到对应上游，请求头、UA、Origin、目录 UA 全部按区域区分。
- 模型目录合并两个区域：显示名与 WorkBuddy 桌面端保持一致，不加区域后缀；只有两区真的
  同名时才补 `（国际）` / `（国内）`，同区域重名再消歧。
- 推理档位优先取上游 `supportedEfforts` / `defaultEffort`，避免展示无效档位。
- 新增 `scripts\wb-model.cmd`（`list` / `current` / `use` / `enable|disable`）作为选择器兜底。
- `WB_FOLLOW_LAST_MODEL`（默认开）取代 `WB_SYNC_CONFIG`；真实对话请求才同步配置，
  同时改写 `config.toml` 与 `wb.config.toml`，保留 CRLF。
- `/health` 新增 `regions.{ai,cn}`、`pid`、`version`、`root`；`/wb/last-model` 新增
  `region` / `upstream_model` / `follow_last_model`。
- 安装器在重装前停掉所有监听同一端口的旧 `wbproxy`，Windows 上关闭地址复用并做单实例检查。
- 保留 1.5.5 的全部修复：`X-Conversation-*` 头、`tool_choice` 扁平化、图片能力传播、
  脱敏错误日志、429 分流。
- 1.6.0 起 `/wb/last-model` 只在上游真正接受请求后才更新（失败/限流不会把默认模型带偏）。

### 实测结果

本机验证环境为 Windows、Codex CLI `0.163.0-alpha.10.2`、WorkBuddy 国际版 5.8.4 与
国内版 5.8.3（上游报告版本 `37.10.3-42`），插件 1.6.0 显示为 `installed, enabled`。

| 项目 | 结果 |
|---|---|
| 单元测试 | 34/34 通过 |
| 模型目录 | `/v1/models` 48 个（国际 28 / 国内 20），`codex debug models` 同样 48 个 |
| 国际版模型（2026-10-22 记录，当时可用） | `ai/hy3`、`ai/deepseek-v4.1-flash`、`ai/step-5-preview` 端到端成功；**2026-11-08 起上游不可用并已隐藏** |
| 国内版模型 | `cn/hy3` 非流式成功；`cn/deepseek-v4.1-flash` 工具调用成功 |
| 流式 / 非流式 | 均完成验证 |
| 工具续轮 | 国际版与国内版均返回 `function_call`，参数正确 |
| 模型切换 | 选择器直接列出两区模型（同名时才补区域后缀），`codex exec -m cn/deepseek-v4.1-flash` 成功 |
| 区域隔离 | 停用国内版后 `cn/hy3` 返回 400 `region_unavailable`，未回退国际版 |
| 单实例 | 8787 只有一个 `wbproxy` 监听进程 |
| 异常路径 | 无效模型返回可读 4xx；缺失凭据提示重新登录 |
| 日志安全 | 未发现 Bearer、访问令牌、刷新令牌或 Cookie 泄露 |
| 图片输入 | `ai/hy3`、`cn/hy3` 与 `ai/deepseek-v4.1-flash-sg` 经代理返回 HTTP 200，正确识别红色 PNG |

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

> 想改代码、调试代理或让 Codex 直接读本地源码，用源码挂载模式：
> [install.md](install.md)（mount-local.ps1 + Junction）。

装插件后还需要执行一次安装器，写入 provider、profile 和模型目录：

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"

python "<插件根>\bin\setup.py" doctor
python "<插件根>\bin\setup.py" install
python "<插件根>\bin\setup.py" status
python "<插件根>\bin\setup.py" status --json
```

`status` 会额外列出两个区域各自的账号昵称（缺昵称时用 UID 后 8 位）、
剩余 / 总额积分与令牌有效期。单区未登录或余额读取失败只标记该区，
命令仍以 0 退出；`--json` 供脚本与技能读取。

模型选择器旁不会显示积分：Codex 宿主没有第三方 composer 插槽，
它只在 dsh 里存在。

安装器是幂等的，只更新 WorkBuddy 的 provider/profile、模型目录、技能和插件规则，
不会覆盖 `config.toml` 中其他 provider。

### 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Windows | — | 依赖 `%LOCALAPPDATA%` 和 `.exe`，当前不支持跨平台运行 |
| WorkBuddy AI 桌面端 | 可选 | 国际版（`ai/*`）凭据来源；装了就要登录，不要求常驻运行 |
| WorkBuddy 国内版 | 可选 | 国内版（`cn/*`）凭据来源；装了就要登录 |
| Python | 3.10+，含 `cryptography` | 用于解密登录凭据 |
| Codex | 0.163.0-alpha.10.2（已验证） | 0.153+ 使用 Responses API |

> 两个桌面端**至少装一个并登录**。只装一个也能正常使用，另一个区域从选择器里消失。

缺项先运行 `doctor.cmd`；它会说明如何补齐 Python、依赖、登录状态和 Codex。

### 升级到 1.6.0

Codex 按版本号物化插件缓存，必须让插件重新安装：

```cmd
codex plugin marketplace upgrade workbuddy
codex plugin add codex-workbuddy-connect@workbuddy
python "<新版插件根>\bin\setup.py" install
python "<新版插件根>\bin\setup.py" status
```

升级后要**重启本地代理**，否则旧进程仍会使用 1.5.5 的单区域代码。安装器会自动停掉监听同一端口的旧 wbproxy 进程（按命令行核对，不误杀其它 Python）。升级前如遇到缓存占用：

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
<插件>\scripts\codex-wb.cmd -m cn/hy3      切换国内版模型
<插件>\scripts\codex-wb.cmd -m ai/hy3      切换国际版模型
<插件>\scripts\wb-model.cmd list|use       列出模型 / 切换全局 provider 与默认模型
```

`wb-model.cmd use <slug>` 会同时把全局 `config.toml` 的 `model_provider` 改为
`workbuddy`、`model_catalog_json` 指向 `workbuddy-catalog.json`，并设置默认模型。
之后完全退出并重启 Codex 桌面端，模型选择器才会重新加载 WorkBuddy provider。

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

3. 从 PowerShell 或 Windows Terminal 启动桌面端：

   ```cmd
   "<插件根>\scripts\start-codex-app-wb.cmd" D:\your-workspace
   ```

   `.cmd` 启动器只在本次进程使用 `-ExecutionPolicy Bypass`，不会修改系统执行策略。
   如果当前 Codex 桌面端仍在运行，启动器会拒绝复用旧实例，避免新配置被静默忽略。

4. 新建聊天，在模型选择器里选 `（国际）` 或 `（国内）` 的条目并发送消息。
5. 查询实际转发给上游的模型：

   ```powershell
   curl.exe --noproxy "*" http://127.0.0.1:8787/wb/last-model
   ```

`model` 是当前请求实际使用的 slug（如 `cn/hy3`），`region` / `upstream_model` 说明
走的哪个账号、转发给上游的哪个 ID；`config_model` 只是新会话的默认值。模型选择器中
显示某个名称不等于请求已经使用该模型，应以 `/wb/last-model` 为准。

---

## 模型与计费

实测免费档（2026-11-08，国内版）：`cn/hy4free`、`cn/hy3`、`cn/deepseek-v4.1-flash`。
`cn/step-5-preview` 现在实测 0.02（旧记录为免费，已失效）；`cn/hy4std`、`cn/hy3-x` 也收费。
国际版当前上游不可用，已从选择器隐藏（`wb-model.cmd hide|show ai`）。

代理把用量记到 `%TEMP%\wb_usage.jsonl`，账号剩余额度按区域查看：

```powershell
curl.exe --noproxy "*" "http://127.0.0.1:8787/wb/credits?region=ai"
curl.exe --noproxy "*" "http://127.0.0.1:8787/wb/credits?region=cn"
```

收费模型只做一次最小验证，不启用自动重试。上游属于私有接口，费率与可用模型可能变化；
调用前以实时模型目录为准。

---

## 代理、hook 与看护

代理绑定 `127.0.0.1:8787`，主要端点如下：

| 路径 | 用途 |
|---|---|
| `GET /health` | 两个区域的账号/令牌状态（`regions.ai` / `regions.cn`）、最近模型、PID 与版本 |
| `GET /v1/models` | 合并后的 OpenAI 形状和 Codex 形状模型目录 |
| `POST /v1/responses` | Codex Responses 请求，支持流式与非流式 |
| `POST /v1/chat/completions` | Chat Completions 兼容入口 |
| `GET /wb/last-model` | 代理实际转发的最近 slug + 区域 + 上游 ID（权威值） |
| `GET /wb/credits?region=ai` / `?region=cn` | 指定区域的剩余额度 |
| `GET /wb/account-status` | 两区账号与积分摘要（实时读取）；`?refresh=0` 用 60 秒缓存 |

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
| `400 / code 11128 / request illegal` | 1.5.4 缺少三个 `X-Conversation-*` 头。升级 1.6.0 并重启代理 |
| 本地 provider 返回 `502` | 进程继承了失效代理变量。使用 `codex-wb.cmd` 或桌面端启动器 |
| 模型选择器只有 DeepSeek 或内置模型 | `model_catalog_json` 未绑定或文件不存在。重跑 `setup.py install`，新建聊天 |
| 选择器里只有一组 WorkBuddy 模型 | 另一个区域未登录或被停用；看 `/health` 的 `regions`，补登录后重跑安装器 |
| 选 `cn/*` 报 `region_unavailable` | 国内版未登录或 `WORKBUDDY_CN_DISABLED=1`；**不会**回退国际版 |
| `Unknown model deepseek-v4.1-flash` | profile 未引用 `workbuddy-catalog.json`。检查 `wb.config.toml` 后重跑安装 |
| 选了模型但没有响应 | 先查 `/health` 与 `/wb/last-model`；再确认使用的是新建聊天 |
| `exceeded retry limit, last status: 429` | 旧版/provider 重试预算耗尽。升级后查日志区分 `14003` / `6004`：前者代理会有限重试，后者立即提示换模型 |
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
├─ PUBLISHING.md
├─ install.md                  从源码本地安装（挂载模式，开发用）
├─ mount-local.ps1             注册本地 marketplace + Junction 挂载
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
