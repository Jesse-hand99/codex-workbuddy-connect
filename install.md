# 从源码本地安装（挂载模式）

本仓库既可以从 GitHub 安装发布版，也支持**直接把当前源码目录挂进 Codex**。

`mount-local.ps1` 会注册一个本地 Marketplace，并把 Codex 插件缓存里的目录用 NTFS Junction
链接回源码。这样改完代码不用重装，也不会出现「缓存里还是旧版本」的问题。

- 想改代码、调试代理、长期跟着本仓库开发 → 用本文的挂载模式。
- 只想用插件 → 见 README 的「安装（已发布）」，不需要本文。

## 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows（挂载依赖 NTFS Junction / SymbolicLink） |
| Shell | PowerShell 5.1 或更高 |
| Python | 3.10+，且能 `import cryptography` |
| Codex | 带 `codex plugin` 子命令（0.16x 及以后） |
| Git | 任意较新版本 |

WorkBuddy 桌面端（国际版 / 国内版，按需装一个或两个）需要先安装并登录，
代理才能解密出登录态。

## 一、拿到源码

```powershell
git clone https://github.com/Jesse-hand99/codex-workbuddy-connect.git
Set-Location codex-workbuddy-connect
$repo = (Get-Location).Path
```

下文命令里的 `$repo` 就是仓库根的绝对路径；`<版本>` 指 `plugins/codex-workbuddy-connect/.codex-plugin/plugin.json`
里的 `version`。

## 二、准备项目 Python

插件自带启动器会**优先**使用插件目录下的 `.venv`，所以建议把依赖装在项目里：

```powershell
$plugin = "$repo\plugins\codex-workbuddy-connect"

python -m venv "$plugin\.venv"
& "$plugin\.venv\Scripts\python.exe" -m pip install cryptography

# 让脚本、hook、看护都找到同一个解释器（写入当前用户环境变量）
[Environment]::SetEnvironmentVariable(
    'WORKBUDDY_AI_PYTHON',
    "$plugin\.venv\Scripts\python.exe",
    'User')
```

不建 `.venv` 也能跑：`scripts\_findpy.cmd` 按
`WORKBUDDY_AI_PYTHON` → 插件 `.venv` → 常见安装路径 → `PATH` 的顺序找 Python，
前提是找到的那个解释器能用 `cryptography`。

## 三、注册 Marketplace 并挂载源码

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\mount-local.ps1"
```

脚本会依次：

1. 确认本地 `workbuddy` Marketplace 已注册（没有就 `codex plugin marketplace add`）。
2. 首次安装时执行 `codex plugin add codex-workbuddy-connect@workbuddy`。
3. 把 Codex 生成的缓存副本移到可恢复的 `.link-backups` 目录。
4. 按 Codex 当前能接受的形态建立链接（见下一节的「链接方式」）。
5. 用 `codex plugin list -m workbuddy --json` 确认插件 `installed` 且 `enabled`。

脚本是幂等的，重复运行不会删除源码。被替换下来的缓存副本保存在：

```text
%USERPROFILE%\.codex\plugins\cache\workbuddy\.link-backups\
```

## 四、写入 provider、profile 和模型目录

```powershell
$plugin = "$repo\plugins\codex-workbuddy-connect"
$py = "$plugin\.venv\Scripts\python.exe"

& $py "$plugin\bin\setup.py" doctor    # 环境体检，不写任何配置
& $py "$plugin\bin\setup.py" install   # 写入 provider / profile / 目录 / 技能 / 看护自启
& $py "$plugin\bin\setup.py" status    # 安装状态 + 两区账号与积分
```

`install` 会写入：

- `%USERPROFILE%\.codex\config.toml` 中的 `[model_providers.workbuddy]`
- `%USERPROFILE%\.codex\wb.config.toml`（`wb` profile、默认模型、目录引用）
- `%USERPROFILE%\.codex\workbuddy-catalog.json`
- `%USERPROFILE%\.codex\skills\query-current-model`
- 全局 `AGENTS.md` 的「现查当前模型」规则
- 代理看护自启

修改前会备份成 `*.bak-cwbc-*`。安装器只动 WorkBuddy 自己的段，不会覆盖其他 provider。

常用开关：`--no-skill`、`--no-agents-rule`、`--no-watch`、`--skip-doctor`、`--force`。

## 五、验证

```powershell
codex plugin list -m workbuddy

$plugin = "$repo\plugins\codex-workbuddy-connect"
& "$plugin\.venv\Scripts\python.exe" "$plugin\bin\setup.py" status

curl.exe --noproxy "*" http://127.0.0.1:8787/health
curl.exe --noproxy "*" http://127.0.0.1:8787/v1/models
curl.exe --noproxy "*" http://127.0.0.1:8787/wb/account-status
```

期望：

- `codex plugin list` 显示 `installed` / `enabled`，版本与 `plugin.json` 一致。
- `setup.py status` 的安装检查项全为 `True`。
- `/health` 返回 `ok: true`，`regions` 下已登录的区域为 `available: true`。
- `/v1/models` 返回的数量 = 两个可用区域的模型之和（未登录或被 `hidden_regions`
  隐藏的区域不计入）。
- `/wb/account-status` 给出每个区域的昵称（缺昵称时回退 UID 后 8 位）、剩余 / 总积分、
  令牌有效期。

`--json` 版本供脚本读取：

```powershell
& "$plugin\.venv\Scripts\python.exe" "$plugin\bin\setup.py" status --json
```

## 目录与链接结构

| 项目 | 位置 | 作用 |
|---|---|---|
| 仓库根 | `$repo` | Marketplace 根目录 |
| 插件源码 | `$repo\plugins\codex-workbuddy-connect` | Codex 实际读取的源码 |
| 本地挂载脚本 | `$repo\mount-local.ps1` | 注册 Marketplace + 建链接 |
| 项目 Python | `<插件>\.venv\Scripts\python.exe` | 代理与安装器使用的解释器 |
| Codex 主配置 | `%USERPROFILE%\.codex\config.toml` | provider 定义 |
| Codex profile | `%USERPROFILE%\.codex\wb.config.toml` | `wb` profile 与默认模型 |
| 模型目录 | `%USERPROFILE%\.codex\workbuddy-catalog.json` | 模型选择器数据 |
| 全局技能副本 | `%USERPROFILE%\.codex\skills\query-current-model` | 查询当前模型技能 |
| 插件挂载根 | `%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect\<版本>` | Codex 读取的插件入口 |
| 插件元数据 | `<挂载根>\.codex-plugin\plugin.json` | 普通文件副本（不能是链接） |
| 代理日志 | `%TEMP%\wbproxy.log` | 运行与上游错误 |
| 看护日志 | `%TEMP%\wbproxy_watch.log` | 看护状态 |
| Hook 日志 | `%TEMP%\wb_hook.log` | `SessionStart` hook |
| 请求诊断 | `%USERPROFILE%\.codex\workbuddy-connect\wbproxy_events.jsonl` | 每请求 event / verdict |
| 区域设置 | `%USERPROFILE%\.codex\workbuddy-settings.json` | `hidden_regions` 等 |
| 登录时看护 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\codex-workbuddy-connect-watch.vbs` | 登录后隐藏启动看护 |

### 链接方式

当前 Codex 版本会拒绝把整个版本目录、`.codex-plugin` 目录、以及其中的 `plugin.json`
做成链接。因此挂载采用兼容形态：

- 版本根目录与 `.codex-plugin` 是**真实目录**；
- `plugin.json` 是**普通文件副本**（每次运行 `mount-local.ps1` 会重新同步）；
- `bin`、`commands`、`hooks`、`scripts`、`skills`、`templates`、`tests` 是指向源码的 Junction；
- 根目录下的脚本与文档是文件符号链接；
- 整个项目不会被复制到 Codex 插件区。

因此改已有源码文件后 Codex 直接读到新内容；在已链接目录里**新增**文件也会立刻可见。

## 改完代码怎么生效

| 改动 | 生效方式 |
|---|---|
| `skills/`、`commands/`、`templates/` | 新建 Codex 会话 |
| `hooks/hooks.json` | 在 Codex 设置 → Hooks 里重新信任 |
| `bin/wbproxy.py`、`wbresponses.py` 等已载入进程的代码 | 重启代理（看护会自动拉起） |
| `.codex-plugin/plugin.json` 的版本号 | 重跑一次 `mount-local.ps1`，生成新版本链接目录 |

重启代理时先确认 8787 的监听者确实是本插件的 `wbproxy.py`，只结束那个 PID，
不要按 `python.exe` 名字批量结束。

### 本地反复重装：打时间戳版本

Codex 按版本号物化到 `plugins\cache\...\<版本>`，**版本号不变就不会重新读取**。
本机调试时可以用：

```powershell
& "$plugin\.venv\Scripts\python.exe" "$plugin\bin\bump-plugin.py" $plugin
# 1.7.5 -> 1.7.5-20261126143000
```

对外发布时用干净版本号：

```powershell
& "$plugin\.venv\Scripts\python.exe" "$plugin\bin\bump-plugin.py" $plugin --set 1.7.5
```

## 卸载

先撤销安装器写入的配置：

```powershell
$plugin = "$repo\plugins\codex-workbuddy-connect"
& "$plugin\.venv\Scripts\python.exe" "$plugin\bin\setup.py" uninstall
```

再移除插件与 Marketplace：

```powershell
codex plugin remove codex-workbuddy-connect@workbuddy
codex plugin marketplace remove workbuddy
```

源码目录与 `.venv` 不会被删除；`*.bak-cwbc-*` 备份仍留在 `%USERPROFILE%\.codex`。

## 常见问题

### 插件显示未安装

检查 `<挂载根>` 是否存在，然后重跑 `mount-local.ps1`。
若报 `failed to load plugin: missing or invalid plugin.json`，说明 Codex 加载器不接受
链接形式的元数据：保持 `.codex-plugin` 为真实目录，`plugin.json` 为普通副本。

### 本地 provider 返回 502

多半是继承了失效的代理变量。先清空再启动 Codex：

```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
$env:http_proxy=''; $env:https_proxy=''; $env:all_proxy=''
$env:NO_PROXY='127.0.0.1,localhost,::1'
```

确实需要企业代理访问上游时，在代理的启动环境里设 `WB_KEEP_PROXY=1`。

### 找不到 Python 或 cryptography

```powershell
Test-Path "$plugin\.venv\Scripts\python.exe"
& "$plugin\.venv\Scripts\python.exe" -c "import cryptography; print(cryptography.__version__)"
```

`scripts\_findpy.cmd` 与 `bin\wbdiscover.py` 都优先用插件自己的 `.venv`，
不要求把 Python 加进系统 `PATH`。

### 某个区域不可用

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8787/health
```

`regions.<区>.available=false` 表示该区域没登录或凭据读不到：
启动并登录对应桌面端，再重启代理、重跑 `setup.py install` 刷新目录。
只想从选择器隐藏该区（账号仍可查）用 `scripts\wb-model.cmd hide ai|cn`，恢复用 `show`。

### 429 或模型繁忙

代理会把上游错误分类返回（如 `14003` 模型繁忙、`6004` 账户额度）。等待约一分钟、
换模型或等账户频率窗口恢复。不要把免费模型当批量 API 用。

## 与发布版安装的区别

| | 发布版（`codex plugin marketplace add`） | 源码挂载（本文） |
|---|---|---|
| 代码来源 | GitHub 上的某个提交 | 你本地的工作区 |
| 改代码后 | 需要提交版本号并重装 | 立即生效（Python 进程需重启） |
| 目录形态 | Codex 复制出的普通目录 | Junction 链接回源码 |
| 适用 | 只用插件 | 开发 / 调试 / 跟主线 |

## 安全与限制

- 代理只监听 `127.0.0.1:8787`，但没有本地 bearer 鉴权，同机进程可以访问该端口。
- 登录凭据由 WorkBuddy 桌面端写入，代理仅在内存中解密使用；不要外传凭据文件或日志。
- 插件依赖 WorkBuddy 私有接口。桌面端升级后，鉴权文件、模型清单或请求规则可能变化。
- 付费模型会产生费用；免费档与费率以上游实时目录为准。
