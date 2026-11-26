"""路径与运行时自动发现。

让这个包能在**任意机器**上跑起来：WorkBuddy 桌面端装在哪、凭据文件叫什么、
Python 和 Codex 在不在 PATH 里，一律自动找，不写死。

两个 WorkBuddy 产品共用一套客户端框架，安装路径、可执行文件名与凭据文件名
各不相同，因此这里按「区域」建模：

  - ``ai``：WorkBuddy AI（国际版）  WorkBuddyAI.exe / workbuddy-desktop-ai.info
  - ``cn``：WorkBuddy（国内版）      WorkBuddy.exe   / workbuddy-desktop.info

所有发现函数都遵循同一优先级：**环境变量覆盖 > 自动探测**。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import wbspawn

# ── 环境变量名（同时被 wbproxy / setup / 启动脚本读取）──
ENV_APP = "WORKBUDDY_AI_APP"                 # 国际版（历史名称，保持兼容）
ENV_AUTH = "WORKBUDDY_AI_AUTH_FILE"
ENV_CN_APP = "WORKBUDDY_CN_APP"
ENV_CN_AUTH = "WORKBUDDY_CN_AUTH_FILE"
ENV_PYTHON = "WORKBUDDY_AI_PYTHON"
ENV_CODEX = "CODEX_WB_CODEX"

REGION_AI = "ai"
REGION_CN = "cn"


@dataclass(frozen=True)
class RegionSpec:
    """One WorkBuddy product's on-disk identity."""

    id: str
    label: str                    # 国际版 / 国内版
    app_env: str
    auth_env: str
    app_exe: str
    app_dir_names: tuple[str, ...]
    display_pattern: str          # 卸载项 DisplayName 的正则
    auth_name: str
    default_domain: str


REGIONS: dict[str, RegionSpec] = {
    REGION_AI: RegionSpec(
        id=REGION_AI,
        label="国际版",
        app_env=ENV_APP,
        auth_env=ENV_AUTH,
        app_exe="WorkBuddyAI.exe",
        app_dir_names=("WorkBuddyAI", "WorkBuddy AI", "CodeBuddyAI"),
        display_pattern=r"^WorkBuddy AI(?:\s|$)",
        auth_name="workbuddy-desktop-ai.info",
        default_domain="www.workbuddy.ai",
    ),
    REGION_CN: RegionSpec(
        id=REGION_CN,
        label="国内版",
        app_env=ENV_CN_APP,
        auth_env=ENV_CN_AUTH,
        app_exe="WorkBuddy.exe",
        app_dir_names=("WorkBuddy", "CodeBuddy"),
        display_pattern=r"^WorkBuddy(?!\s+AI)(?:\s|$)",
        auth_name="workbuddy-desktop.info",
        default_domain="www.codebuddy.cn",
    ),
}

REGION_ORDER = (REGION_AI, REGION_CN)

# 桌面端路径不会几秒变一次；缓存自动发现结果，避免 /health 的 5 秒轮询反复
# 启动 PowerShell 查卸载注册表。环境变量覆盖仍每次即时生效。
DISCOVERY_TTL_SEC = 300.0
_DISCOVERY_CACHE: dict[tuple[str, object], tuple[float, object]] = {}
_DISCOVERY_LOCK = threading.Lock()


def _env_fingerprint() -> tuple:
    return tuple((name, os.environ.get(name)) for name in (
        ENV_APP, ENV_AUTH, ENV_CN_APP, ENV_CN_AUTH, ENV_PYTHON, ENV_CODEX,
        "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
        "ProgramW6432",
    ))


def _cached_discovery(key: str, region: str, producer):
    cache_key = (key, region, _env_fingerprint())
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        hit = _DISCOVERY_CACHE.get(cache_key)
        if hit and now - float(hit[0]) < DISCOVERY_TTL_SEC:
            return hit[1]
    value = producer(region)
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE[cache_key] = (time.monotonic(), value)
    return value


def clear_discovery_cache() -> None:
    """Drop cached path lookups (tests and explicit refresh points)."""
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE.clear()



# 兼容旧调用方（只描述国际版时代的常量）
APP_EXE_NAMES = tuple(spec.app_exe for spec in REGIONS.values())
APP_DIR_NAMES = tuple(d for spec in REGIONS.values() for d in spec.app_dir_names)
AUTH_NAMES = tuple(spec.auth_name for spec in REGIONS.values())


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def console_encodings() -> None:
    """让 stdout/stderr 的编码匹配控制台代码页，避免中文乱码。

    有些环境（例如 Hermes）会设 PYTHONUTF8=1，Python 便输出 UTF-8，而 Windows 控制台
    默认是 cp936，中文就变成乱码。这里统一按**控制台输出代码页**重配，于是：

      - 终端里显示正常；
      - 重定向到文件时也和 .cmd 自身的中文（cmd 用 OEM 代码页输出）保持一致，
        不会出现半截 cp936、半截 UTF-8 的混合文件。

    不依赖 chcp —— 改 chcp 会打断正在执行的 .cmd 里 cp936 编码的中文。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    enc = f"cp{cp}" if cp else "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass


# ── WorkBuddy 桌面端可执行文件 ──────────────────────────────
def _uninstall_entries() -> list[tuple[str, str]]:
    """返回 [(DisplayName, UninstallString)]，最多 16 条。"""
    if sys.platform != "win32":
        return []
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p=@('HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
        "Get-ItemProperty $p | "
        "Where-Object { $_.DisplayName -match 'WorkBuddy|CodeBuddy' } | "
        "Select-Object -First 16 DisplayName,UninstallString | "
        "ForEach-Object { $_.DisplayName + \"`t\" + $_.UninstallString }"
    )
    try:
        r = wbspawn.run_hidden(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                               "-Command", ps],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[tuple[str, str]] = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if "\t" not in line:
            continue
        name, uninstall = line.split("\t", 1)
        out.append((name.strip(), uninstall.strip()))
    return out


def _exe_from_uninstall(uninstall: str, spec: RegionSpec) -> str | None:
    line = uninstall.strip().strip('"')
    if not line:
        return None
    # 形如  "D:\...\WorkBuddyAI\Uninstall WorkBuddyAI.exe" /currentuser
    path = line.split('"')[0] if line.startswith('"') else line
    path = path.split(" /")[0].strip().strip('"')
    d = Path(path).parent
    for exe in (spec.app_exe, "WorkBuddy.exe", "WorkBuddyAI.exe", "CodeBuddy.exe"):
        cand = d / exe
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def _app_from_registry(spec: RegionSpec) -> str | None:
    """NSIS 安装会写卸载项，按 DisplayName 精确区分两个产品。"""
    import re
    pattern = re.compile(spec.display_pattern, re.I)
    for name, uninstall in _uninstall_entries():
        if not pattern.search(name):
            continue
        got = _exe_from_uninstall(uninstall, spec)
        if got:
            return got
    return None


def _app_from_process(spec: RegionSpec) -> str | None:
    """桌面端正在跑的话，直接问它。"""
    if sys.platform != "win32":
        return None
    name = spec.app_exe
    try:
        r = wbspawn.run_hidden(
            ["wmic", "process", "where", f"name='{name}'",
             "get", "ExecutablePath", "/format:csv"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts and parts[-1].lower().endswith(name.lower()):
            try:
                if Path(parts[-1]).is_file():
                    return parts[-1]
            except OSError:
                continue
    return None


def _app_from_common_paths(spec: RegionSpec) -> str | None:
    roots = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    seen: list[Path] = []
    for root in roots:
        if not root:
            continue
        for d in spec.app_dir_names:
            for sub in ("Programs", ""):
                base = Path(root) / sub / d if sub else Path(root) / d
                if base not in seen:
                    seen.append(base)
    # 非系统盘上的同名目录（很多人装到 D:）
    for drive in "CDEFGH":
        for d in spec.app_dir_names:
            seen.append(Path(f"{drive}:/Programs/{d}"))
            seen.append(Path(f"{drive}:/Program Files/{d}"))
    for base in seen:
        cand = base / spec.app_exe
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def find_app(region: str = REGION_AI) -> str | None:
    """返回指定区域桌面端主程序路径；找不到返回 None。

    显式设置了该区域的环境变量就原样采信（哪怕路径不存在）——这样写错路径时
    体检会如实报「找不到」，而不是悄悄回退到自动发现、让人以为配置生效了。
    自动发现结果按 TTL 缓存，避免高频 /health 反复拉起 PowerShell。
    """
    spec = REGIONS[region]
    override = _env(spec.app_env)
    if override:
        return override

    def _discover(_region: str):
        for probe in (_app_from_registry, _app_from_process, _app_from_common_paths):
            got = probe(REGIONS[_region])
            if got:
                return got
        return None

    return _cached_discovery("app", region, _discover)


def app_version(region: str = REGION_AI) -> str | None:
    """读取桌面端安装目录里的 version 文件。"""
    app = find_app(region)
    if not app:
        return None
    try:
        v = Path(app).parent / "version"
        text = v.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


# ── 凭据文件 ────────────────────────────────────────────────
def _auth_dirs() -> list[Path]:
    la = os.environ.get("LOCALAPPDATA")
    out: list[Path] = []
    if la:
        out.append(Path(la) / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    # 部分版本把数据放在用户目录下
    out.append(Path.home() / ".codebuddy" / "auth")
    return out


def find_auth_files() -> list[str]:
    """列出所有可用的凭据文件（国际版在前）。"""
    found: list[str] = []
    for spec in (REGIONS[r] for r in REGION_ORDER):
        for d in _auth_dirs():
            p = d / spec.auth_name
            try:
                if p.is_file() and p.stat().st_size > 0:
                    found.append(str(p))
                    break
            except OSError:
                continue
    return found


def find_auth_file(region: str = REGION_AI) -> str | None:
    spec = REGIONS[region]
    override = _env(spec.auth_env)
    if override:
        return override

    def _discover(_region: str):
        for d in _auth_dirs():
            p = d / REGIONS[_region].auth_name
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return str(p)
            except OSError:
                continue
        return None

    return _cached_discovery("auth", region, _discover)


def discover_region(region: str) -> dict:
    """One region's paths and presence, for diagnostics and startup."""
    spec = REGIONS[region]
    app = find_app(region)
    auth = find_auth_file(region)
    return {
        "id": region,
        "label": spec.label,
        "app": app,
        "app_exists": bool(app and Path(app).is_file()),
        "auth": auth,
        "auth_exists": bool(auth and Path(auth).is_file()),
        "version": app_version(region),
        "app_env": spec.app_env,
        "auth_env": spec.auth_env,
    }


# ── Python 解释器（供 .cmd 启动脚本用）────────────────────
def _python_candidates() -> list[str]:
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    local_venv = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
    out = [
        str(local_venv),
        os.path.join(la, "hermes", "hermes-agent", "venv", "Scripts", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python313", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python311", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python310", "python.exe"),
        os.path.join(pf, "Python313", "python.exe"),
        os.path.join(pf, "Python312", "python.exe"),
        os.path.join(pf, "Python311", "python.exe"),
        os.path.join(pf, "Python310", "python.exe"),
    ]
    return out


def find_python() -> str | None:
    """返回一个能用的 python 解释器（优先带 cryptography 的）。"""
    override = _env(ENV_PYTHON)
    if override and Path(override).is_file():
        return override
    cands = [c for c in _python_candidates() if Path(c).is_file()]
    # PATH 上的 python / py
    for name in ("python.exe", "python", "py"):
        p = shutil.which(name)
        if p and Path(p).is_file():
            cands.append(p)
    seen: set[str] = set()
    for cand in cands:
        if cand in seen:
            continue
        seen.add(cand)
        if has_crypto(cand)[0]:
            return cand
    return cands[0] if cands else None


def _has_crypto(python: str) -> bool:
    try:
        r = wbspawn.run_hidden([python, "-c", "import cryptography; print(cryptography.__version__)"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def has_crypto(python: str) -> tuple[bool, str]:
    """返回 (是否有 cryptography, 版本或错误)。"""
    try:
        r = wbspawn.run_hidden([python, "-c",
                            "import sys, cryptography; "
                            "print(f'{sys.version_info.major}.{sys.version_info.minor}|{cryptography.__version__}')"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return True, r.stdout.decode("utf-8", "replace").strip()
        return False, r.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:120]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:120]


# ── Codex 可执行文件 ────────────────────────────────────────
def find_codex() -> str | None:
    """返回 codex 的 .cmd/.exe 路径。

    本机 PATH 里 Hermes 自带的那份 0.162.1 排在前面，而它 exec 会失败；npm 全局那份
    0.153.2 可用。所以默认优先 npm 全局安装（那是用户自己装的），再退回 PATH。
    """
    override = _env(ENV_CODEX)
    if override:
        p = shutil.which(override) or (override if Path(override).is_file() else None)
        if p:
            return p
    appdata = os.environ.get("APPDATA", "")
    for rel in ("npm/codex.cmd", "npm/codex.exe", "npm/codex"):
        p = Path(appdata) / rel
        if p.is_file():
            return str(p)
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ── 一次性体检 ──────────────────────────────────────────────
def diagnose(port: int = 8787) -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否通过, 说明)]，供 setup.py doctor 打印。"""
    out: list[tuple[str, bool, str]] = []

    # Python
    py = find_python()
    if py:
        ok, info = has_crypto(py)
        ver = info.split("|")[0] if "|" in info else "?"
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
            ver_ok = (major, minor) >= (3, 10)
        except ValueError:
            ver_ok = False
        out.append(("Python >=3.10", ver_ok, py if ver_ok else f"{py}（版本 {ver} 偏低）"))
        out.append(("cryptography", ok, info if ok else f"缺失 —— {info}"))
    else:
        out.append(("Python >=3.10", False, "找不到 python，请安装 Python 3.10+ 并勾选 Add to PATH"))
        out.append(("cryptography", False, "跳过（没有 python）"))

    # WorkBuddy 桌面端与凭据：每个区域各一行
    for region in REGION_ORDER:
        spec = REGIONS[region]
        found = discover_region(region)
        out.append((
            f"WorkBuddy {spec.label}",
            found["app_exists"],
            found["app"] or f"找不到；安装 {spec.label} 桌面端，或设置 {spec.app_env}",
        ))
        out.append((
            f"{spec.label}登录凭据",
            found["auth_exists"],
            found["auth"] or f"找不到；先启动 {spec.label} 桌面端并登录，或设置 {spec.auth_env}",
        ))

    # Codex
    cx = find_codex()
    out.append(("Codex CLI", bool(cx), cx or "找不到 codex，请安装：npm i -g @openai/codex"))

    # 端口
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            out.append((f"端口 {port}", True, "已被占用（可能是本代理已在跑）"))
    except OSError:
        out.append((f"端口 {port}", True, "空闲"))

    return out


def main() -> int:
    import argparse
    console_encodings()
    ap = argparse.ArgumentParser(description="路径与运行时自动发现")
    ap.add_argument("what", nargs="?", default="all",
                    choices=["app", "auth", "python", "codex", "all", "doctor", "regions"])
    ap.add_argument("--region", choices=list(REGIONS), default=REGION_AI)
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()

    if a.what == "app":
        print(find_app(a.region) or "")
    elif a.what == "auth":
        print(find_auth_file(a.region) or "")
    elif a.what == "python":
        print(find_python() or "")
    elif a.what == "codex":
        print(find_codex() or "")
    elif a.what == "doctor":
        width = max(len(n) for n, _, _ in diagnose(a.port))
        for name, ok, detail in diagnose(a.port):
            print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")
    elif a.what == "regions":
        import json
        print(json.dumps({r: discover_region(r) for r in REGION_ORDER},
                         ensure_ascii=False, indent=1))
    else:
        print(f"python : {find_python()}")
        print(f"codex  : {find_codex()}")
        for region in REGION_ORDER:
            d = discover_region(region)
            print(f"{region:<7}: app={d['app']} auth={d['auth']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
