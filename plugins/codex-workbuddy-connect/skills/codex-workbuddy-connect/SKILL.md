---
name: codex-workbuddy-connect
description: >-
  把 WorkBuddy AI（国际版）与 WorkBuddy（国内版）桌面端已登录的模型同时接进 Codex
  （CLI 与桌面端），或排查该接入的问题。一个本地代理、一个 8787 端口，模型 ID 以
  ai/ 与 cn/ 前缀区分区域，可直接在 Codex 模型选择器里切换。
  Use when the user wants to use WorkBuddy/CodeBuddy models in Codex, asks about the
  workbuddy model provider, the local proxy on 127.0.0.1:8787, the free WorkBuddy models
  (hy4flash-free / deepseek-v4.1-flash), switching between 国际版 and 国内版,
  hiding a region from the picker (hidden_regions),
  ai/ or cn/ model slugs, the /workbuddy-setup or /workbuddy-status commands, hook trust,
  the proxy watcher/autostart, model-switching not taking effect, or 429 rate limits
  from the WorkBuddy upstream.
metadata:
  short-description: WorkBuddy 模型接入 Codex
---

# Codex × WorkBuddy Connect

让 Codex 用 WorkBuddy 桌面端的登录态当模型源 —— 不需要 OpenAI 订阅，也不需要 Codex 套餐。
Codex 只出它的 agent 骨架（读写文件、跑命令、apply_patch、多轮工具循环），模型换成 WorkBuddy 的。

两个区域共用**同一个代理、同一个端口、同一个 provider**：

| 区域 | 上游 | 模型前缀 | 桌面端 |
|---|---|---|---|
| 国际版 | `https://www.workbuddy.ai` | `ai/` | `WorkBuddyAI.exe` |
| 国内版 | `https://copilot.tencent.com` | `cn/` | `WorkBuddy.exe` |

```
Codex ──► 127.0.0.1:8787（本地代理）──┬─► workbuddy.ai      ──► 国际版模型
           │  按 ai/ / cn/ 前缀选区域  └─► copilot.tencent.com ──► 国内版模型
           │  解密两个桌面端登录态（各自独立）
           │  Responses ⇄ Chat Completions 转换
           │  合并目录 / 记录并同步「实际在用」的模型
```

- 不带前缀的旧 ID（如 `deepseek-v4.1-flash`）**一律按国际版**处理，历史会话与旧配置继续可用。
- 某个区域没装或没登录，只是该区域从目录里消失；**绝不跨区回退**——选了 `cn/*` 但国内版
  未登录会直接返回 `region_unavailable`，不会偷偷走国际版账号。

## 插件装在哪 / 怎么找到它

插件被 Codex **复制**到 cache，源码改动不会自动生效：

```
%USERPROFILE%\.codex\plugins\cache\<marketplace>\<plugin>\<version>\
```

下面所有 `<插件>` 都要按真实位置拼。先定位：

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"
```

安装 / 更新（`marketplace add` 只需一次）：

```cmd
codex plugin marketplace add <owner>/<repo>          :: 或本地目录 / zip 解压处
codex plugin add codex-workbuddy-connect@workbuddy
python "<插件>\bin\setup.py" doctor
python "<插件>\bin\setup.py" install
```

**Codex 按版本号缓存** —— 改了插件内容要先升版本再重装，否则不生效：

```cmd
python "<插件>\bin\bump-plugin.py" "<插件目录>"
codex plugin add codex-workbuddy-connect@workbuddy
```

⚠ 重装前确保**没有进程把插件目录当工作目录**（自己 `cd` 进去过、或代理/看护还在跑），
否则报 `failed to back up plugin cache entry: 拒绝访问 (os error 5)`。
先把代理和看护停掉再重装。

## 文件一览

| 路径 | 用途 |
|---|---|
| `install.cmd` / `uninstall.cmd` / `status.cmd` / `doctor.cmd` | 装 / 卸 / 查 / 体检 |
| `bin\wbproxy.py` | 本地代理本体（凭据解密、请求改写、SSE 回译、模型同步） |
| `bin\wbresponses.py` | Responses API ⇄ Chat Completions 适配层 |
| `bin\wbdiscover.py` | 路径自动发现（桌面端 / Python / Codex / 凭据） |
| `bin\gen-catalog.py` | 生成合并后的模型切换列表 `workbuddy-catalog.json` |
| `bin\wb-model.py` | 选择器不可用时的兜底：`list` / `current` / `use` / `enable` / `disable` |
| `bin\setup.py` | 安装/卸载器（provider、profile、目录、技能、AGENTS 规则、看护自启、清理旧代理） |
| `bin\hook_session_start.py` | SessionStart hook 的实现 |
| `bin\wbspawn.py` | 无控制台窗口启动后台进程（venv 的 python.exe 是 uv 存根，转发不传 flags） |
| `bin\watch_plugin.py` | **看护**：按插件启用状态开关代理 |
| `bin\bump-plugin.py` | 更新插件时打 cachebuster 版本号 |
| `scripts\codex-wb.cmd` | 启动 Codex（代理没跑会自动拉起） |
| `scripts\start-wbproxy.cmd` | 只启动代理（`--verbose` 看请求流水） |
| `scripts\watch-plugin.cmd` | 看护：`--status` / `--once` / `--install-autostart` / `--remove-autostart` |
| `scripts\wb-model.cmd` | 快捷切换模型/区域（`wb-model.cmd list`、`use cn/deepseek-v4.1-flash`、`disable cn`） |
| `skills\` | 本技能 + `query-current-model` |
| `commands\` | `/workbuddy-setup`、`/workbuddy-status`（**注意**：本版本 Codex 的
`plugin/read` 不枚举 commands，这两个可能不被暴露；功能等同的命令行见下） |

## 常用操作（可靠路径）

插件命令（`/workbuddy-setup` 等）**在部分 Codex 版本里不会被暴露**，所以日常直接用脚本：

```cmd
python "<插件>\bin\setup.py" doctor        :: 体检
python "<插件>\bin\setup.py" install       :: 安装（幂等）
python "<插件>\bin\setup.py" status        :: 安装状态
python "<插件>\bin\watch_plugin.py" --status   :: 看护判定
```

## 代理的生命周期：两层保障

**代理不需要手动启动。** 两层互补：

1. **hook（会话级）** —— `SessionStart`：新开会话时确保代理在跑。
2. **看护（常驻）** —— `install.cmd` 装成**登录自启**，每 5 秒对账：
   **插件激活 → 代理在跑；插件停用/卸载 → 停掉代理**（实测 4 秒内跟随）。

| 场景 | 谁负责 |
|---|---|
| 新开 CLI 会话 / 桌面端新聊天 | hook |
| 机器重启后 | 看护 |
| 已开着的会话里代理中途挂了 | 看护（几秒自愈） |
| 在 Codex 里停用插件 | 看护（自动停代理） |
| 不用 Codex 也想常驻 | 看护 |

### hook 必须被信任一次，否则永远不执行

未信任时它的状态是 **`untrusted`**，**不会跑** —— 这正是「代理没起来、桌面端静默失效」
最常见的原因。查状态（起 app-server 问 `hooks/list`）会看到：

```
key:         <plugin>@<market>:hooks/hooks.json:session_start:0:0
enabled:     True
trustStatus: untrusted        ← 就是这个
```

**桌面端**：设置 → **Hooks** → 来源分组 **From Plugins** 下找到本插件 `SessionStart`
（未信任时显示 *Disabled until hook is trusted*）→ 点 **Trust all**。

**TUI**：新开会话时弹 *Hooks need review … [Trust all and continue]*，选它。

**命令行 / 自动化**：`codex exec --dangerously-bypass-hook-trust ...`

信任状态存在 `config.toml` 的 `hooks.state.<key>.trusted_hash`；**改过 hook 内容后要重新信任**
（会显示 *Hook changed since last trusted*）。若提示 *Another config layer overrides these hook
settings*，是有更高优先级的配置层压着。

### 看护命令与日志

```cmd
python "<插件>\bin\watch_plugin.py" --status
```

| 参数 | 作用 |
|---|---|
| （无） | 常驻看护 |
| `--status` | 打印判定：插件是否激活、代理是否在跑、是否一致 |
| `--once` | 只对账一次 |
| `--install-autostart` | 装登录自启（写 VBS 到启动文件夹，免管理员） |
| `--remove-autostart` | 取消登录自启 |

日志：`%TEMP%\wbproxy_watch.log`（只记状态变化与错误）。自启文件：
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-workbuddy-connect-watch.vbs`。

看护**不会误杀别人的程序**：停代理前核对进程命令行含本包 `wbproxy.py`；
端口被别的程序占用时报 `foreign-port` 并什么都不做。

## 用法

```cmd
codex --profile wb                        新会话走 WorkBuddy
<插件>\scripts\codex-wb.cmd               等价，且代理没跑会自动拉起
<插件>\scripts\codex-wb.cmd -m cn/deepseek-v4.1-flash  选国内版模型
<插件>\scripts\codex-wb.cmd -m ai/deepseek-v4.1-flash  选国际版模型
<插件>\scripts\start-codex-app-wb.cmd     桌面端（自动绕过 .ps1 执行策略）
```

桌面端（ChatGPT.exe / Codex app）同理：**模型选择器里会同时列出两组模型**，显示名与
WorkBuddy 桌面端一致（不加区域后缀，只有两区同名时才补），选中即切换区域，无需换端口、
profile 或会话。桌面端的「快速 / 均衡 / 极致」是档位快捷项，不是独立模型，所以不进列表。
桌面端**必须完全退出后**再运行启动器；当前进程会复用旧 provider，单独新建聊天不够。
切换后**必须新建会话**才让默认值生效；当前会话可直接在选择器里改，立即生效。

### 快捷切换（选择器不可用时）

```cmd
<插件>\scripts\wb-model.cmd list            列出两个区域全部模型
<插件>\scripts\wb-model.cmd current         实际在用 + 新建会话默认
<插件>\scripts\wb-model.cmd use cn/deepseek-v4.1-flash  切换全局 provider/目录/默认模型
<插件>\scripts\wb-model.cmd hide ai         只从选择器隐藏国际版（账号仍可查、可恢复）
<插件>\scripts\wb-model.cmd show ai         恢复国际版
<插件>\scripts\wb-model.cmd disable cn      整区停用国内版（用户环境变量，重启代理后生效）
<插件>\scripts\wb-model.cmd enable cn       恢复国内版
```

`use` 会同时把全局 `config.toml` 的 `model_provider` 改为 `workbuddy`、
`model_catalog_json` 指向 `workbuddy-catalog.json`，并设置默认模型。桌面端必须完全退出后
重新启动，模型选择器才会按新 provider 重新加载。只运行 `setup.py install` 只会注册 provider，
不会覆盖全局 provider。

`disable` / `enable` 写的是 `WORKBUDDY_CN_DISABLED` / `WORKBUDDY_AI_DISABLED` 用户环境变量；
对已运行的代理无效，需要重启代理（重新登录插件或 `watch_plugin.py --once`）。

## 国内版模型（2026-11-24 实时目录，16 个）

`cn/hy4std`、`cn/hy4flash-free`、`cn/hy4flash`、`cn/deepseek-v4.1-flash`、
`cn/glm-5.3`、`cn/glm-5.3-flash`、`cn/glm-5.2`、`cn/space-bunny`、
`cn/minimax-m3.1-flash`、`cn/minimax-m2.7`、`cn/kimi-k3.1-pro`、`cn/kimi-k3-1`、
`cn/kimi-k2.8-preview`、`cn/deepseek-v4-pro`、`cn/step-5-preview`、`cn/mimo-v2.6-pro`。

这份名单来自桌面端的 `agents.cli.models`（CLI 名单），不是全部内部模型：`/v3/config`
里还有 `glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`kimi-k2.6`、`kimi-k2.7` 等，但 CLI
不可用，所以不进目录。Hy3 / Hy3 (X) 已从 CLI 名单下线，`hy4free` 也已改名成
`hy4flash-free`（Hy4 flash，限时免费）与 `hy4flash`。

目录里**不收录**两类条目：桌面端的档位快捷项（`fast-model` / `balanced-model` /
`primary-model` / `deep-model`，即「快速 / 均衡 / 极致 / 深度思考」）与出图、出视频模型
（`hunyuan-image-*`、`kling-video-*`、`minimax-video-*`）。

## 免费档（目录标价）

- `cn/hy4flash-free`、`cn/deepseek-v4.1-flash`：**0 扣分**
- `cn/step-5-preview`：0.02（旧文档记的免费已失效）
- `cn/hy4std`：标价 0.29，短请求实扣约 0.04

⚠ 不要凭名字判断，也不要只看目录标价——以响应里的 `usage.credit` 为准。
默认档取 `cn/deepseek-v4.1-flash`（免费里 1M 上下文 / 128k 输出最大）。

## 区域可见性（hidden_regions）

国际版不可用时，不必登出或彻底停用，直接隐藏：

```cmd
<插件>\scripts\wb-model.cmd hide ai
```

- 设置写入 `%USERPROFILE%\.codex\workbuddy-settings.json` 的 `hidden_regions`
- 效果：`/v1/models` 和 `workbuddy-catalog.json` 不再列出该区；`/health` 仍报告账号
- 显式请求被隐藏的区域 → 400 `region_unavailable`，**不会跨区回退**
- 恢复：`wb-model.cmd show ai`，然后重启 Codex 桌面端（新聊天）

区域完全不可用（凭据也不加载）是另一件事，用 `disable ai` / `WORKBUDDY_AI_DISABLED=1`。

## 「当前在用哪个模型」必须现查

模型**无法内省自己的身份**，而 `config.toml` 里的 `model` 只是**新建会话的默认值** ——
在会话里用选择器切换是**运行时覆盖，不会写回配置**。所以不要读配置回答，要问代理：

```cmd
curl -s http://127.0.0.1:8787/wb/last-model
```

返回里必须同时看三个字段：

- `model`：选择器 slug，如 `cn/deepseek-v4.1-flash`（权威）
- `region`：`ai` 或 `cn`，说明走的是哪个账号
- `upstream_model`：实际转发给上游的模型 ID（已剥掉前缀）

代理是唯一看到真实 `model` 字段的一方。`/health` 也带 `last_model`，并新增
`regions.{ai,cn}`、`pid`、`version`、`root`，可确认到底是哪个进程在应答。
细节见同插件的 `query-current-model` 技能。

## 上游 429（`exceeded retry limit, last status: 429`）

这是 WorkBuddy 上游限流，不是代理的 bug；429 不计费。必须区分业务码：

| 业务码 | 含义 | 当前处理 |
|---|---|---|
| `14003` | 当前模型短时饱和 | 代理对同一个请求最多重试 4 次（约 2/5/10/20 秒）；仍失败再返回 429 |
| `6004` | 账号频率窗口用尽 | 不重试，立即返回 HTTP 400 与 `rate_limit_account`；按上游恢复时间等待或切换模型 |

Codex 对同一采样请求会退避重试，但不会生成「继续任务 / go on」用户消息。为避免与代理重试相乘，
WorkBuddy provider 的 `request_max_retries` / `stream_max_retries` 已设为 `0`。
`WB_14003_RETRIES` 只能把 `14003` 的代理重试次数调低到 `0..4`，不会让 `6004` 重试。

## 图片提示“不支持图片输入”

先检查 `~/.codex/workbuddy-catalog.json` 中目标模型的 `input_modalities`。支持视觉的模型应为
`["text", "image"]`；若是 `["text"]`，说明目录还是旧版。运行 `setup.py install` 重新生成目录，
完全退出 Codex 桌面端后新建聊天。未知模型或上游 `supportsImages: false` 不应手工改成多模态。
代理日志里默认就有一行可读摘要（不需要 `--verbose`）：

```
[wbproxy] 上游错误 429: code=14003 / too many requests / 建议等待约 1 分钟后重试，或切换到负载较低的模型。
```

## 排错顺序

1. **代理在不在**：`curl -s http://127.0.0.1:8787/health`
2. 不在 → 先看**看护**（`watch_plugin.py --status`）；没装就 `--install-autostart`，或先 `--once` 应急
3. 看护说「未激活」→ 在 Codex 里启用插件（`config.toml` 的 `[plugins."…"] enabled`）
4. 起不来 → `doctor.cmd`，看是缺桌面端、缺凭据、还是缺 cryptography
5. 起来了但报错 → 看日志 `%TEMP%\wbproxy.log`
6. 报 `11128 / 请求被拦截` → 确认插件是 1.6.0+，并重启代理。1.5.4 缺少上游要求的三个 `X-Conversation-*` 头
7. 本地 provider 报 `502 Bad Gateway` → 进程继承了失效的 `HTTP(S)_PROXY`。用 `codex-wb.cmd`，或先清除代理变量
8. 报 429 → 查日志区分 `14003` / `6004`；前者重试耗尽后换模型或稍等，后者立即换模型或等恢复时间
9. CLI 报 `Unknown model` 或桌面端看不到 WorkBuddy 模型 → 确认 `wb.config.toml` 的 `model_catalog_json` 指向 `workbuddy-catalog.json`，重跑 `install.cmd`
10. 选了 `cn/*` 却报 `region_unavailable` → 国内版未安装/未登录，或 `WORKBUDDY_CN_DISABLED=1`。
    按错误里的 `app` / `auth` 路径检查；**不会**自动回退到国际版
11. 选择器里只有一组模型 → 检查 `/health` 的 `regions`：缺失区域的 `available` 为 false，
    按 `error` 补齐登录；然后重跑 `install.cmd` 刷新目录
12. 桌面端旧会话不跟随 → **新建会话**
13. 想让「新会话自动起代理」也生效 → 按上文**信任 hook**

## 合规

上游是**私有逆向接口**，绕过其 system prompt 指纹属于主动规避风控。
**自己人小范围用没问题，不要公开分发、打包发布或写教程。**
让每个使用者登录自己的账号，不要传别人的凭据文件。
