#!/usr/bin/env python3
"""在 Codex 之外切换 WorkBuddy 模型（选择器不可用时的兜底）。

子命令：
    list                  列出各区域的模型（并标出被隐藏的区域）
    current               显示当前实际在用 / 新建会话默认
    use <slug>            改默认模型（新会话生效）
    hide|show <r>         从模型选择器隐藏 / 恢复某个区域（不改登录态）
    enable|disable <r>    整个区域启用/停用（不加载凭据）

示例：
    python wb-model.py list
    python wb-model.py use cn/deepseek-v4.1-flash
    python wb-model.py hide ai
    python wb-model.py show ai
    python wb-model.py disable cn
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wbspawn  # noqa: E402
import wbdiscover  # noqa: E402
import wbsettings  # noqa: E402
from wbdiscover import console_encodings  # noqa: E402

PROFILE = "wb"
REGION_ORDER = ("ai", "cn")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def port() -> int:
    try:
        return int(os.environ.get("WORKBUDDY_AI_PROXY_PORT") or 8787)
    except ValueError:
        return 8787


def proxy_json(path: str, timeout: int = 60):
    url = f"http://127.0.0.1:{port()}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def read_text(path: Path) -> str:
    # newline="" keeps CRLF intact: Path.read_text() normalizes newlines and a
    # rewrite would silently convert the whole Codex config to LF.
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError:
        return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def top_level_value(path: Path, key: str) -> str | None:
    """Read a scalar top-level key without accepting values from sections."""
    for line in read_text(path).splitlines():
        if line.lstrip().startswith("["):
            break
        match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return None


def rewrite_top_level_values(path: Path, updates: dict[str, str]) -> bool:
    """Rewrite or add only the requested top-level keys.

    Codex config files mix top-level settings, TOML sections and comments.
    Keeping this edit line-oriented preserves every unrelated byte and the
    file's CRLF/LF convention.
    """
    if not path.is_file():
        return False
    text = read_text(path)
    lines = text.splitlines(keepends=True)
    pending = dict(updates)
    changed = False
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            insert_at = i
            break
        for key in tuple(pending):
            match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$",
                             line.rstrip("\r\n"))
            if not match:
                continue
            current = match.group(1).strip().strip('"').strip("'")
            if current != pending[key]:
                nl = "\r\n" if line.endswith("\r\n") else "\n"
                lines[i] = f'{key} = "{pending[key]}"{nl}'
                changed = True
            pending.pop(key)
            break

    if pending:
        # Keep missing top-level keys above the first section, but before any
        # blank separator that already belongs to that section.
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        nl = "\r\n" if "\r\n" in text else "\n"
        for key, value in pending.items():
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += nl
            lines.insert(insert_at, f'{key} = "{value}"{nl}')
            insert_at += 1
        changed = True

    if changed:
        write_text(path, "".join(lines))
    return changed


def set_top_level_model(path: Path, model: str) -> bool:
    return rewrite_top_level_values(path, {"model": model})


def backup_file(path: Path) -> Path | None:
    if not path.is_file():
        return None
    suffix = time.strftime("%H%M%S")
    target = path.with_name(f"{path.name}.bak-wb-{suffix}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.bak-wb-{suffix}-{counter}")
        counter += 1
    shutil.copy2(path, target)
    return target


def profile_model(path: Path) -> str | None:
    m = re.search(r'^model\s*=\s*"([^"]+)"', read_text(path), re.M)
    return m.group(1) if m else None


def _region_of(row: dict) -> str:
    region = row.get("region")
    if region in REGION_ORDER:
        return region
    slug = str(row.get("slug") or "")
    return "cn" if slug.startswith("cn/") else "ai"


def cmd_list(args) -> int:
    try:
        doc = proxy_json("/v1/models")
    except Exception as e:
        print(f"读不到模型列表（代理没跑？）：{e}")
        return 1
    rows = doc.get("models") or []
    if not rows:
        print("代理没有返回任何模型；检查两个桌面端是否已登录。")
        return 1
    hidden = set(doc.get("hidden_regions") or [])
    for region in REGION_ORDER:
        group = [r for r in rows if _region_of(r) == region]
        label = wbdiscover.REGIONS[region].label
        if region in hidden:
            print(f"\n[{region}] {label}（已隐藏，选择器里不显示）")
            continue
        if not group:
            continue
        print(f"\n[{region}] {label}（{len(group)} 个）")
        for r in group:
            print(f'  {r["slug"]:<30} {r["display_name"]}')
    if hidden:
        print("\n隐藏的区域：" + ", ".join(sorted(hidden)))
        print("  恢复：wb-model.cmd show <ai|cn>   之后重启代理并刷新目录")
    print("\n切换：python wb-model.py use <slug>   （或直接在选择器里选，立即生效）")
    return 0


def cmd_current(args) -> int:
    home = codex_home()
    profile = home / f"{PROFILE}.config.toml"
    config = home / "config.toml"
    print(f"全局 provider           : {top_level_value(config, 'model_provider')}")
    print(f"全局模型目录            : {top_level_value(config, 'model_catalog_json')}")
    print(f"新建会话默认（config）  : {profile_model(config)}")
    print(f"新建会话默认（profile） : {profile_model(profile)}")
    try:
        d = proxy_json("/wb/last-model", timeout=10)
    except Exception as e:
        print(f"实际在用模型            : （代理没跑或没请求过：{e}）")
        return 0
    print(f"实际在用模型            : {d.get('model')}")
    print(f"  区域 / 上游模型       : {d.get('region')} / {d.get('upstream_model')}")
    print(f"  最近请求时间          : {d.get('ts')}")
    print(f"  配置已同步            : {d.get('config_synced')}（跟随上次使用：{d.get('follow_last_model')}）")
    return 0


def _catalog_slugs() -> set[str]:
    p = codex_home() / "workbuddy-catalog.json"
    try:
        doc = json.loads(read_text(p))
        return {str(x.get("slug")) for x in doc.get("models", []) if isinstance(x, dict)}
    except Exception:
        return set()


def refresh_catalog() -> bool:
    """Regenerate the selector catalog from the live proxy before validating."""
    gen = HERE / "gen-catalog.py"
    if not gen.is_file():
        return False
    out = codex_home() / "workbuddy-catalog.json"
    try:
        r = wbspawn.run_hidden([sys.executable, str(gen), "--port", str(port()),
                            "--out", str(out), "--quiet"],
                           capture_output=True, text=True, errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        print(f"（刷新模型目录失败：{(r.stderr or r.stdout).strip()[:200]}）")
        return False
    return True


def switch_global_defaults(home: Path, slug: str) -> tuple[list[str], Path | None]:
    """Point Codex's global model defaults at WorkBuddy atomically."""
    config = home / "config.toml"
    profile = home / f"{PROFILE}.config.toml"
    catalog = (home / "workbuddy-catalog.json").resolve().as_posix()
    config_updates = {
        "model_provider": "workbuddy",
        "model_catalog_json": catalog,
        "model": slug,
    }
    needs_config = any(top_level_value(config, key) != value
                       for key, value in config_updates.items())
    config_backup = backup_file(config) if needs_config else None
    config_changed = rewrite_top_level_values(config, config_updates)

    changed = ["config.toml"] if config_changed else []
    if profile.is_file() and top_level_value(profile, "model") != slug:
        backup_file(profile)
        if set_top_level_model(profile, slug):
            changed.append(profile.name)
    return changed, config_backup


def cmd_use(args) -> int:
    slug = args.slug.strip()
    if not slug:
        print("用法：python wb-model.py use cn/deepseek-v4.1-flash")
        return 2
    refresh_catalog()
    slugs = _catalog_slugs()
    if slugs and slug not in slugs:
        print(f"目录里没有 {slug}。可运行 `python wb-model.py list` 查看。")
        return 2
    home = codex_home()
    config = home / "config.toml"
    if not config.is_file():
        print(f"找不到 {config}；请先运行 setup.py install。")
        return 1
    if "[model_providers.workbuddy]" not in read_text(config):
        print("config.toml 里缺少 [model_providers.workbuddy]；请先运行 setup.py install。")
        return 1

    changed, config_backup = switch_global_defaults(home, slug)

    if not changed:
        print(f"全局 provider 与默认模型已就绪：workbuddy / {slug}")
    else:
        print(f"已切换全局 provider 为 workbuddy、模型目录为 workbuddy-catalog.json，"
              f"默认模型为 {slug}（{', '.join(changed)}）。")
        if config_backup:
            print(f"配置备份：{config_backup}")
    print("请完全退出并重新启动 Codex 桌面端；重启后新建聊天，模型选择器会加载 WorkBuddy 目录。")
    return 0


def cmd_visibility(args) -> int:
    """Hide or show a region without signing it out.

    Hiding only affects discovery: ``/v1/models`` and the generated catalog
    omit the region, so the picker stops offering models that no longer work.
    Credentials and ``/health`` are untouched.
    """
    region = args.region
    hidden = args.action == "hide"
    try:
        path = wbsettings.set_region_hidden(region, hidden)
    except (OSError, ValueError) as e:
        print(f"写入设置失败：{e}")
        return 1
    print(f"{'已隐藏' if hidden else '已恢复'} {wbdiscover.REGIONS[region].label}"
          f"（{path}）")
    refresh_catalog()
    if hidden:
        print("已从 /v1/models 与 workbuddy-catalog.json 中移除。")
        print("请完全退出并重新启动 Codex 桌面端，再新建聊天。")
    else:
        print("已重新加入模型目录。请完全退出并重新启动 Codex 桌面端。")
    return 0


def cmd_region(args) -> int:
    region = args.region
    if region not in wbdiscover.REGIONS:
        print("区域只能是 ai 或 cn")
        return 2
    env = "WORKBUDDY_CN_DISABLED" if region == "cn" else "WORKBUDDY_AI_DISABLED"
    disabled = args.action == "disable"
    if sys.platform == "win32":
        # setx with an empty value leaves an empty variable behind; use the
        # .NET API so "enable" truly removes the user-level override.
        if disabled:
            script = f"[Environment]::SetEnvironmentVariable('{env}', '1', 'User')"
        else:
            script = f"[Environment]::SetEnvironmentVariable('{env}', $null, 'User')"
        r = wbspawn.run_hidden(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                               "-Command", script],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(f"写入用户环境变量失败：{(r.stderr or r.stdout).strip()[:200]}")
            return 1
        print(f"{'已停用' if disabled else '已启用'} {wbdiscover.REGIONS[region].label}"
              f"（用户环境变量 {env} {'=1' if disabled else '已清除'}）。")
        print("环境变量对已运行的代理无效；请重启代理（或重新登录插件）后再试。")
    else:
        print(f"请在你的 shell 配置里设置 {env}={'1' if disabled else '(清除)'}，然后重启代理。")
    return 0


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="切换 WorkBuddy 模型 / 区域")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有模型")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("current", help="显示实际在用与默认模型")
    p.set_defaults(func=cmd_current)

    p = sub.add_parser("use", help="设置新建会话的默认模型")
    p.add_argument("slug")
    p.set_defaults(func=cmd_use)

    for action in ("enable", "disable"):
        p = sub.add_parser(action, help=f"{action} 一个区域")
        p.add_argument("region", choices=list(wbdiscover.REGIONS))
        p.set_defaults(func=cmd_region, action=action)

    for action in ("hide", "show"):
        p = sub.add_parser(action, help=f"{action} 一个区域（只影响模型选择器）")
        p.add_argument("region", choices=list(wbdiscover.REGIONS))
        p.set_defaults(func=cmd_visibility, action=action)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
