# WorkBuddy provider layout

The `workbuddy` model provider is configured in `<CODEX_HOME>/config.toml`
(`CODEX_HOME` defaults to `~/.codex`):

```toml
model_provider = 'workbuddy'
model = "cn/deepseek-v4.1-flash"
model_reasoning_effort = "low"
model_catalog_json = "<CODEX_HOME>/workbuddy-catalog.json"
```

- It is one local proxy (`http://127.0.0.1:8787/v1`) that serves **both**
  WorkBuddy desktop apps:
  - `ai/...` → WorkBuddy AI (international, `www.workbuddy.ai`)
  - `cn/...` → WorkBuddy CN (`copilot.tencent.com`)
  - a bare id (no prefix) is a legacy international alias.
- The `model_catalog_json` file lists every available slug under a top-level
  `models` array, each with `slug`, `display_name`, `context_window`,
  `max_output_tokens`, `supported_reasoning_levels`, and (from the proxy)
  `region` / `upstream_model`.
- The `model` field in config is the **slug**; the helper resolves the friendly
  `display_name` from the catalog. Display names mirror the WorkBuddy desktop app
  (no region suffix); `（国际）` / `（国内）` only appears when both regions expose
  the same name.

## Authority for "right now"

`GET http://127.0.0.1:8787/wb/last-model` returns:

```json
{"model":"cn/hy3","upstream_model":"hy3","region":"cn","ts":1792475670,
 "count":42,"config_model":"cn/hy3","config_synced":true,
 "follow_last_model":true,"config_path":"...","profile_path":"..."}
```

`GET /health` mirrors the last visible region at the top level and adds
`regions.{ai,cn}`, `pid`, `version`, `root`. If a region is signed out its
`regions.<id>.available` is `false` with an `error` string.

## Hidden regions

`regions.<id>.hidden` reflects `<CODEX_HOME>/workbuddy-settings.json`:

```json
{"hidden_regions": ["ai"]}
```

A hidden region keeps its credentials and still appears in `/health` and
`/wb/account-status`, but is omitted from `/v1/models` and from
`workbuddy-catalog.json`. Sending one of its slugs to `/v1/responses` returns
HTTP 400 `region_unavailable` — there is no cross-region fallback.

Toggle it with `bin/wb-model.py hide|show <ai|cn>`, or set
`WORKBUDDY_HIDDEN_REGIONS=ai` for the current process. Hiding is **not** the
same as `WORKBUDDY_AI_DISABLED=1`, which skips loading credentials entirely.

## Model roster — read it live, do not hard-code

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8787/v1/models
```

Snapshot on 2026-11-24 (CN account, 16 CLI models):

`cn/hy4std`, `cn/hy4flash-free`, `cn/hy4flash`, `cn/deepseek-v4.1-flash`,
`cn/glm-5.3`, `cn/glm-5.3-flash`, `cn/glm-5.2`, `cn/space-bunny`,
`cn/minimax-m3.1-flash`, `cn/minimax-m2.7`, `cn/kimi-k3.1-pro`, `cn/kimi-k3-1`,
`cn/kimi-k2.8-preview`, `cn/deepseek-v4-pro`, `cn/step-5-preview`,
`cn/mimo-v2.6-pro`. Hy3 / Hy3 (X) are gone from the upstream CLI roster.

`/v3/config` also lists internal ids that are **not** in `agents.cli.models`
(e.g. `glm-5.1`, `glm-5v-turbo`, `minimax-m3`, `kimi-k2.6`, `kimi-k2.7`); the
proxy filters those out, so they cannot be selected. It also carries the
desktop app's tier shortcuts (`fast-model` / `balanced-model` /
`primary-model` / `deep-model`, i.e. 快速 / 均衡 / 极致) and image/video models,
which are filtered out too — the picker only lists real chat models with the
same names the WorkBuddy app shows.

International (`ai/`) models are currently unavailable upstream and hidden in
this installation (`hidden_regions: ["ai"]`).

If `model` names a slug absent from the catalog, the helper still reports it but
flags `catalog_found = false`.
