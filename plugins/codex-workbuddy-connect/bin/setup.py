"""codex-workbuddy-connect 安装 / 卸载器。

做四件事（全部幂等，可反复执行）：

  1. 在 $CODEX_HOME/config.toml 写入 [model_providers.workbuddy] 段
  2. 写入 $CODEX_HOME/wb.config.toml（profile 本体；0.162 起 profile 必须是独立文件）
  3. 生成 $CODEX_HOME/workbuddy-catalog.json（模型切换列表的数据源）
  4. 安装 query-current-model 技能，并可选地在 AGENTS.md 里加一条「现查当前模型」规则

两个 WorkBuddy 区域（国际版 / 国内版）共用一个代理与一个模型目录；安装时会先停掉
本插件遗留的旧代理进程，避免多个进程同时监听 8787。

用法：
    python setup.py install      [--no-skill] [--no-agents-rule] [--port 8787]
    python setup.py uninstall    [--yes]
    python setup.py status [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wbspawn  # noqa: E402  （无控制台启动）
import wbdiscover  # noqa: E402
import wbsettings  # noqa: E402
from wbdiscover import console_encodings  # noqa: E402

HERE = Path(__file__).resolve().parent          # .../bin
PKG_ROOT = HERE.parent                           # .../codex-workbuddy-connect
PROVIDER = "workbuddy"
PROFILE_NAME = "wb"
MARK_BEGIN = "<!-- BEGIN codex-workbuddy-connect -->"
MARK_END = "<!-- END codex-workbuddy-connect -->"

PROVIDER_BLOCK = f"""[model_providers.{PROVIDER}]
name = "WorkBuddy"
# 固定回环 IP，避免 localhost/DNS 偶发解析失败（WinError 11001）。
base_url = "http://127.0.0.1:{{port}}/v1"
# 本地代理不校验密钥，占位即可（真正的登录态在代理里）
experimental_bearer_token = "workbuddy-local"
wire_api = "responses"
requires_openai_auth = false
# 429 由本地代理分类处理：14003 有界重试，6004 立即返回。
# Codex 不再重试 429，避免两层重试相乘放大请求。
request_max_retries = 0
stream_max_retries = 0
"""

def profile_body(home: Path, model: str = "cn/deepseek-v4.1-flash") -> str:
    """Render the WorkBuddy profile, including its own model catalog."""
    catalog_path = home / "workbuddy-catalog.json"
    catalog_text = catalog_path.as_posix()
    # Keep an already-absolute Windows drive path intact when tests run on
    # POSIX; Path.resolve() would otherwise prefix the current directory.
    if re.match(r"^[A-Za-z]:/", catalog_text):
        catalog = catalog_text
    elif catalog_path.is_absolute():
        catalog = catalog_text
    else:
        catalog = str(catalog_path.absolute()).replace("\\", "/")
    return f"""# Codex profile：走 WorkBuddy（本地代理）
#
# 用法：codex --profile {PROFILE_NAME}
# 前提：代理在跑 → scripts\\start-wbproxy.cmd（或 scripts\\codex-wb.cmd 会自动拉起）
# provider 本体在 config.toml 的 [model_providers.{PROVIDER}]

model_provider = "{PROVIDER}"
# 选择器里的 slug 带区域前缀；不带前缀的旧写法仍按国际版处理。
model = "{model}"
model_reasoning_effort = "low"
model_catalog_json = "{catalog}"
"""

AGENTS_RULE = f"""{MARK_BEGIN}
## 回答「当前模型」必须现查（不得凭上下文）

被问到「当前模型 / 现在用的什么模型 / which model / 换模型了吗」等身份类问题时：

**必须现场执行以下命令取数，禁止依据对话历史或记忆回答。**

```bash
curl -s http://127.0.0.1:{{port}}/wb/last-model
```

规则：

- 报告 `model` 字段（选择器 slug，如 `cn/hy3`）以及 `region` / `upstream_model`。
  代理是唯一权威：`region` 说明走的是国际版还是国内版账号。
- `config_model` 只是「新建会话的默认值」，不等于当前会话在用的模型。
- **模型无法内省自己的身份**：上下文里出现过的模型名、你自己上一轮的回答，全部不可信——
  用户可以在会话中途切换模型，历史消息里没有任何痕迹。
- 每次都重新执行上面的命令，不要复用本会话早先拿到的值。
- 用户说「国内版 / CN / codebuddy」时指 `cn/*`；「国际版 / WorkBuddy AI」时指 `ai/*`。
{MARK_END}
"""


# ── 基础工具 ────────────────────────────────────────────────
def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def usable_regions() -> list[str]:
    """Regions that are both installed and signed in on this machine."""
    out: list[str] = []
    for region in wbdiscover.REGION_ORDER:
        found = wbdiscover.discover_region(region)
        if found["app_exists"] and found["auth_exists"]:
            out.append(region)
    return out


def proxy_python() -> str:
    """挑一个带 cryptography 的 python；挑不到就用当前解释器。"""
    return wbdiscover.find_python() or sys.executable


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_proxy(port: int, wait: int = 25) -> bool:
    """代理没跑就拉起来，等到端口可用。"""
    if port_open(port):
        return True
    script = HERE / "wbproxy.py"
    if not script.is_file():
        print(f"  找不到 {script}")
        return False
    print(f"  启动代理：{script}")
    log = Path(os.environ.get("TEMP", ".")) / "wbproxy.log"
    try:
        # 必须用 wbspawn：venv 的 python.exe 是 uv 存根，转发时不传 creationflags，
        # 真实解释器会自己弹出一个可见控制台（实测 MainWindowHandle != 0）。
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(script), "--port", str(port)],
                               log=log)
    except OSError as e:
        print(f"  启动失败：{e}")
        return False
    for _ in range(wait * 2):
        time.sleep(0.5)
        if port_open(port):
            return True
    print(f"  代理在 {wait} 秒内没有就绪，日志：{log}")
    return False


def _listening_pids(port: int) -> list[str]:
    try:
        r = wbspawn.run_hidden(["netstat", "-ano", "-p", "TCP"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[str] = []
    needle = f":{port}"
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(needle) or parts[3].upper() != "LISTENING":
            continue
        if parts[4] not in pids:
            pids.append(parts[4])
    return pids


def _pid_command(pid: str) -> str:
    try:
        r = wbspawn.run_hidden(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def stop_stale_proxies(port: int) -> list[str]:
    """Stop every wbproxy listener on the port, whatever install path it uses.

    Codex caches plugins by version and the watcher may still be running an
    older copy. Two proxies on one port split requests between accounts, so an
    upgrade stops all of them before the new one starts. Only processes whose
    command line actually names ``wbproxy`` are touched.
    """
    stopped: list[str] = []
    for pid in _listening_pids(port):
        cmd = _pid_command(pid)
        if "wbproxy" not in cmd.lower():
            continue
        try:
            wbspawn.run_hidden(["taskkill", "/F", "/PID", pid],
                              capture_output=True, timeout=30)
            stopped.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    if stopped:
        for _ in range(20):
            if not port_open(port):
                break
            time.sleep(0.25)
    return stopped


def backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    dst = path.with_name(path.name + time.strftime(".bak-cwbc-%H%M%S"))
    shutil.copy2(path, dst)
    return dst


# ── 读写文本并保留原换行符 ──────────────────────────────────
# Codex 在 Windows 上写 CRLF。若用默认的文本模式读写，会把整份配置的换行符改成 LF，
# 表现为 diff 里「整个文件都变了」——必须按二进制原样读写。
def read_text_keep(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text_keep(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def to_newline(text: str, nl: str) -> str:
    """把文本统一成指定换行符（先把已有换行归一成 \\n 再替换）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


# ── TOML 段落的插入 / 移除（按文本行操作，保留其余内容与注释）──
def strip_section(text: str, header: str) -> str:
    """删掉名为 header 的 [section]（含其后续行，直到下一个 [ 或文件尾）。"""
    lines = text.splitlines(keepends=True)
    out, i, n = [], 0, len(lines)
    removed = False
    while i < n:
        if lines[i].strip() == header:
            removed = True
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            # 连同上一行遗留的注释块也顺手让位于下一轮处理
            continue
        out.append(lines[i])
        i += 1
    return "".join(out) if removed else text


def upsert_section(text: str, header: str, body: str) -> str:
    """替换/追加一个 [section] 段。

    必须幂等：同一份输入反复执行，输出字节不变。上一版在追加前无条件补一个分隔空行，
    而 strip_section 不回收上次留下的那一行，于是每装一次就多一行空行（实测 159→160→161 行）。
    这里先把尾部空行清干净，再统一补「恰好一个」分隔空行。
    """
    nl = detect_newline(text)
    text = strip_section(text, header)
    head = text.rstrip("\r\n")
    body_out = to_newline(body, nl).rstrip("\r\n")
    if not head:
        return body_out + nl
    return head + nl + nl + body_out + nl


# ── 各步骤 ──────────────────────────────────────────────────
def step_provider(home: Path, port: int) -> Path | None:
    cfg = home / "config.toml"
    if not cfg.is_file():
        print(f"  !! 找不到 {cfg}，请先安装并至少运行一次 Codex")
        return None
    bak = backup(cfg)
    text = read_text_keep(cfg)
    text = upsert_section(text, f"[model_providers.{PROVIDER}]", PROVIDER_BLOCK.format(port=port))
    write_text_keep(cfg, text)
    print(f"  已写入 [model_providers.{PROVIDER}]" + (f"（备份：{bak.name}）" if bak else ""))
    return cfg


def step_profile(home: Path) -> Path | None:
    prof = home / f"{PROFILE_NAME}.config.toml"
    model = "cn/deepseek-v4.1-flash"
    if prof.is_file():
        bak = backup(prof)
        # Reinstalling must not silently reset a model the user picked.
        m = re.search(r'^model\s*=\s*"([^"]+)"', read_text_keep(prof), re.M)
        if m:
            model = m.group(1)
            # Older profiles used a bare international id. Keep the user's
            # choice, but make it explicit so the selector and logs agree.
            if not model.startswith(("ai/", "cn/")):
                model = "ai/" + model
        print(f"  已存在 {prof.name}" + (f"，备份：{bak.name}" if bak else ""))
    write_text_keep(prof, to_newline(profile_body(home, model), "\r\n"))
    print(f"  已写入 profile：{prof.name}（默认模型 {model}；用法：codex --profile {PROFILE_NAME}）")
    return prof


def step_catalog(home: Path, port: int) -> Path | None:
    out = home / "workbuddy-catalog.json"
    gen = HERE / "gen-catalog.py"
    r = wbspawn.run_hidden([proxy_python(), str(gen), "--port", str(port), "--out", str(out), "--quiet"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"  生成目录失败：{(r.stderr or r.stdout).strip()[:300]}")
        return None
    print(f"  已生成模型目录：{out.name}")
    return out


def step_skill(home: Path) -> None:
    src = PKG_ROOT / "skills" / "query-current-model"
    if not src.is_dir():
        print("  包里没有 skills/query-current-model，跳过")
        return
    dst = home / "skills" / "query-current-model"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  已安装技能：{dst}")


def step_agents_rule(home: Path, port: int) -> None:
    agents = home / "AGENTS.md"
    text = read_text_keep(agents) if agents.is_file() else ""
    nl = detect_newline(text) if text else "\r\n"
    rule = to_newline(AGENTS_RULE.format(port=port), nl).rstrip("\r\n")
    if MARK_BEGIN in text:                      # 已装过 → 先移除旧版再写，便于升级
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", text, flags=re.S)
    # 与 upsert_section 同理：先清掉上一轮留下的分隔空行，再补「恰好一个」。
    # 不清就会每装一次多一行空行（实测 65a66 / 66a67）。
    text = text.rstrip("\r\n")
    if text:
        text += nl + nl
    bak = backup(agents) if agents.is_file() else None
    write_text_keep(agents, text + rule + nl)
    print(f"  已在 AGENTS.md 加入「现查当前模型」规则" + (f"（备份：{bak.name}）" if bak else ""))


def step_watch(enable: bool) -> None:
    """把「按插件状态看护代理」装上登录自启。

    这是 hook 覆盖不到的部分：hook 只在会话开始时触发一次，管不了
    「会话进行中代理挂掉」「重启后还没开会话」「不用 Codex」这三种情况。
    """
    watcher = HERE / "watch_plugin.py"
    if not watcher.is_file():
        print("  跳过看护（找不到 bin/watch_plugin.py）")
        return
    exe = proxy_python()
    pyw = Path(exe).with_name("pythonw.exe")
    runner = str(pyw) if pyw.is_file() else exe
    action = "install-autostart" if enable else "remove-autostart"
    r = wbspawn.run_hidden([exe, str(watcher), f"--{action}"],
                       capture_output=True, text=True, errors="replace")
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        print(f"  看护自启失败（退出码 {r.returncode}）：{(out or r.stderr or '').strip()[:200]}")
        print(f"  可手动重试：{runner} \"{watcher}\" --{action}")
        return
    print(("  已安装" if enable else "  已移除") + "代理看护自启："
          + ("插件激活时代理自动在跑，未激活则自动停掉" if enable else ""))


def step_models_note(port: int) -> None:
    """列出可用模型，方便用户挑默认值。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=20) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  （读不到模型列表：{e}）")
        return
    ids = [m.get("id") for m in (doc.get("data") or []) if m.get("id")]
    ai = [x for x in ids if str(x).startswith("ai/")]
    cn = [x for x in ids if str(x).startswith("cn/")]
    print(f"  可用模型 {len(ids)} 个（国际 {len(ai)} / 国内 {len(cn)}）")
    hidden = wbsettings.hidden_regions()
    for region in wbdiscover.REGION_ORDER:
        if region in hidden:
            print(f"  {wbdiscover.REGIONS[region].label}目前在设置里隐藏，不进入选择器"
                  f"（恢复：scripts\\wb-model.cmd show {region}）")
    print("  列表与 WorkBuddy 桌面端一致（档位快捷项「快速/均衡/极致」不进选择器）")
    print("  切换全局 provider/默认：scripts\\wb-model.cmd use cn/deepseek-v4.1-flash")


# ── 命令 ────────────────────────────────────────────────────
def cmd_install(args) -> int:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"安装到 {home}")
    print(f"包目录 {PKG_ROOT}")

    if not args.skip_doctor:
        print("\n[0/6] 环境体检")
        bad = [c for c in wbdiscover.diagnose(args.port) if not c[1]]
        for name, ok, detail in wbdiscover.diagnose(args.port):
            print(f"  [{'OK ' if ok else '!! '}] {name}: {detail}")
        # 代理没在跑不算致命（下一步会拉起）。两个区域任一可用即可安装；
        # 只有工具链缺失，或两个区域都不可用时才阻断。
        fatal = [c for c in bad if "端口" not in c[0] and "国际版" not in c[0] and "国内版" not in c[0]]
        if not usable_regions():
            print("\n  需要至少一个 WorkBuddy 桌面端已安装并登录（国际版或国内版均可）。")
            if not args.force:
                return 1
        if fatal:
            print("\n  上面标 !! 的项需要先解决。修好后重跑 install.cmd。")
            if not args.force:
                return 1

    print("\n[1/6] 确保本地代理可用（生成目录需要它）")
    stopped = stop_stale_proxies(args.port)
    if stopped:
        print(f"  已停止旧代理进程：{', '.join(stopped)}（避免多进程抢占同一端口）")
    if not ensure_proxy(args.port):
        print("  代理不可用。请先启动并登录任意一个 WorkBuddy 桌面端（国际版或国内版），再重试。")
        return 1
    print("  OK")

    print("\n[2/6] 写入 provider")
    if not step_provider(home, args.port):
        return 1

    print("\n[3/6] 写入 profile")
    step_profile(home)

    print("\n[4/6] 生成模型目录")
    step_catalog(home, args.port)

    print("\n[5/6] 附加项")
    if args.no_skill:
        print("  跳过技能安装（--no-skill）")
    else:
        step_skill(home)
    if args.no_agents_rule:
        print("  跳过 AGENTS.md 规则（--no-agents-rule）")
    else:
        step_agents_rule(home, args.port)

    step_models_note(args.port)

    if args.no_watch:
        print("  跳过代理看护自启（--no-watch）")
    else:
        step_watch(True)

    print("\n完成。用法：")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wb.cmd'}                 # TUI（自动拉起代理）")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wb.cmd'} exec \"任务\"      # 非交互")
    print(f"  codex --profile {PROFILE_NAME}                            # 已装 Codex 时")
    print(f"  {PKG_ROOT / 'scripts' / 'start-codex-app-wb.cmd'} [工作区]  # 桌面端（先完全退出 Codex）")
    print("\n注意：桌面端切换模型不会写回 config.toml，代理会自动同步；")
    print("      旧的会话记住自己创建时的模型，改用「新聊天」才走新默认值。")
    return 0


def cmd_uninstall(args) -> int:
    home = codex_home()
    print(f"从 {home} 卸载")
    stop_stale_proxies(args.port)
    cfg = home / "config.toml"
    if cfg.is_file():
        bak = backup(cfg)
        text = strip_section(read_text_keep(cfg), f"[model_providers.{PROVIDER}]")
        write_text_keep(cfg, text)
        print(f"  已移除 [model_providers.{PROVIDER}]" + (f"（备份：{bak.name}）" if bak else ""))

    prof = home / f"{PROFILE_NAME}.config.toml"
    if prof.is_file():
        prof.unlink()
        print(f"  已删除 {prof.name}")

    cat = home / "workbuddy-catalog.json"
    if cat.is_file():
        cat.unlink()
        print(f"  已删除 {cat.name}")

    dst = home / "skills" / "query-current-model"
    if dst.is_dir():
        shutil.rmtree(dst)
        print(f"  已删除技能 {dst}")

    agents = home / "AGENTS.md"
    if agents.is_file() and MARK_BEGIN in read_text_keep(agents):
        bak = backup(agents)
        cur = read_text_keep(agents)
        nl = detect_newline(cur)
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", cur, flags=re.S)
        write_text_keep(agents, text.rstrip("\r\n") + nl)
        print(f"  已移除 AGENTS.md 规则" + (f"（备份：{bak.name}）" if bak else ""))

    step_watch(False)

    print("\n完成。代理进程若还在跑，可手动结束；包目录未删除。")
    return 0


def cmd_doctor(args) -> int:
    """环境体检：不安装任何东西，只报告缺什么、怎么补。"""
    home = codex_home()
    print(f"CODEX_HOME : {home}")
    print(f"包目录      : {PKG_ROOT}")
    print()
    rows = wbdiscover.diagnose(args.port)
    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")

    bad = [r for r in rows if not r[1] and "端口" not in r[0]
           and "国际版" not in r[0] and "国内版" not in r[0]]
    print()
    if not usable_regions():
        print("需要处理：至少需要一个 WorkBuddy 桌面端已安装并登录（国际版或国内版均可）。")
        return 1
    if bad:
        print("需要处理：")
        for name, _, _ in bad:
            tip = {
                "Python >=3.10": "装 Python 3.10+（安装时勾选 Add python.exe to PATH），或设 WORKBUDDY_AI_PYTHON",
                "cryptography": '用上一步的 python 执行：python -m pip install cryptography',
                "WorkBuddy 国际版": "装 WorkBuddy AI 桌面端并登录；或用 WORKBUDDY_AI_APP 指定路径",
                "国际版登录凭据": "启动国际版并完成登录，凭据会自动写出；或用 WORKBUDDY_AI_AUTH_FILE 指定",
                "WorkBuddy 国内版": "装 WorkBuddy 桌面端并登录；或用 WORKBUDDY_CN_APP 指定路径（不装也能只用国际版）",
                "国内版登录凭据": "启动国内版并完成登录；或用 WORKBUDDY_CN_AUTH_FILE 指定",
                "Codex CLI": "npm i -g @openai/codex，或用 CODEX_WB_CODEX 指定",
            }.get(name, "")
            if tip:
                print(f"  - {name}：{tip}")
        return 1
    optional = [r for r in rows if not r[1] and ("国际版" in r[0] or "国内版" in r[0])]
    if optional:
        print("可选：未就绪的区域不会出现在选择器里；登录后重跑 install.cmd 即可加入。")
    print("环境就绪，可以运行 install.cmd")
    return 0


def fetch_account_status(port: int) -> dict[str, dict]:
    """Ask the proxy for both regions' account + credit summary.

    Missing proxy is not an install failure: it returns one unavailable entry
    per region so `status` stays a diagnostic, not a gate.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/wb/account-status", timeout=60
        ) as r:
            document = json.loads(r.read().decode("utf-8", "replace"))
        regions = document.get("regions")
        if isinstance(regions, dict):
            return dict(regions)
    except Exception as e:
        if port_open(port):
            detail = "当前运行的代理没有 /wb/account-status 端点；重跑 install.cmd 重启代理后再查"
        else:
            detail = f"代理未就绪：{type(e).__name__}: {e}"
        return {
            region: {
                "region": region,
                "label": wbdiscover.REGIONS[region].label,
                "available": False,
                "nickname": None,
                "display_name": None,
                "uid_tail": None,
                "enterprise_id": None,
                "credits": None,
                "error": detail,
            }
            for region in wbdiscover.REGION_ORDER
        }
    return {}


def fetch_last_model(port: int) -> dict | None:
    """The proxy's last forwarded model; None when it cannot answer."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/wb/last-model", timeout=10
        ) as r:
            document = json.loads(r.read().decode("utf-8", "replace"))
        return document if isinstance(document, dict) else None
    except Exception:
        return None


def status_install_section(home: Path) -> dict:
    """The install self-check as data, printed by the text renderer."""
    cfg = home / "config.toml"
    text = read_text_keep(cfg) if cfg.is_file() else ""
    prof = home / f"{PROFILE_NAME}.config.toml"
    profile_text = read_text_keep(prof) if prof.is_file() else ""
    catalog_file = home / "workbuddy-catalog.json"
    provider_match = re.search(r'^model_provider\s*=\s*"([^"]+)"', text, re.M)
    global_catalog_match = re.search(r'^model_catalog_json\s*=\s*"([^"]+)"', text, re.M)
    global_provider = provider_match.group(1) if provider_match else None
    global_catalog = global_catalog_match.group(1) if global_catalog_match else None
    catalog_slugs: set[str] = set()
    if catalog_file.is_file():
        try:
            catalog_doc = json.loads(read_text_keep(catalog_file))
            catalog_slugs = {
                str(item.get("slug")) for item in catalog_doc.get("models", [])
                if isinstance(item, dict) and item.get("slug")
            }
        except Exception:
            catalog_slugs = set()
    model_match = re.search(r'^model\s*=\s*"([^"]+)"', profile_text, re.M)
    profile_model = model_match.group(1) if model_match else None
    counts: dict[str, int] = {}
    for slug in catalog_slugs:
        region = "cn" if slug.startswith("cn/") else "ai" if slug.startswith("ai/") else None
        if region is not None:
            counts[region] = counts.get(region, 0) + 1
    startup = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup" / "codex-workbuddy-connect-watch.vbs")
    ag = home / "AGENTS.md"
    return {
        "codex_home": str(home),
        "provider_installed": f"[model_providers.{PROVIDER}]" in text,
        "global_provider": global_provider,
        "global_catalog": global_catalog,
        "profile_file": prof.is_file(),
        "catalog_file": catalog_file.is_file(),
        "catalog_linked": catalog_file.resolve().as_posix() in profile_text.replace("\\", "/"),
        "catalog_has_profile_model": profile_model in catalog_slugs if profile_model else False,
        "profile_model": profile_model,
        "skill_installed": (home / "skills" / "query-current-model").is_dir(),
        "agents_rule": ag.is_file() and MARK_BEGIN in read_text_keep(ag),
        "catalog_models": counts,
        "hidden_regions": sorted(wbsettings.hidden_regions()),
        "watch_autostart": startup.is_file(),
    }


def build_status_document(home: Path, port: int) -> dict:
    """Stable machine-readable status: install + regions + last_model."""
    return {
        "install": status_install_section(home),
        "regions": fetch_account_status(port),
        "last_model": fetch_last_model(port),
    }


def display_number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def display_time(value) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "-"


def render_account_section(regions: dict[str, dict]) -> list[str]:
    """Text view of both regions; failures are labelled, never zeroed."""
    lines = ["", "账号与积分"]
    for region in wbdiscover.REGION_ORDER:
        item = regions.get(region) or {}
        label = item.get("label") or wbdiscover.REGIONS[region].label
        if not item.get("available"):
            lines.append(f"  {label:<6}: 不可用 — {item.get('error') or '未登录'}")
            continue
        name = item.get("display_name") or "未知账号"
        line = f"  {label:<6}: {name}"
        if item.get("enterprise_id"):
            line += f"（企业 {item['enterprise_id']}）"
        lines.append(line)
        credits = item.get("credits")
        if not isinstance(credits, dict):
            lines.append(f"          积分：读取失败 — {item.get('error') or '未知原因'}")
        elif credits.get("unlimited") is True:
            lines.append("          积分：企业额度 · 不限")
        elif credits.get("remaining") is None or credits.get("total") is None:
            lines.append("          积分：尚未读取")
        else:
            lines.append(
                f"          积分：剩余 {display_number(credits['remaining'])}"
                f" / {display_number(credits['total'])}"
            )
        reset = (credits.get("cycleResetTime") if isinstance(credits, dict) else None) \
            or item.get("cycleResetTime")
        if reset:
            lines.append(f"          周期重置：{reset}")
        if item.get("token_expires_at"):
            lines.append(f"          令牌有效期至：{display_time(item['token_expires_at'])}")
        if item.get("read_at"):
            lines.append(f"          读取时间：{display_time(item['read_at'])}")
    return lines


def render_status_text(document: dict, port: int) -> str:
    install = document["install"]
    lines = [
        f"CODEX_HOME          : {install['codex_home']}",
        f"provider 段已安装    : {install['provider_installed']}",
        f"全局 provider        : {install['global_provider']}",
        f"全局模型目录        : {install['global_catalog']}",
        f"profile 文件        : {install['profile_file']}",
        f"模型目录            : {install['catalog_file']}",
        f"profile 引用目录    : {install['catalog_linked']}",
        f"目录含 profile 模型 : {install['catalog_has_profile_model']} ({install['profile_model']})",
        f"技能                : {install['skill_installed']}",
        f"AGENTS.md 规则      : {install['agents_rule']}",
        f"代理 {port} 在跑 : {port_open(port)}",
    ]
    for region in wbdiscover.REGION_ORDER:
        d = wbdiscover.discover_region(region)
        lines.append(f"{d['label']:<6} 桌面端/凭据 : {d['app_exists']}/{d['auth_exists']}  {d['app'] or '-'}")
        lines.append(f"{'':<6} 目录模型数   : {install['catalog_models'].get(region, 0)}")
    hidden = install.get("hidden_regions") or []
    lines.append("隐藏的区域          : " + (", ".join(hidden) if hidden else "无"))
    lines.append(f"代理看护自启   : {install['watch_autostart']}")
    last = document.get("last_model")
    if isinstance(last, dict):
        lines.append(f"  实际在用模型      : {last.get('model')}  (区域={last.get('region')}，上游={last.get('upstream_model')})")
        lines.append(f"  配置默认          : {last.get('config_model')} (synced={last.get('config_synced')})")
    lines.extend(render_account_section(document["regions"]))
    return "\n".join(lines)


def cmd_status(args) -> int:
    home = codex_home()
    document = build_status_document(home, args.port)
    if getattr(args, "json", False):
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(render_status_text(document, args.port))
    # A signed-out or unreadable region is a diagnosable state, not an
    # install failure; status stays 0 so scripts do not treat it as fatal.
    return 0


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="codex-workbuddy-connect 安装器")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WORKBUDDY_AI_PROXY_PORT", 8787)))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="安装（幂等）")
    p.add_argument("--no-skill", action="store_true", help="不安装 query-current-model 技能")
    p.add_argument("--no-agents-rule", action="store_true", help="不改动全局 AGENTS.md")
    p.add_argument("--skip-doctor", action="store_true", help="跳过环境体检")
    p.add_argument("--force", action="store_true", help="体检查出问题时也继续安装")
    p.add_argument("--no-watch", action="store_true", help="不安装代理看护自启")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="卸载（保留包目录与备份）")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="查看安装状态、账号积分与实际在用模型")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="环境体检（不安装任何东西）")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
