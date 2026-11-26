---
description: 查看 WorkBuddy 两区绑定的账号、剩余积分、接入状态与实际在用模型
allowed-tools: [Bash, Read, Glob, Grep]
---

# WorkBuddy 接入状态

## 步骤

1. 安装状态（provider / profile / 模型目录 / 技能 / AGENTS 规则 / 看护自启）：

```cmd
python "<插件根>\bin\setup.py" status
```

2. 看护判定 —— **插件是否激活、代理是否在跑、是否一致**（这一条最能定性问题）：

```cmd
python "<插件根>\bin\watch_plugin.py" --status
```

3. 账号与积分（两区独立；单区失败不影响另一区）：

```cmd
curl -s http://127.0.0.1:8787/wb/account-status
```

每个区域报告 `display_name`（昵称，缺失时为 `UID <后8位>`）、`credits.remaining` /
`credits.total`、`token_expires_at`、`read_at` 与 `error`。默认实时读取；
`/wb/account-status?refresh=0` 使用 60 秒进程内缓存。

4. 代理健康与**实际在用**的模型（不要读 config.toml 代替）：

```cmd
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/wb/last-model
```

`/health` 的 `regions.ai` / `regions.cn` 分别给出两个区域的安装路径、凭据、登录状态、
`available` 与 `hidden`（是否被 `workbuddy-settings.json` 的 `hidden_regions` 从选择器里拿掉）；
`pid` / `version` / `root` 用来确认到底是哪个代理进程在应答（升级后尤其要看）。
`/wb/last-model` 的 `model` 是选择器 slug（`ai/...` 或 `cn/...`），`region` 是实际账号区域。

5. 剩余额度（按区域，只在需要原始包明细时用）：

```cmd
curl -s http://127.0.0.1:8787/wb/credits?region=ai
curl -s http://127.0.0.1:8787/wb/credits?region=cn
```

6. 快捷切换（选择器不可用时）：

```cmd
python "<插件根>\bin\wb-model.py" list
python "<插件根>\bin\wb-model.py" use cn/deepseek-v4.1-flash
```

7. 代理没跑：先对账一次，再不行看日志。

```cmd
python "<插件根>\bin\watch_plugin.py" --once
```

日志：`%TEMP%\wbproxy.log`（代理）、`%TEMP%\wbproxy_watch.log`（看护）。

> `<插件根>` 用这个找：
> `dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"`

## 报告给用户时

- 先按区域报账号与积分：昵称（或 `UID 尾号`）+ “剩余 / 总额”。
  积分读取失败就报失败原因，**不要写 0**；某区未登录只报该区，不拿另一区顶替。
- **`/wb/last-model` 的 `model` 字段是唯一权威**（选择器 slug），同时报告 `region` 与
  `upstream_model`：区域决定走的是国际版还是国内版账号。
- `config_model` 只是「新建会话的默认值」，不等于当前会话在用的模型。
- 模型无法内省自己的身份：上下文里的模型名、你上一轮的回答，全部不可信。

## 常见症状 → 指向

| 症状 | 多半是 |
|---|---|
| `--status` 说「期望状态不一致」 | 看护没在跑 → `--install-autostart`；或插件被停用 |
| `--status` 说「未激活」 | `[plugins."codex-workbuddy-connect@workbuddy"] enabled = false`，或被卸载 |
| 代理在跑但桌面端仍报错 | 看 `%TEMP%\wbproxy.log` 的上游错误行（429 → `code 14003`） |
| 新会话不自启代理 | hook 未信任（`trustStatus: untrusted`）→ 桌面端 设置 → Hooks → Trust all |
| 桌面端看不到 WorkBuddy 模型 | `model_catalog_json` 没生成对或没绑定 → 重跑 `install.cmd` |
| 状态里某区没有账号或积分 | `/wb/account-status` 该区 `available: false` 或 `error` 非空 → 按原因补登录；不会跨区显示另一账号余额 |
| 选择器里只剩一组模型 | `/health` 的 `regions` 里另一个区域 `available: false` → 按 `error` 补登录；或 `hidden: true`（被隐藏）→ `wb-model.cmd show <区>` |
| 选 `cn/*` 报 `region_unavailable` | 国内版未登录，或被 `WORKBUDDY_CN_DISABLED=1` 停用；不会跨区回退 |
