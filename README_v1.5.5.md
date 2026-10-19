# codex-workbuddy-connect 1.5.5

发布日期：2026-10-20

1.5.5 修复了 1.5.4 安装后仍无法从 Codex 调用 WorkBuddy 模型的三个独立问题，
并补齐了桌面端启动入口。此版本继续只修改 WorkBuddy 专用的 provider/profile，
不会把 WorkBuddy 设为全局默认模型。

## 1.5.4 的失败原因

### 1. 上游拒绝聊天请求：`400 / code 11128`

WorkBuddy 上游要求每次 chat 请求携带三个会话标识头：

```text
X-Conversation-ID
X-Conversation-Request-ID
X-Conversation-Message-ID
```

1.5.4 没有发送这些头，因此请求虽然能够到达上游，但会返回
`request illegal / 请求被拦截`。受控对比显示，仅补齐三个头即可从 HTTP 400
恢复为 HTTP 200。

### 2. Codex 不认识 WorkBuddy 模型

1.5.4 会生成 `workbuddy-catalog.json`，但生成的 `wb.config.toml` 没有绑定
`model_catalog_json`。结果是 Codex 报 `Unknown model`，模型元数据和工具定义
回退到默认值，桌面端模型选择器也可能无法正确显示 WorkBuddy 模型。

### 3. 失效代理变量阻断本地链路

如果 Codex 继承了失效的 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`，访问
`http://127.0.0.1:8787` 时可能返回 502，请求根本到不了本地代理。

## 1.5.5 的修改

- 为 chat 和探测请求生成三个新的 `X-Conversation-*` UUID；额度请求仍使用独立的
  账户头，不携带会话标识。
- 在 `wb.config.toml` 中写入绝对路径的 `model_catalog_json`，并让 `status` 检查
  模型目录是否存在、profile 是否引用目录、目录是否包含当前模型。
- Responses 到 Chat 的转换支持对象形式的 `tool_choice`；`tool_choice = "none"`
  时不再向上游发送工具定义。
- `codex-wb.cmd`、`start-wbproxy.cmd` 和代理自身默认清理失效的代理环境变量。
  需要企业代理时设置 `WB_KEEP_PROXY=1`。
- 未预期异常会写入脱敏日志，不记录 Authorization、访问令牌、刷新令牌、Cookie、
  完整请求体或凭据文件内容。
- 新增 `scripts/start-codex-app-wb.ps1`，用于从 Codex 桌面端单次启动时注入
  WorkBuddy provider、模型目录和默认模型。
- 插件版本提升到 1.5.5，新增覆盖请求头、错误脱敏、工具转换、模型目录和桌面端
  启动脚本的单元测试。

## 安装与升级

```cmd
codex plugin marketplace upgrade workbuddy
codex plugin add codex-workbuddy-connect@workbuddy
```

随后找到实际插件目录并执行安装器：

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"
python "<插件根>\bin\setup.py" doctor
python "<插件根>\bin\setup.py" install
python "<插件根>\bin\setup.py" status
```

安装器是幂等的，只会更新 WorkBuddy 的 provider/profile、模型目录和插件附带配置。

## 在 Codex 桌面端使用

1. 完全退出 Codex 桌面端。
2. 确认 WorkBuddy 代理已启动且健康：

   ```powershell
   curl.exe --noproxy "*" http://127.0.0.1:8787/health
   ```

3. 从 PowerShell 启动桌面端，并传入工作区路径：

   ```powershell
   powershell -ExecutionPolicy Bypass -File `
     "<插件根>\scripts\start-codex-app-wb.ps1" `
     D:\your-workspace
   ```

4. 在桌面端**新建聊天**，选择 WorkBuddy 模型并发送消息。
5. 用代理观察到的值确认实际模型：

   ```powershell
   curl.exe --noproxy "*" http://127.0.0.1:8787/wb/last-model
   ```

`model` 字段是代理实际转发给上游的模型；`config_model` 只是新建会话的默认值。

## 验证结果

本机验证环境为 Windows、Codex CLI `0.163.0-alpha.10.2`、WorkBuddy AI 桌面端
`37.10.3-42`，插件 1.5.5 显示为 `installed, enabled`。

- 单元测试：6/6 通过。
- 安装状态：provider、profile、模型目录、模型目录引用和模型 slug 检查全部通过。
- 免费模型：`deepseek-v4.1-flash` 和 `hy3` 均完成端到端请求。
- 流式与非流式 Responses：均完成验证。
- 工具续轮：shell 工具执行成功并返回最终结果。
- 异常路径：无效模型返回可读上游 4xx；缺失凭据提示重新登录。
- 收费模型：`deepseek-v4.1-flash-sg` 完成一次最小调用。
- 日志检查：未发现 Bearer、访问令牌、刷新令牌或 Cookie 泄露。

详细命令、根因证据和脱敏记录见
[`docs/workbuddy-codex-validation.md`](docs/workbuddy-codex-validation.md)。

## 限制与安全

- WorkBuddy 使用私有接口；会话头、限流、模型目录和计费规则可能变化。
- 代理只绑定 `127.0.0.1`，但本地请求没有额外 bearer 鉴权；同机进程理论上可以调用。
- 不要公开凭据、Cookie 或完整 HTTP 请求转储；不要高频调用免费模型。
- 429 限流和 600 秒真实超时未主动制造，相关结果仍标记为未验证。

## 回滚

停止本插件代理和看护的精确 PID，恢复安装前的 `config.toml`、`wb.config.toml`、
`AGENTS.md` 备份，然后重新安装 1.5.4。不要按进程名批量结束 Python 进程，也不要
递归删除整个 Codex 缓存目录。
