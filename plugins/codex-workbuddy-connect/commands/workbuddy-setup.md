---
description: 把 WorkBuddy 接入 Codex（安装 / 修复 / 重装）
argument-hint: "[--skip-doctor] [--no-watch] [--no-skill] [--no-agents-rule]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# 安装 WorkBuddy → Codex 接入

用户调用了本命令，参数：`$ARGUMENTS`

## 步骤

1. 先做体检，把结果如实报给用户：

```cmd
python "<插件根>\bin\setup.py" doctor
```

2. 若体检标了 `!!`，按提示补齐后再继续（缺 Python/cryptography、未登录 WorkBuddy 桌面端、
   缺 Codex 都属要先解决的项）。**不要**自己下载或安装 WorkBuddy 桌面端 —— 让用户自己装并登录。
   两个桌面端**至少装一个并登录**；国际版和国内版都可选，缺的那个区域会自动从选择器里消失。

3. 体检通过后安装：

```cmd
python "<插件根>\bin\setup.py" install $ARGUMENTS
```

安装会一并做这些（幂等）：provider 段、profile、合并模型目录、技能、AGENTS 规则，
以及**代理看护的登录自启**（想让代理按插件启停自动开关，就必须这一步）。
安装前会先停掉所有监听同一端口的旧 `wbproxy` 进程（按命令行核对，不误杀其它 Python），
避免多个版本同时抢 8787 导致请求随机落到不同账号。

4. 跑 status 与看护判定确认：

```cmd
python "<插件根>\bin\setup.py" status
python "<插件根>\bin\watch_plugin.py" --status
```

5. 如果用户要求 Codex 桌面端直接使用 WorkBuddy（不依赖一次性启动参数），切换全局默认：

```cmd
"<插件根>\scripts\wb-model.cmd" use cn/deepseek-v4.1-flash
```

   该命令会同时设置全局 `model_provider="workbuddy"`、`model_catalog_json` 和默认模型，
   并保留 `config.toml` 备份。必须完全退出并重启桌面端，再新建聊天。

6. 告诉用户两件事：

   - 怎么开始用：`codex --profile wb`，或者桌面端**新建会话**（旧会话逐会话绑定 provider，不跟随）。
   - 模型选择器里的名字与 WorkBuddy 桌面端一致（不再统一加 `（国内）` 后缀，只有两区同名才补），
     `ai/...` 走国际版，`cn/...` 走国内版；桌面端「快速 / 均衡 / 极致」的档位快捷项不会出现在列表里。
     某区未登录或被隐藏时只剩另一区。选择器不可用时用 `scripts\wb-model.cmd list|use`。
     国际版当前不可用的话，用 `wb-model.cmd hide ai` 把它从选择器隐藏，`show ai` 恢复。
   - **建议信任 hook**，否则「新会话自动拉起代理」不生效：
     桌面端 **设置 → Hooks → From Plugins → 本插件 SessionStart → Trust all**
     （或 TUI 新会话时选 *Trust all and continue*；自动化用
     `codex exec --dangerously-bypass-hook-trust`）。
     不信任也能用 —— 看护会兜底。

## 注意

- `<插件根>` 是插件被安装后的真实目录，用这个命令找到它：
  `dir /b /s "%USERPROFILE%\.codex\plugins\cache\workbuddy\codex-workbuddy-connect"`
- 安装是幂等的：重复运行不会改动已就绪的内容，也不会累积空行；已选的默认模型会被保留。
- 不要修改 `config.toml` 里本插件以外的段落。
- 如果安装失败（退出码非 0），把 `!!` 那行原样贴给用户，不要自己猜。
- 改了插件内容要先升版本再重装（Codex 按版本号缓存）：
  `python "<插件根>\bin\bump-plugin.py" "<插件目录>"` 然后
  `codex plugin add codex-workbuddy-connect@workbuddy`。
