"""从本地代理生成 Codex 用的模型目录（workbuddy-catalog.json）。

Codex 桌面端的「模型切换」列表来自 app-server 的 `model/list`，而它读取 config.toml 里
`model_catalog_json` 指向的文件。不设这个文件，列表就回落到 Codex 内置的 OpenAI 目录，
看不到任何 WorkBuddy 模型。

目录同时包含国际版与国内版两套模型：slug 带 `ai/` / `cn/` 前缀。
显示名与 WorkBuddy 桌面端的选择器保持一致，不加区域后缀；只有两区同名时
才临时加 `（国际版）` / `（国内版）` 区分。

用法：
    python gen-catalog.py                       # 写 ~/.codex/workbuddy-catalog.json
    python gen-catalog.py --out D:/x/cat.json   # 自定义输出
    python gen-catalog.py --port 8787           # 自定义代理端口
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def console_encodings() -> None:
    """见 wbdiscover.console_encodings：让中文输出不依赖 PYTHONUTF8。"""
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

# Codex 0.16x 的 Model 结构里有、而 OpenAI 形状没有的字段。
# 缺 tool_mode / apply_patch_tool_type 时新版本会认为「没有可用工具」而发 0 个工具。
EXTRA_FIELDS: dict = {
    "tool_mode": "parameters",
    "apply_patch_tool_type": "freeform",
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "use_responses_lite": False,
    "supports_reasoning_effort_updates": False,
    "node_repl_disabled": True,
    "include_permissions_instructions": False,
    "include_environment_context": False,
}

# 非对话模型（图片/视频），不属于模型切换列表
SKIP_PREFIXES = ("gpt-image", "seedance", "sora", "dall-e")

# 别名模型的友好显示名（否则多个别名会同名，列表里看着像重复项）
DISPLAY_NAMES = {
    "default-model": "Auto (智能选路)",
    "deepseek-v4.1-flash-sg": "Deepseek-V4.1-Flash (SG)",
}

# WorkBuddy 桌面端把它做成「档位快捷项」而不是具体模型：快速 / 均衡 / 极致
# 分别映射到 fast-model / balanced-model / primary-model。Codex 选择器要的是
# 真实模型清单，这些快捷项进去只会让人以为可以单独选，所以直接剔除。
TIER_ALIASES = frozenset({
    "fast-model", "balanced-model", "primary-model", "deep-model",
})

REGION_LABELS = {"ai": "国际版", "cn": "国内版"}
REGION_ORDER = ("ai", "cn")


def fetch_models(port: int, timeout: int = 60) -> list[dict]:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上本地代理 {url}：{e}\n"
            "请先启动代理（start-wbproxy.cmd），并确认至少一个 WorkBuddy 桌面端已登录。"
        )
    # 代理同时返回 OpenAI 形状(data) 与 Codex 形状(models)；优先用后者
    models = doc.get("models")
    if isinstance(models, list) and models:
        return models
    # 回退：从 OpenAI 形状自行转换
    out = []
    for m in doc.get("data") or []:
        if not m.get("id"):
            continue
        out.append({
            "slug": m["id"],
            "display_name": m.get("name") or m["id"],
            "description": m.get("name") or m["id"],
            "context_window": m.get("context_window") or 200_000,
            "max_output_tokens": m.get("max_output_tokens") or 24_000,
            "supports_images": m.get("supports_images") is True,
            "region": m.get("region"),
            "upstream_model": m.get("upstream_model"),
        })
    return out


def _region_of(model: dict) -> str:
    region = model.get("region")
    if region in REGION_LABELS:
        return region
    slug = str(model.get("slug") or "")
    if slug.startswith("cn/"):
        return "cn"
    if slug.startswith("ai/"):
        return "ai"
    return "ai"


def _base_name(model: dict) -> str:
    slug = str(model.get("slug") or "")
    upstream = str(model.get("upstream_model") or "")
    bare = slug[3:] if slug.startswith(("ai/", "cn/")) else slug
    if bare in DISPLAY_NAMES:
        return DISPLAY_NAMES[bare]
    if upstream in DISPLAY_NAMES:
        return DISPLAY_NAMES[upstream]
    return model.get("display_name") or model.get("description") or bare


def build_catalog(models: list[dict]) -> list[dict]:
    """补字段、剔非对话模型、按区域分组、显示名去重、重排 priority。"""
    entries: list[dict] = []
    seen_slug: set[str] = set()

    def add(region: str, rows: list[dict]) -> None:
        for m in rows:
            slug = m.get("slug") or m.get("id")
            if not slug or slug in seen_slug:
                continue
            upstream = str(m.get("upstream_model") or (slug[3:] if slug.startswith(("ai/", "cn/")) else slug))
            if upstream.startswith(SKIP_PREFIXES) or upstream in TIER_ALIASES:
                continue
            seen_slug.add(slug)

            name = _base_name(m)

            levels = m.get("supported_reasoning_levels") or []
            efforts = [str(x.get("effort")) for x in levels if isinstance(x, dict) and x.get("effort")]
            if not efforts:
                efforts = ["none", "low", "medium", "high"]
            default = m.get("default_reasoning_level")
            if default not in efforts:
                default = "low" if "low" in efforts else efforts[0]

            e = {
                "slug": slug,
                "display_name": name,
                "description": name,
                "context_window": m.get("context_window") or 200_000,
                "max_output_tokens": m.get("max_output_tokens") or 24_000,
                "base_instructions": "default",
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,                      # 稍后统一重排
                "supports_reasoning_summaries": False,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "truncation_policy": {"mode": "bytes", "limit": 10_000},
                "experimental_supported_tools": [],
                "supports_parallel_tool_calls": False,
                # Codex uses this field to decide whether the composer accepts an
                # attachment. Preserve the upstream capability instead of turning
                # every WorkBuddy model into text-only metadata.
                "input_modalities": (
                    ["text", "image"]
                    if (
                        m.get("supports_images") is True
                        or "image" in (m.get("input_modalities") or [])
                    )
                    else ["text"]
                ),
                "default_reasoning_level": default,
                "supported_reasoning_levels": [
                    {"effort": x, "description": x} for x in efforts
                ],
            }
            e.update(EXTRA_FIELDS)
            entries.append(e)

    for region in REGION_ORDER:
        add(region, [m for m in models if _region_of(m) == region])
    # 未知区域（理论上不会出现）放最后，避免静默丢失模型
    add("", [m for m in models if _region_of(m) not in REGION_LABELS])

    # 显示名去重。多数情况下两区模型清单不同名，名字保持与桌面端选择器一致；
    # 只有真的重名时才补后缀：跨区用（国际版）/（国内版），同区再用 slug 区分。
    names: dict[str, int] = {}
    for e in entries:
        names[e["display_name"]] = names.get(e["display_name"], 0) + 1
    seen: dict[str, int] = {}
    for e in entries:
        name = e["display_name"]
        if names[name] > 1:
            region = _region_of({"slug": e["slug"]})
            suffix = REGION_LABELS.get(region) or e["slug"]
            name = f"{name}（{suffix}）"
        if seen.get(name):
            name = f'{name} [{e["slug"]}]'
        seen[name] = seen.get(name, 0) + 1
        e["display_name"] = name
        e["description"] = name

    for i, e in enumerate(entries):
        e["priority"] = i
    return entries


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="生成 Codex 用的 WorkBuddy 模型目录")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WORKBUDDY_AI_PROXY_PORT", 8787)))
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".codex", "workbuddy-catalog.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    raw = fetch_models(args.port)
    entries = build_catalog(raw)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"models": entries}, fh, ensure_ascii=False, indent=1)

    if not args.quiet:
        counts = {r: sum(1 for e in entries if e["slug"].startswith(r + "/")) for r in REGION_ORDER}
        print(f"代理模型 {len(raw)} 个 → 写入 {len(entries)} 个"
              f"（国际 {counts['ai']} / 国内 {counts['cn']}；"
              f"已剔除 {len(raw) - len(entries)} 个非对话/重复项）")
        print(f"输出：{args.out}")
        for e in entries:
            print(f'  {e["slug"]:<30} {e["display_name"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
