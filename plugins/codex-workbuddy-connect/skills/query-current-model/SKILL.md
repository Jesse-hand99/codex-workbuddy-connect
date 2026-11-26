---
name: query-current-model
description: >-
  Resolve and report the model Codex is actually running against (region, slug,
  upstream model, display name, provider, context window, max output, reasoning
  levels) by querying the local WorkBuddy proxy and reading the active Codex
  config and provider model catalog. Use when the user asks "what model are you",
  "current model", "which model am I using", "国际版还是国内版", or to verify the
  effective model after switching providers/profiles/regions. Do NOT use for
  generic model-selection advice, pricing, or API usage questions unrelated to
  this machine's live configuration.
metadata:
  short-description: Query the model Codex actually runs
---

# Query Current Model

The running assistant cannot introspect its own model identity, and **reading
config.toml is not enough**: switching the model inside a Codex session (desktop
model picker) is a *runtime override* that is never written back to config.toml.
The configured `model` is only the default for **new** sessions/threads.

So there are two different questions and two different sources:

| Question | Source |
|---|---|
| What will a **new** session use? | `config.toml` `model` key |
| What is **actually** being used right now? | the local proxy — it is the only component that sees the real `model` field on the wire |

## Authority for "right now": the proxy endpoint

```bash
curl -s http://127.0.0.1:8787/wb/last-model
# {"model": "cn/hy3", "upstream_model": "hy3", "region": "cn",
#  "ts": 1791873063, "count": 42,
#  "config_model": "cn/hy3", "config_synced": true,
#  "follow_last_model": true, "config_path": "...", "profile_path": "..."}
```

`model` is the selector slug (`ai/...` = WorkBuddy AI international,
`cn/...` = WorkBuddy CN). `region` is the account that actually served it and
`upstream_model` is the bare id sent upstream. Bare legacy ids (no prefix) are
international aliases.

`/health` carries `last_model`, `last_model_ts`, `requests_seen`, plus
`regions.{ai,cn}`, `pid`, `version` and `root`. Use `regions` to see whether one
side is signed out, and `pid`/`version`/`root` to tell which proxy process is
answering when several plugin versions are cached.

**The proxy also syncs config.toml**: every real chat/Responses request rewrites
the top-level `model` key (in both `config.toml` and `wb.config.toml`) to the
canonical selector slug. So `config_model` should normally equal `model`
(`config_synced: true`). If they disagree, trust `model` and send a message to let
the sync catch up. `WB_FOLLOW_LAST_MODEL=0` disables the sync entirely
(`follow_last_model: false`); the old `WB_SYNC_CONFIG` is ignored.

If the proxy is down or no request has been made yet, fall back to config and say
so explicitly.

**Never** answer "which model are you" from your own intuition — a session-level
switch changes what is used without any on-disk trace until the next message.

## When to use
- User asks "what model are you / current model / which model am I using".
- After switching model in the desktop picker or in config, to confirm the change.
- Verifying `reasoning_effort`, context window, or max output tokens.

## How to run
Use the bundled PowerShell helper (Windows). It now queries the proxy first and
reports `effective_model` + `effective_source`, keeping the config value as
`config default`:

```powershell
pwsh -NoProfile -File "$env:CODEX_HOME/skills/query-current-model/scripts/Get-CurrentModel.ps1"
# raw JSON: add -Json
```

The script:
1. Queries `http://127.0.0.1:8787/wb/last-model` → `effective_model` (authority).
2. Reads `config.toml` (`model`, `model_provider`, `model_reasoning_effort`,
   `model_catalog_json`) → the **default for new sessions**.
3. Loads the catalog JSON for display name, context window, max output tokens and
   reasoning levels of the effective slug.
4. Flags `config_default_mismatch` when the two disagree, and best-effort inspects
   the running `codex.exe` command line for a `-m/--model` override.

## Reporting back
Lead with the **effective** model: region, slug, and display name, plus the
upstream model id — e.g. `Hy3（国内）/ cn/hy3 → upstream hy3, region cn`. Say its
source is "proxy-observed". Then give the config default. If they differ, say
plainly that the session was switched and config is only the default. Include
context window, max output, and reasoning effort when relevant. If the slug is NOT
in the catalog, say so and fall back to the raw slug. Do not guess a model the
config does not name, and never infer the region from the display name alone.

## Limitations
- `/wb/last-model` reflects the **last accepted request**; before any traffic it is
  `null`. Failed or rate-limited calls do not overwrite it, so a stale value can
  persist after you switch until the next successful message — after a picker
  change, send a message before verifying.
- The endpoint belongs to the WorkBuddy proxy. With a different provider there is no
  such authority; report config + say it is only the default.
- If the selected region is signed out, the request fails with `region_unavailable`;
  the proxy never falls back to the other region, so do not report the other
  region's model as "in use".
- Config path / catalog can be overridden with `-ConfigPath` / `-CatalogPath`.

See `references/workbuddy-config.md` for the specifics of this machine's WorkBuddy
provider layout (proxy, catalog JSON location, known slugs).
