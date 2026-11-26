from __future__ import annotations

import json
import sys
import unittest
import uuid
import urllib.error
from unittest import mock
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = PLUGIN_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))

import setup  # noqa: E402
import wbproxy  # noqa: E402
import wbresponses  # noqa: E402
import watch_plugin  # noqa: E402


class ChatHeaderTests(unittest.TestCase):
    def test_chat_headers_include_fresh_conversation_trio(self) -> None:
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.region = "ai"
        creds.spec = wbproxy.REGIONS["ai"]
        creds.access_token = "local-test-token"
        creds.app_version = "1.2.3"
        creds.uid = "test-user"
        creds.enterprise_id = ""
        creds.domain = "www.workbuddy.ai"

        first = creds.chat_headers()
        second = creds.chat_headers()

        for name in (
            "X-Conversation-ID",
            "X-Conversation-Request-ID",
            "X-Conversation-Message-ID",
        ):
            self.assertIn(name, first)
            uuid.UUID(first[name])
            self.assertNotEqual(first[name], second[name])
        self.assertNotIn("X-Refresh-Token", first)
        self.assertEqual(first["Authorization"], "Bearer local-test-token")

        account = creds.account_headers()
        self.assertNotIn("X-Conversation-ID", account)
        self.assertEqual(account["Authorization"], "Bearer local-test-token")

    def test_cn_chat_headers_use_desktop_ua_and_cn_origin(self) -> None:
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.region = "cn"
        creds.spec = wbproxy.REGIONS["cn"]
        creds.access_token = "cn-token"
        creds.app_version = "37.10.3-42"
        creds.uid = "cn-user"
        creds.enterprise_id = ""
        creds.domain = "www.codebuddy.cn"

        headers = creds.chat_headers()
        self.assertEqual(headers["Origin"], "https://www.codebuddy.cn")
        self.assertEqual(headers["User-Agent"], "WorkBuddy/37.10.3-42 WorkBuddy/37.10.3-42")
        self.assertEqual(headers["X-Domain"], "www.codebuddy.cn")
        self.assertIn("X-Conversation-ID", headers)

    def test_cn_catalog_uses_cli_ua(self) -> None:
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.region = "cn"
        creds.spec = wbproxy.REGIONS["cn"]
        creds.access_token = "cn-token"
        creds.app_version = "37.10.3-42"
        creds.uid = "cn-user"
        creds.enterprise_id = ""
        creds.domain = "www.codebuddy.cn"
        headers = creds.catalog_headers()
        self.assertEqual(headers["User-Agent"], wbproxy.CN_CLI_UA)
        self.assertEqual(headers["Authorization"], "Bearer cn-token")

    def test_upstream_error_summary_does_not_echo_extra_secret_fields(self) -> None:
        detail = json.dumps(
            {
                "code": 11128,
                "msg": "request illegal",
                "displayMsg": {"zh": "请求被拦截"},
                "accessToken": "must-not-leak",
                "refreshToken": "must-not-leak",
            },
            ensure_ascii=False,
        )
        summary = wbproxy.upstream_error_text(detail)
        self.assertIn("11128", summary)
        self.assertNotIn("must-not-leak", summary)


class ResponsesConversionTests(unittest.TestCase):
    def test_tools_and_tool_choice_are_flattened(self) -> None:
        payload = {
            "model": "deepseek-v4.1-flash",
            "input": [
                {"type": "message", "role": "developer", "content": "be concise"},
                {"type": "message", "role": "user", "content": "run it"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell",
                    "arguments": "{\"cmd\":\"echo WB_TOOL_OK\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "WB_TOOL_OK",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "run a shell command",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "function", "name": "shell"},
            "parallel_tool_calls": False,
        }
        chat = wbresponses.request_to_chat(payload)
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertIn("be concise", chat["messages"][0]["content"])
        self.assertEqual(chat["tools"][0]["function"]["name"], "shell")
        self.assertEqual(chat["tool_choice"], "shell")
        self.assertFalse(chat["parallel_tool_calls"])
        self.assertIn({"role": "tool", "tool_call_id": "call_1", "content": "WB_TOOL_OK"}, chat["messages"])
        self.assertTrue(any(m.get("tool_calls") for m in chat["messages"]))

    def test_tool_choice_none_drops_tools(self) -> None:
        chat = wbresponses.request_to_chat(
            {
                "input": "hello",
                "tools": [{"type": "function", "name": "shell"}],
                "tool_choice": "none",
            }
        )
        self.assertNotIn("tools", chat)
        self.assertNotIn("tool_choice", chat)


class RateLimitRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.region = "ai"
        creds.spec = wbproxy.REGIONS["ai"]
        creds.access_token = "local-test-token"
        creds.app_version = "1.2.3"
        creds.uid = "test-user"
        creds.enterprise_id = ""
        creds.domain = "www.workbuddy.ai"
        creds.ensure_fresh = mock.Mock()
        self.creds = creds
        self.payload = {"model": "hy3", "messages": [{"role": "user", "content": "hello"}]}

    @staticmethod
    def _http_error(code: int, detail: dict) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://www.workbuddy.ai/v2/chat/completions",
            code,
            "error",
            {},
            mock.Mock(read=lambda: json.dumps(detail).encode("utf-8")),
        )

    def test_14003_retries_four_times_then_succeeds(self) -> None:
        busy = self._http_error(429, {"code": 14003, "msg": "too many requests"})
        sentinel = object()
        with mock.patch.dict("os.environ", {"WB_14003_RETRIES": "4"}), \
             mock.patch.object(wbproxy.urllib.request, "urlopen", side_effect=[busy] * 4 + [sentinel]) as opened, \
             mock.patch.object(wbproxy.time, "sleep") as sleep, \
             mock.patch.object(wbproxy.random, "uniform", return_value=1.0):
            response, returned = wbproxy.open_upstream(self.creds, self.payload)

        self.assertIs(response, sentinel)
        self.assertEqual(returned, self.payload)
        self.assertEqual(opened.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 5.0, 10.0, 20.0])

    def test_14003_retry_budget_is_bounded(self) -> None:
        busy = self._http_error(429, {"code": 14003, "msg": "too many requests"})
        with mock.patch.dict("os.environ", {"WB_14003_RETRIES": "4"}), \
             mock.patch.object(wbproxy.urllib.request, "urlopen", side_effect=[busy] * 5) as opened, \
             mock.patch.object(wbproxy.time, "sleep") as sleep, \
             mock.patch.object(wbproxy.random, "uniform", return_value=1.0), \
             self.assertRaises(wbproxy.UpstreamError):
            wbproxy.open_upstream(self.creds, self.payload)

        self.assertEqual(opened.call_count, 5)
        self.assertEqual(sleep.call_count, 4)

    def test_6004_returns_immediately_without_sleeping(self) -> None:
        quota = self._http_error(429, {"code": 6004, "msg": "usage exceeds frequency limit"})
        with mock.patch.object(wbproxy.urllib.request, "urlopen", side_effect=quota) as opened, \
             mock.patch.object(wbproxy.time, "sleep") as sleep, \
             self.assertRaises(wbproxy.UpstreamError) as raised:
            wbproxy.open_upstream(self.creds, self.payload)

        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(raised.exception.status, 429)

    def test_invalid_retry_override_uses_default_schedule(self) -> None:
        with mock.patch.dict("os.environ", {"WB_14003_RETRIES": "999"}):
            self.assertEqual(wbproxy._retry_delays(), (2.0, 5.0, 10.0, 20.0))
        with mock.patch.dict("os.environ", {"WB_14003_RETRIES": "0"}):
            self.assertEqual(wbproxy._retry_delays(), ())

    def test_6004_error_response_is_non_retryable_and_actionable(self) -> None:
        handler = wbproxy.Handler.__new__(wbproxy.Handler)
        handler._json = mock.Mock()
        detail = json.dumps(
            {
                "code": 6004,
                "msg": "usage exceeds frequency limit",
                "displayTips": {"zh": "usage resets at 21:27"},
                "accessToken": "must-not-leak",
            }
        )
        handler._upstream_error(wbproxy.UpstreamError(429, detail))
        payload, status = handler._json.call_args.args
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], 6004)
        self.assertIn("Switch to another model", payload["error"]["message"])
        self.assertNotIn("must-not-leak", payload["error"]["message"])

    def test_14003_error_response_keeps_429_status(self) -> None:
        handler = wbproxy.Handler.__new__(wbproxy.Handler)
        handler._json = mock.Mock()
        detail = json.dumps({"code": 14003, "msg": "too many requests"})
        handler._upstream_error(wbproxy.UpstreamError(429, detail))
        payload, status = handler._json.call_args.args
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], 14003)
        self.assertIn("Switch to another model", payload["error"]["message"])


class SlugRoutingTests(unittest.TestCase):
    def test_prefix_selects_region_and_strips_it(self) -> None:
        self.assertEqual(wbproxy.resolve_slug("cn/hy3"), ("cn", "hy3"))
        self.assertEqual(wbproxy.resolve_slug("ai/deepseek-v4.1-flash-sg"), ("ai", "deepseek-v4.1-flash-sg"))

    def test_bare_ids_remain_international_aliases(self) -> None:
        self.assertEqual(wbproxy.resolve_slug("hy3"), ("ai", "hy3"))
        self.assertEqual(wbproxy.resolve_slug("deepseek-v4.1-flash"), ("ai", "deepseek-v4.1-flash"))

    def test_unknown_prefix_is_passed_to_international(self) -> None:
        self.assertEqual(wbproxy.resolve_slug("vendor/model"), ("ai", "vendor/model"))

    def test_missing_region_never_falls_back(self) -> None:
        rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
        rt.region = "cn"
        rt.spec = wbproxy.REGIONS["cn"]
        rt.creds = None
        rt.error = "no credential"
        with self.assertRaises(wbproxy.RegionUnavailable) as raised:
            rt.require()
        self.assertEqual(raised.exception.region, "cn")


class CatalogTests(unittest.TestCase):
    def test_cn_cli_roster_is_intersected_and_names_disambiguated(self) -> None:
        rows = wbproxy.normalize_catalog({
            "models": [
                {"id": "hy3", "name": "Hy3", "maxInputTokens": 192000, "maxOutputTokens": 64000,
                 "supportsImages": True, "reasoning": {"supportedEfforts": ["low", "high"]}},
                {"id": "hy3-x", "name": "Hy3", "maxInputTokens": 192000, "maxOutputTokens": 64000},
                {"id": "internal-only", "name": "Internal", "maxInputTokens": 1000, "maxOutputTokens": 100},
            ],
            "agents": [{"name": "cli", "models": ["hy3", "hy3-x"]}],
        })
        self.assertEqual([r["id"] for r in rows], ["hy3", "hy3-x"])
        self.assertEqual(rows[0]["name"], "Hy3")
        self.assertEqual(rows[1]["name"], "Hy3 (X)")
        self.assertEqual(rows[0]["reasoning_efforts"], ["low", "high"])
        self.assertTrue(rows[0]["supports_images"])

    def test_declared_efforts_win_and_none_requires_disable(self) -> None:
        levels, default = wbproxy.reasoning_levels(
            {"reasoning_efforts": ["low", "high", "max"], "default_effort": "max",
             "can_disable_thinking": False})
        self.assertEqual(levels, ["low", "high", "max"])
        self.assertEqual(default, "max")

        levels, _ = wbproxy.reasoning_levels(
            {"reasoning_efforts": ["low", "high"], "default_effort": "high",
             "can_disable_thinking": True})
        self.assertEqual(levels, ["none", "low", "high"])

    def test_models_without_declared_efforts_keep_fallback(self) -> None:
        levels, default = wbproxy.reasoning_levels({})
        self.assertEqual(levels, ["none", "low", "medium", "high"])
        self.assertEqual(default, "low")

    def test_tier_shortcuts_and_media_models_are_not_offered(self) -> None:
        """快速/均衡/极致 是档位别名；出图出视频的也不是对话模型。"""
        rows = wbproxy.normalize_catalog({
            "models": [
                {"id": "hy4flash", "name": "Hy4 flash", "maxInputTokens": 960000},
                {"id": "fast-model", "name": "快速"},
                {"id": "balanced-model", "name": "均衡"},
                {"id": "primary-model", "name": "极致"},
                {"id": "deep-model", "name": "深度思考"},
                {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha",
                 "tags": ["text-to-image"]},
                {"id": "kling-video-v3", "name": "Kling-Video-V3",
                 "tags": ["text-to-video"], "videoGeneration": {"maxSeconds": 15}},
            ],
        })
        self.assertEqual([r["id"] for r in rows], ["hy4flash"])

    def test_picker_names_match_the_desktop_app(self) -> None:
        """名字跟桌面端一致：不加区域后缀，只有真的重名才补。"""
        class Creds:
            def __init__(self, region):
                self.region = region

            def catalog(self):
                return [{
                    "id": "hy4flash", "name": "Hy4 flash", "context_window": 960000,
                    "max_output_tokens": 64000, "supports_images": True,
                    "reasoning_efforts": ["high"], "default_effort": "high",
                    "can_disable_thinking": False, "credits": "",
                }]

        import os
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old_settings = os.environ.get("WB_SETTINGS")
        os.environ["WB_SETTINGS"] = str(Path(self._tmp.name) / "settings.json")
        self.addCleanup(lambda: (os.environ.pop("WB_SETTINGS", None) if old_settings is None
                                 else os.environ.__setitem__("WB_SETTINGS", old_settings)))

        saved = dict(wbproxy.REGISTRY)
        wbproxy.REGISTRY.clear()
        try:
            self.addCleanup(lambda: (wbproxy.REGISTRY.clear(),
                                     wbproxy.REGISTRY.update(saved)))
            for region in ("ai", "cn"):
                rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
                rt.region = region
                rt.spec = wbproxy.REGIONS[region]
                rt.creds = Creds(region)
                rt.error = None
                wbproxy.REGISTRY[region] = rt

            # 只有国内版时保持原样
            wbproxy.REGISTRY.pop("ai")
            self.assertEqual([m["name"] for m in wbproxy.build_models()], ["Hy4 flash"])

            # 两区同名 → 补区域后缀，避免列表里两条一模一样的名字
            rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
            rt.region = "ai"
            rt.spec = wbproxy.REGIONS["ai"]
            rt.creds = Creds("ai")
            rt.error = None
            wbproxy.REGISTRY["ai"] = rt
            names = {m["region"]: m["name"] for m in wbproxy.build_models()}
            self.assertEqual(names["cn"], "Hy4 flash（国内版）")
            self.assertEqual(names["ai"], "Hy4 flash（国际版）")
        finally:
            wbproxy.REGISTRY.clear()
            wbproxy.REGISTRY.update(saved)


class CatalogFileTests(unittest.TestCase):
    def _models(self):
        return [{
            "slug": "cn/hy4flash", "region": "cn", "region_label": "国内版",
            "upstream_model": "hy4flash", "name": "Hy4 flash",
            "context_window": 960000, "max_output_tokens": 64000,
            "supports_images": True, "credits": "x0.03 credits",
            "reasoning_levels": ["high"], "default_reasoning_level": "high",
        }]

    def test_write_catalog_is_idempotent_and_keeps_names(self) -> None:
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cat.json"
            self.assertTrue(wbproxy.write_catalog(self._models(), str(path)))
            doc = _json.loads(path.read_text(encoding="utf-8"))
            entry = doc["models"][0]
            self.assertEqual(entry["display_name"], "Hy4 flash")
            self.assertEqual(entry["description"], "Hy4 flash")
            self.assertIn("tool_mode", entry)   # Codex 0.16x 需要这些字段
            # 名单没变就不重写（避免每次 /v1/models 都动文件）
            self.assertFalse(wbproxy.write_catalog(self._models(), str(path)))

    def test_write_catalog_rewrites_when_roster_changes(self) -> None:
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cat.json"
            wbproxy.write_catalog(self._models(), str(path))
            retired = self._models() + [dict(self._models()[0], slug="cn/hy3",
                                             upstream_model="hy3", name="Hy3")]
            self.assertTrue(wbproxy.write_catalog(retired, str(path)))
            slugs = [m["slug"] for m in _json.loads(path.read_text(encoding="utf-8"))["models"]]
            self.assertEqual(slugs, ["cn/hy4flash", "cn/hy3"])


class ConfigSyncTests(unittest.TestCase):
    def test_rewrite_changes_only_top_level_model_and_keeps_crlf(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            original = (
                'model = "ai/old"\r\n'
                'model_provider = "workbuddy"\r\n'
                '\r\n'
                '[model_providers.workbuddy]\r\n'
                'model = "must-not-change"\r\n'
            )
            path.write_bytes(original.encode("utf-8"))
            self.assertTrue(wbproxy._rewrite_model(str(path), "cn/hy3"))
            text = path.read_bytes().decode("utf-8")
            self.assertIn('model = "cn/hy3"\r\n', text)
            self.assertIn('model = "must-not-change"\r\n', text)
            self.assertEqual(text.count("\r\n"), original.count("\r\n"))
            self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_sync_writes_both_config_and_profile(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            prof = Path(d) / "wb.config.toml"
            cfg.write_text('model_provider = "workbuddy"\nmodel = "ai/old"\n', encoding="utf-8")
            prof.write_text('model_provider = "workbuddy"\nmodel = "ai/old"\n', encoding="utf-8")
            with mock.patch.object(wbproxy, "CODEX_CONFIG", str(cfg)), \
                    mock.patch.object(wbproxy, "PROFILE_CONFIG", str(prof)), \
                    mock.patch.object(wbproxy, "FOLLOW_LAST_MODEL", True):
                wbproxy._sync_config_model("cn/hy3")
            self.assertIn('model = "cn/hy3"', cfg.read_text(encoding="utf-8"))
            self.assertIn('model = "cn/hy3"', prof.read_text(encoding="utf-8"))
            self.assertIn('model_provider = "workbuddy"', prof.read_text(encoding="utf-8"))

    def test_sync_does_not_mutate_a_non_workbuddy_global_provider(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            prof = Path(d) / "wb.config.toml"
            cfg.write_text(
                'model_provider = "custom"\nmodel = "deepseek-flash"\n',
                encoding="utf-8",
            )
            prof.write_text(
                'model_provider = "workbuddy"\nmodel = "cn/hy4free"\n',
                encoding="utf-8",
            )
            with mock.patch.object(wbproxy, "CODEX_CONFIG", str(cfg)), \
                    mock.patch.object(wbproxy, "PROFILE_CONFIG", str(prof)), \
                    mock.patch.object(wbproxy, "FOLLOW_LAST_MODEL", True):
                wbproxy._sync_config_model("cn/hy3")
            self.assertEqual(
                cfg.read_text(encoding="utf-8"),
                'model_provider = "custom"\nmodel = "deepseek-flash"\n',
            )
            self.assertIn('model = "cn/hy3"', prof.read_text(encoding="utf-8"))

    def test_follow_last_model_off_writes_nothing(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text('model = "ai/old"\n', encoding="utf-8")
            with mock.patch.object(wbproxy, "CODEX_CONFIG", str(cfg)), \
                    mock.patch.object(wbproxy, "PROFILE_CONFIG", str(Path(d) / "missing.toml")), \
                    mock.patch.object(wbproxy, "FOLLOW_LAST_MODEL", False):
                wbproxy._sync_config_model("cn/hy3")
            self.assertIn('model = "ai/old"', cfg.read_text(encoding="utf-8"))


class HiddenRegionTests(unittest.TestCase):
    """`hidden_regions` removes a region from discovery, not from the account."""

    class Creds:
        region = "ai"

        def catalog(self):
            return [{
                "id": "hy3", "name": "Hy3", "context_window": 192000,
                "max_output_tokens": 64000, "supports_images": True,
                "reasoning_efforts": ["low", "high"], "default_effort": "high",
                "can_disable_thinking": False, "credits": "x0.00 credits",
            }]

    def setUp(self):
        import os
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._old_settings = os.environ.get("WB_SETTINGS")
        os.environ["WB_SETTINGS"] = str(Path(self._tmp.name) / "settings.json")
        self._old_registry = dict(wbproxy.REGISTRY)
        wbproxy.REGISTRY.clear()

    def tearDown(self):
        import os
        wbproxy.REGISTRY.clear()
        wbproxy.REGISTRY.update(self._old_registry)
        if self._old_settings is None:
            os.environ.pop("WB_SETTINGS", None)
        else:
            os.environ["WB_SETTINGS"] = self._old_settings
        self._tmp.cleanup()

    def _register(self):
        for region in ("ai", "cn"):
            rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
            rt.region = region
            rt.spec = wbproxy.REGIONS[region]
            rt.creds = self.Creds()
            rt.creds.region = region
            rt.error = None
            wbproxy.REGISTRY[region] = rt

    def test_hidden_region_is_absent_from_build_models(self):
        import wbsettings
        self._register()
        self.assertEqual({m["slug"] for m in wbproxy.build_models()},
                         {"ai/hy3", "cn/hy3"})
        wbsettings.set_region_hidden("ai", True)
        self.assertEqual({m["slug"] for m in wbproxy.build_models()}, {"cn/hy3"})

    def test_hidden_region_blocks_model_traffic_only(self):
        import wbsettings
        self._register()
        rt = wbproxy.runtime("ai")
        wbsettings.set_region_hidden("ai", True)
        with self.assertRaises(wbproxy.RegionUnavailable):
            rt.require()
        self.assertIs(rt.require(allow_hidden=True), rt.creds)
        self.assertTrue(rt.hidden)

    def test_show_restores_the_region(self):
        import wbsettings
        wbsettings.set_region_hidden("ai", True)
        wbsettings.set_region_hidden("ai", False)
        self.assertEqual(wbsettings.hidden_regions(), set())

    def test_unknown_region_is_rejected(self):
        import wbsettings
        with self.assertRaises(ValueError):
            wbsettings.set_region_hidden("eu", True)


class WbModelGlobalProviderTests(unittest.TestCase):
    def _module(self):
        import importlib.util
        path = BIN_DIR / "wb-model.py"
        spec = importlib.util.spec_from_file_location("wb_model_test", path)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        return module

    def test_use_switches_global_provider_catalog_and_model(self) -> None:
        import tempfile
        wb_model = self._module()
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / "config.toml"
            profile = home / "wb.config.toml"
            original = (
                'model = "deepseek-flash"\r\n'
                'model_provider = "deepseek"\r\n'
                'model_catalog_json = "C:/old/models.json"\r\n'
                '\r\n'
                '[model_providers.workbuddy]\r\n'
                'model = "must-not-change"\r\n'
            )
            config.write_bytes(original.encode("utf-8"))
            profile.write_text(
                'model_provider = "workbuddy"\nmodel = "ai/old"\n',
                encoding="utf-8",
            )

            changed, backup = wb_model.switch_global_defaults(home, "cn/hy3")

            text = config.read_bytes().decode("utf-8")
            self.assertEqual(
                wb_model.top_level_value(config, "model_provider"), "workbuddy"
            )
            self.assertEqual(wb_model.top_level_value(config, "model"), "cn/hy3")
            self.assertEqual(
                wb_model.top_level_value(config, "model_catalog_json"),
                (home / "workbuddy-catalog.json").resolve().as_posix(),
            )
            self.assertIn('model = "must-not-change"\r\n', text)
            self.assertNotIn("\n", text.replace("\r\n", ""))
            self.assertIn('model = "cn/hy3"', profile.read_text(encoding="utf-8"))
            self.assertIn("config.toml", changed)
            self.assertTrue(backup and backup.is_file())

            changed_again, backup_again = wb_model.switch_global_defaults(home, "cn/hy3")
            self.assertEqual(changed_again, [])
            self.assertIsNone(backup_again)


class RegionUnavailableHttpTests(unittest.TestCase):
    def test_missing_region_returns_400_without_fallback(self) -> None:
        import threading
        import urllib.request

        rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
        rt.region = "cn"
        rt.spec = wbproxy.REGIONS["cn"]
        rt.creds = None
        rt.error = "not signed in"
        old = dict(wbproxy.REGISTRY)
        wbproxy.REGISTRY["cn"] = rt
        srv = wbproxy._Server(("127.0.0.1", 0), wbproxy.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/wb/credits?region=cn", timeout=10)
            self.assertEqual(raised.exception.code, 400)
            body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], "region_unavailable")
            self.assertEqual(body["error"]["region"], "cn")
        finally:
            srv.shutdown()
            srv.server_close()
            wbproxy.REGISTRY.clear()
            wbproxy.REGISTRY.update(old)

    def test_windows_disables_address_reuse(self) -> None:
        if sys.platform == "win32":
            self.assertFalse(wbproxy._Server.allow_reuse_address)


class AcceptedRequestTrackingTests(unittest.TestCase):
    def test_failed_upstream_does_not_overwrite_last_model(self) -> None:
        import threading
        import urllib.request

        old_registry = dict(wbproxy.REGISTRY)
        old_last = dict(wbproxy.LAST_MODEL)
        old_open = wbproxy.open_upstream

        class DummyCreds:
            spec = wbproxy.REGIONS["cn"]

        rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
        rt.region = "cn"
        rt.spec = wbproxy.REGIONS["cn"]
        rt.creds = DummyCreds()
        rt.error = None
        wbproxy.REGISTRY["cn"] = rt
        wbproxy.LAST_MODEL.update({"model": "cn/old", "upstream_model": "old",
                                   "region": "cn", "ts": 1, "count": 7})

        def fail(creds, payload):
            raise wbproxy.UpstreamError(429, '{"code":6021,"msg":"queue full"}')

        wbproxy.open_upstream = fail
        srv = wbproxy._Server(("127.0.0.1", 0), wbproxy.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            body = json.dumps({"model": "cn/hy3", "stream": False,
                               "input": [{"type": "message", "role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/responses",
                                         data=body, headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=20)
            self.assertEqual(wbproxy.LAST_MODEL["model"], "cn/old")
            self.assertEqual(wbproxy.LAST_MODEL["count"], 7)
        finally:
            srv.shutdown()
            srv.server_close()
            wbproxy.open_upstream = old_open
            wbproxy.REGISTRY.clear()
            wbproxy.REGISTRY.update(old_registry)
            wbproxy.LAST_MODEL.clear()
            wbproxy.LAST_MODEL.update(old_last)

class StreamRobustnessTests(unittest.TestCase):
    """Upstream resets must still produce a terminal Responses event."""

    def test_stream_responses_events_always_emits_terminal_on_failed_finish(self):
        events = list(wbresponses.stream_responses_events([
            {"choices": [{"delta": {"content": "partial"}}]},
            {"choices": [{"delta": {}, "finish_reason": "failed"}]},
        ], "cn/hy3"))
        names = [name for name, _ in events]
        self.assertIn("response.created", names)
        self.assertEqual(names[-1], "response.completed")
        self.assertEqual(events[-1][1]["response"]["status"], "failed")

    def test_clean_eof_without_upstream_terminal_is_reported_as_failed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(wbproxy, "WB_DIAG_DIR", d):
            diag = wbproxy.StreamDiagnostics("truncated", endpoint="/v1/responses",
                                             slug="cn/hy3", region="cn",
                                             upstream_model="hy3", payload={})
            chat_events = [{"choices": [{"delta": {"content": "partial"}}]}]
            events = list(wbproxy._safe_responses_events(chat_events, "cn/hy3", diag))
            names = [name for name, _ in events]
            self.assertIn("response.failed", names)
            self.assertEqual(names[-1], "response.completed")
            response = events[-1][1]["response"]
            self.assertEqual(response["status"], "failed")
            self.assertEqual(
                response["error"]["code"],
                "upstream_closed_without_terminal_frame",
            )
            diag.end(completed=diag.completed)
            verdict = wbproxy._analyze_diagnostics(
                wbproxy.recent_diagnostics(20)
            )["requests"][0]["verdict"]
            self.assertEqual(verdict, "upstream_closed_without_terminal_frame")

    def test_safe_responses_events_recovers_from_upstream_transport_error(self):
        import tempfile
        from unittest import mock
        old_dir = wbproxy.WB_DIAG_DIR
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(wbproxy, "WB_DIAG_DIR", d):
            diag = wbproxy.StreamDiagnostics("test", endpoint="/v1/responses",
                                             slug="cn/hy3", region="cn",
                                             upstream_model="hy3", payload={})

            def broken():
                yield {"choices": [{"delta": {"content": "partial"}}]}
                raise ConnectionResetError(10054, "forcibly closed")

            events = list(wbproxy._safe_responses_events(broken(), "cn/hy3", diag))
            names = [name for name, _ in events]
            self.assertEqual(names[-1], "response.completed")
            self.assertEqual(events[-1][1]["response"]["status"], "failed")
            self.assertIn("response.failed", names)
            rows = wbproxy.recent_diagnostics(20)
            self.assertTrue(any(r["event"] == "upstream_error" for r in rows))
        wbproxy.WB_DIAG_DIR = old_dir

    def test_responses_terminal_error_has_failed_and_completed(self):
        events = list(wbproxy._responses_terminal_error("cn/hy3", "upstream reset"))
        self.assertEqual([name for name, _ in events],
                         ["response.failed", "response.completed"])
        self.assertEqual(events[0][1]["error"]["code"], "upstream_transport_error")

    def test_responses_terminal_error_usage_satisfies_responses_schema(self):
        """terminal 事件的 usage 必须是零值结构体，不能是空字典。

        Responses 把 usage 当必填结构体：空字典会让 Codex 在解析
        `response.completed` 时抛 `missing field input_tokens`，把上游真正的
        错误（例如 prompt is too long）完全盖住。
        """
        events = list(wbproxy._responses_terminal_error(
            "cn/deepseek-v4.1-flash", "prompt is too long: 1223456 tokens > 1048576 maximum"))
        for name, ev in events:
            usage = ev["response"]["usage"]
            self.assertIsInstance(usage, dict)
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                self.assertIn(key, usage, f"{name} 缺少 {key}")
                self.assertIsInstance(usage[key], int)
                self.assertGreaterEqual(usage[key], 0)
            self.assertIsInstance(usage.get("input_tokens_details"), dict)
            self.assertIsInstance(usage.get("output_tokens_details"), dict)
        # 用户可见的错误信息不能被 usage 解析问题吃掉
        self.assertIn("prompt is too long", events[0][1]["error"]["message"])

    def test_aggregate_response_fallback_has_valid_usage(self):
        """非流式兜底同样不能返回空 usage。"""
        final = wbresponses.aggregate_response([], "cn/hy3")
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIn(key, final["usage"])
            self.assertIsInstance(final["usage"][key], int)

    def test_wire_error_classifies_dns_and_reset_as_upstream(self):
        for exc in (ConnectionResetError(10054, "reset"), OSError(11001, "getaddrinfo failed")):
            info = wbproxy._wire_error(exc, phase="open")
            self.assertEqual(info["side"], "upstream")
            self.assertIn(info["category"], ("upstream_reset", "dns_resolution_failed"))

    def test_open_upstream_retries_transient_transport_once(self):
        from unittest import mock

        class Creds:
            spec = wbproxy.REGIONS["cn"]

            def ensure_fresh(self):
                return None

            def chat_headers(self):
                return {}

        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError(10054, "forcibly closed")
            return object()

        with mock.patch.object(wbproxy.urllib.request, "urlopen", flaky), \
                mock.patch.object(wbproxy.time, "sleep"):
            resp, _ = wbproxy.open_upstream(Creds(), {"messages": [], "model": "hy3"})
        self.assertIsNotNone(resp)
        self.assertEqual(len(calls), 2)


class ImageCapabilityTests(unittest.TestCase):
    def test_catalog_preserves_upstream_image_capability(self) -> None:
        import importlib.util

        gen_path = BIN_DIR / "gen-catalog.py"
        spec = importlib.util.spec_from_file_location("gen_catalog", gen_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        gen_catalog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen_catalog)

        rows = gen_catalog.build_catalog(
            [
                {"slug": "vision", "display_name": "Vision", "supports_images": True},
                {"slug": "already-vision", "display_name": "Already", "input_modalities": ["text", "image"]},
                {"slug": "text-only", "display_name": "Text", "supports_images": False},
                {"slug": "unknown", "display_name": "Unknown"},
            ]
        )
        modalities = {row["slug"]: row["input_modalities"] for row in rows}
        self.assertEqual(modalities["vision"], ["text", "image"])
        self.assertEqual(modalities["already-vision"], ["text", "image"])
        self.assertEqual(modalities["text-only"], ["text"])
        self.assertEqual(modalities["unknown"], ["text"])

    def test_unknown_or_disabled_multimodal_is_text_only(self) -> None:
        self.assertTrue(wbproxy.supports_images({"supportsImages": True}))
        self.assertFalse(wbproxy.supports_images({"supportsImages": False}))
        self.assertFalse(wbproxy.supports_images({}))
        self.assertFalse(
            wbproxy.supports_images({"supportsImages": True, "disabledMultimodal": True})
        )

    def test_responses_image_parts_become_chat_image_url(self) -> None:
        image = "data:image/png;base64,AQID"
        chat = wbresponses.request_to_chat(
            {
                "model": "hy3",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what color?"},
                            {"type": "input_image", "image_url": image},
                        ],
                    }
                ],
            }
        )
        content = chat["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what color?"})
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": image}})


class ProfileTests(unittest.TestCase):
    def test_profile_links_its_own_catalog(self) -> None:
        home = Path("C:/Users/Example/.codex")
        body = setup.profile_body(home)
        self.assertIn('model_provider = "workbuddy"', body)
        self.assertIn('model_catalog_json = "C:/Users/Example/.codex/workbuddy-catalog.json"', body)

    def test_desktop_launcher_is_shipped(self) -> None:
        launcher = PLUGIN_ROOT / "scripts" / "start-codex-app-wb.ps1"
        self.assertTrue(launcher.is_file())
        text = launcher.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("Resolve-CodexCli", text)
        self.assertIn("& $codex app", text)
        self.assertIn('model_provider="workbuddy"', text)
        self.assertIn("model_catalog_json=", text)

    def test_desktop_cmd_launcher_bypasses_execution_policy(self) -> None:
        launcher = PLUGIN_ROOT / "scripts" / "start-codex-app-wb.cmd"
        self.assertTrue(launcher.is_file())
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("-ExecutionPolicy Bypass", text)
        self.assertIn("start-codex-app-wb.ps1", text)

    def test_profile_catalog_path_handles_platform_native_home(self) -> None:
        home = Path.cwd() / "example-home"
        body = setup.profile_body(home)
        expected = (home / "workbuddy-catalog.json").as_posix()
        self.assertIn(f'model_catalog_json = "{expected}"', body)

    def test_provider_disables_codex_429_retries(self) -> None:
        self.assertIn('base_url = "http://127.0.0.1:{port}/v1"',
                      setup.PROVIDER_BLOCK)
        self.assertNotIn("http://localhost:", setup.PROVIDER_BLOCK)
        self.assertIn("request_max_retries = 0", setup.PROVIDER_BLOCK)
        self.assertIn("stream_max_retries = 0", setup.PROVIDER_BLOCK)

    def test_profile_default_uses_region_prefix(self) -> None:
        body = setup.profile_body(Path("C:/Users/Example/.codex"))
        self.assertIn('model = "cn/deepseek-v4.1-flash"', body)

    def test_reinstall_preserves_model_and_prefixes_legacy_ids(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            prof = home / "wb.config.toml"
            prof.write_text('model_provider = "workbuddy"\nmodel = "hy3"\n', encoding="utf-8")
            setup.step_profile(home)
            self.assertIn('model = "ai/hy3"', prof.read_text(encoding="utf-8"))

            prof.write_text('model_provider = "workbuddy"\nmodel = "cn/hy3"\n', encoding="utf-8")
            setup.step_profile(home)
            self.assertIn('model = "cn/hy3"', prof.read_text(encoding="utf-8"))


class ProxyHealthTests(unittest.TestCase):
    def test_watcher_restarts_only_its_unhealthy_proxy(self) -> None:
        with mock.patch.object(watch_plugin, "is_active", return_value=(True, "active")), \
                mock.patch.object(watch_plugin, "proxy_running", return_value=(True, "listening")), \
                mock.patch.object(watch_plugin, "proxy_health", return_value=(False, "hung")), \
                mock.patch.object(watch_plugin, "our_pids", return_value=["4242"]), \
                mock.patch.object(watch_plugin, "stop_proxy", return_value=True) as stopped, \
                mock.patch.object(watch_plugin, "start_proxy", return_value=True) as started:
            self.assertEqual(watch_plugin.reconcile(Path("config.toml"), 8787), "restarted")
            stopped.assert_called_once_with(8787)
            started.assert_called_once_with(8787)

    def test_watcher_never_kills_an_unhealthy_foreign_listener(self) -> None:
        with mock.patch.object(watch_plugin, "is_active", return_value=(True, "active")), \
                mock.patch.object(watch_plugin, "proxy_running", return_value=(True, "listening")), \
                mock.patch.object(watch_plugin, "proxy_health", return_value=(False, "wrong provider")), \
                mock.patch.object(watch_plugin, "our_pids", return_value=[]), \
                mock.patch.object(watch_plugin, "stop_proxy") as stopped, \
                mock.patch.object(watch_plugin, "start_proxy") as started:
            self.assertEqual(watch_plugin.reconcile(Path("config.toml"), 8787),
                             "unhealthy-port")
            stopped.assert_not_called()
            started.assert_not_called()



class _FakeCreds:
    """Minimal credential object for account/status tests (no network)."""

    app = "C:/fake/WorkBuddy.exe"
    auth_file = "C:/fake/auth.info"
    app_version = "1.0.0"
    expires_at = 1_800_000_000.0

    def __init__(self, *, nickname="Alice", uid="uid-1234567890", enterprise_id="",
                 credits=None, credit_error=None):
        self.nickname = nickname
        self.uid = uid
        self.enterprise_id = enterprise_id
        self.access_token = "super-secret-token"
        self.refresh_token = "refresh-secret"
        self.domain = "www.workbuddy.ai"
        self.credits_value = credits or {"region": "ai", "remaining": 12.5, "total": 20.0, "packages": []}
        self.credit_error = credit_error
        self.credits_calls = 0

    def expired(self):
        return False

    def credits(self):
        self.credits_calls += 1
        if self.credit_error is not None:
            raise self.credit_error
        return self.credits_value


def _runtime(region, creds=None, error=None):
    rt = wbproxy.RegionRuntime.__new__(wbproxy.RegionRuntime)
    rt.region = region
    rt.spec = wbproxy.REGIONS[region]
    rt.creds = creds
    rt.error = error
    return rt


class AccountStatusTests(unittest.TestCase):
    def setUp(self):
        self.old_registry = dict(wbproxy.REGISTRY)
        wbproxy.REGISTRY.clear()
        wbproxy.ACCOUNT_STATUS_CACHE.clear()

    def tearDown(self):
        wbproxy.REGISTRY.clear()
        wbproxy.REGISTRY.update(self.old_registry)
        wbproxy.ACCOUNT_STATUS_CACHE.clear()

    def test_health_reports_nickname_and_enterprise(self):
        creds = _FakeCreds(nickname="Alice", enterprise_id="ent-1")
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        health = wbproxy.REGISTRY["ai"].health()
        self.assertEqual(health["nickname"], "Alice")
        self.assertEqual(health["enterprise_id"], "ent-1")

    def test_account_status_uses_nickname_and_credit_total(self):
        creds = _FakeCreds(nickname="Alice", uid="uid-1234567890")
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        status = wbproxy.account_status("ai", refresh=True)
        self.assertTrue(status["available"])
        self.assertEqual(status["nickname"], "Alice")
        self.assertEqual(status["display_name"], "Alice")
        self.assertEqual(status["uid_tail"], "1234567890"[-8:])
        self.assertEqual(status["credits"]["remaining"], 12.5)
        self.assertEqual(status["credits"]["total"], 20.0)

    def test_missing_nickname_falls_back_to_uid_tail(self):
        creds = _FakeCreds(nickname="", uid="uid-1234567890")
        wbproxy.REGISTRY["cn"] = _runtime("cn", creds)
        status = wbproxy.account_status("cn", refresh=True)
        self.assertEqual(status["display_name"], "UID 34567890")

    def test_unavailable_region_does_not_hide_the_other(self):
        wbproxy.REGISTRY["ai"] = _runtime("ai", _FakeCreds(nickname="Alice"))
        wbproxy.REGISTRY["cn"] = _runtime("cn", creds=None, error="not signed in")
        document = wbproxy.account_status_document(refresh=True)
        self.assertTrue(document["regions"]["ai"]["available"])
        self.assertFalse(document["regions"]["cn"]["available"])
        self.assertIn("not signed in", document["regions"]["cn"]["error"])

    def test_partial_failure_endpoint_returns_200(self):
        import threading, urllib.request
        wbproxy.REGISTRY["ai"] = _runtime("ai", _FakeCreds(nickname="Alice"))
        wbproxy.REGISTRY["cn"] = _runtime("cn", creds=None, error="not signed in")
        srv = wbproxy._Server(("127.0.0.1", 0), wbproxy.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/wb/account-status", timeout=10) as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(body["regions"]["ai"]["nickname"], "Alice")
            self.assertFalse(body["regions"]["cn"]["available"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_credit_failure_is_not_reported_as_zero(self):
        creds = _FakeCreds(nickname="Alice", credit_error=RuntimeError("billing exploded"))
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        status = wbproxy.account_status("ai", refresh=True)
        self.assertTrue(status["available"])
        self.assertIsNone(status["credits"])
        self.assertIn("billing exploded", status["error"])

    def test_default_reads_live_and_refresh_zero_uses_cache(self):
        creds = _FakeCreds(nickname="Alice")
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        wbproxy.account_status("ai", refresh=True)
        wbproxy.account_status("ai", refresh=False)
        wbproxy.account_status("ai", refresh=False)
        self.assertEqual(creds.credits_calls, 1)
        wbproxy.account_status("ai", refresh=True)
        self.assertEqual(creds.credits_calls, 2)

    def test_response_omits_tokens_and_full_uid(self):
        creds = _FakeCreds(nickname="Alice", uid="uid-1234567890")
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        raw = json.dumps(wbproxy.account_status_document(refresh=True), ensure_ascii=False)
        self.assertNotIn("super-secret-token", raw)
        self.assertNotIn("refresh-secret", raw)
        self.assertNotIn("uid-1234567890", raw)
        self.assertNotIn("Authorization", raw)
        self.assertIn("34567890", raw)

    def test_credit_error_is_redacted_before_it_reaches_status(self):
        creds = _FakeCreds(
            nickname="Alice",
            credit_error=RuntimeError(
                'HTTP 500 body={"accessToken":"super-secret-token",'
                '"refreshToken":"refresh-secret","detail":"upstream exploded"}'
            ),
        )
        wbproxy.REGISTRY["ai"] = _runtime("ai", creds)
        raw = json.dumps(wbproxy.account_status_document(refresh=True), ensure_ascii=False)
        self.assertNotIn("super-secret-token", raw)
        self.assertNotIn("refresh-secret", raw)
        self.assertNotIn("upstream exploded", raw)
        self.assertIn("RuntimeError", raw)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EnterpriseCreditsTests(unittest.TestCase):
    def _creds(self, enterprise_id):
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.region = "cn"
        creds.spec = wbproxy.REGIONS["cn"]
        creds.access_token = "cn-token"
        creds.uid = "cn-user"
        creds.enterprise_id = enterprise_id
        creds.domain = "www.codebuddy.cn"
        creds.app_version = "37.10.3-42"
        return creds

    def test_cn_enterprise_uses_enterprise_endpoint_and_headers(self):
        creds = self._creds("ent-1")
        payload = {"code": 0, "data": {"limitNum": 1000, "credit": 250, "cycleResetTime": "2026-11-01 00:00:00"}}
        with mock.patch.object(wbproxy.urllib.request, "urlopen", return_value=_FakeResponse(payload)) as opened:
            result = creds.credits()
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v2/billing/meter/get-enterprise-user-usage"))
        self.assertEqual(request.data, b"{}")
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers.get("x-tenant-id"), "ent-1")
        self.assertEqual(result["remaining"], 750.0)
        self.assertEqual(result["total"], 1000.0)

    def test_cn_enterprise_negative_limit_marks_unlimited(self):
        creds = self._creds("ent-1")
        payload = {"code": 0, "data": {"data": {"limitNum": -1}}}
        with mock.patch.object(wbproxy.urllib.request, "urlopen", return_value=_FakeResponse(payload)):
            result = creds.credits()
        self.assertTrue(result["unlimited"])
        self.assertEqual(result["remaining"], 0)


class NicknameCredentialTests(unittest.TestCase):
    def _load(self, auth_doc, encrypted_key=b""):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            auth_file = Path(directory) / "workbuddy-desktop-ai.info"
            auth_file.write_text(json.dumps(auth_doc), encoding="utf-8")
            with mock.patch.object(wbproxy.wbdiscover, "find_app", return_value=""), \
                    mock.patch.object(wbproxy.wbdiscover, "find_auth_file", return_value=str(auth_file)), \
                    mock.patch.object(wbproxy, "_protector_key_for", return_value=encrypted_key):
                return wbproxy.Credentials("ai").nickname

    def _base_doc(self, nickname):
        return {
            "auth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 1_800_000_000_000,
                     "domain": "www.workbuddy.ai"},
            "account": {"uid": "uid-1", "nickname": nickname},
        }

    def test_plain_nickname_is_read(self):
        self.assertEqual(self._load(self._base_doc("Alice")), "Alice")

    def test_unreadable_nickname_leaves_credentials_usable(self):
        doc = self._base_doc("$wbEncrypted-but-not-an-envelope")
        self.assertEqual(self._load(doc), "")

    def test_encrypted_nickname_is_decrypted(self):
        import base64, hashlib, os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = hashlib.sha256(b"unit-test-key").digest()
        key_id = hashlib.sha256(key).hexdigest()[:16]
        nonce = os.urandom(12)
        sealed = AESGCM(key).encrypt(nonce, b"Alice", wbproxy._aad(key_id, 1))
        envelope = base64.b64encode(json.dumps({
            "suite": 1, "keyId": key_id, "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(sealed[:-16]).decode(),
            "authTag": base64.b64encode(sealed[-16:]).decode(),
        }).encode("utf-8")).decode("ascii")
        doc = self._base_doc({"$wbEncrypted": 1, "envelope": envelope})
        self.assertEqual(self._load(doc, encrypted_key=key), "Alice")


class SetupStatusTests(unittest.TestCase):
    def _home(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        (home / "config.toml").write_text(
            'model_provider = "workbuddy"\n'
            'model = "cn/hy3"\n'
            f'model_catalog_json = "{(home / "workbuddy-catalog.json").as_posix()}"\n'
            '[model_providers.workbuddy]\nname = "WorkBuddy"\n',
            encoding="utf-8",
        )
        (home / "wb.config.toml").write_text(
            'model_provider = "workbuddy"\nmodel = "cn/hy3"\n'
            f'model_catalog_json = "{(home / "workbuddy-catalog.json").as_posix()}"\n',
            encoding="utf-8",
        )
        (home / "workbuddy-catalog.json").write_text(
            json.dumps({"models": [{"slug": "ai/hy3"}, {"slug": "cn/hy3"}]}), encoding="utf-8")
        (home / "skills" / "query-current-model").mkdir(parents=True)
        (home / "AGENTS.md").write_text(setup.MARK_BEGIN + "\nrule\n" + setup.MARK_END + "\n", encoding="utf-8")
        return home

    def _regions(self):
        return {
            "ai": {"region": "ai", "label": "国际版", "available": True, "nickname": "Alice",
                   "display_name": "Alice", "uid_tail": "34567890", "credits": {"remaining": 318.93, "total": 470.0}},
            "cn": {"region": "cn", "label": "国内版", "available": False, "nickname": None,
                   "display_name": None, "uid_tail": None, "credits": None, "error": "not signed in"},
        }

    def test_status_document_exposes_install_regions_and_last_model(self):
        home = self._home()
        with mock.patch.object(setup, "fetch_account_status", return_value=self._regions()), \
             mock.patch.object(setup, "fetch_last_model", return_value={"model": "cn/hy3", "region": "cn"}), \
             mock.patch.object(setup, "port_open", return_value=True):
            document = setup.build_status_document(home, 8787)
        self.assertEqual(set(document), {"install", "regions", "last_model"})
        self.assertIn("provider_installed", document["install"])
        self.assertEqual(document["install"]["global_provider"], "workbuddy")
        self.assertEqual(
            document["install"]["global_catalog"],
            (home / "workbuddy-catalog.json").as_posix(),
        )
        self.assertIn("catalog_models", document["install"])
        self.assertEqual(document["regions"]["cn"]["error"], "not signed in")
        self.assertEqual(document["last_model"]["model"], "cn/hy3")

    def test_cmd_status_json_returns_zero_when_a_region_fails(self):
        import contextlib, io
        home = self._home()
        with mock.patch.object(setup, "codex_home", return_value=home), \
             mock.patch.object(setup, "fetch_account_status", return_value=self._regions()), \
             mock.patch.object(setup, "fetch_last_model", return_value=None), \
             mock.patch.object(setup, "port_open", return_value=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = setup.cmd_status(type("Args", (), {"port": 8787, "json": True})())
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue())
        self.assertFalse(document["regions"]["cn"]["available"])

    def test_cmd_status_text_shows_accounts_and_credits(self):
        import contextlib, io
        home = self._home()
        with mock.patch.object(setup, "codex_home", return_value=home), \
             mock.patch.object(setup, "fetch_account_status", return_value=self._regions()), \
             mock.patch.object(setup, "fetch_last_model", return_value=None), \
             mock.patch.object(setup, "port_open", return_value=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = setup.cmd_status(type("Args", (), {"port": 8787, "json": False})())
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("账号与积分", text)
        self.assertIn("Alice", text)
        self.assertIn("318.93", text)
        self.assertIn("not signed in", text)


class HiddenWindowAndDiscoveryCacheTests(unittest.TestCase):
    def test_run_hidden_uses_windows_no_console_startupinfo(self):
        import wbspawn

        if sys.platform != "win32":
            self.skipTest("Windows-only helper")
        with mock.patch.object(wbspawn.subprocess, "run", return_value="ok") as run:
            self.assertEqual(wbspawn.run_hidden(["powershell"], capture_output=True), "ok")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], wbspawn.CREATE_NO_WINDOW)
        self.assertTrue(kwargs["startupinfo"].dwFlags & wbspawn.STARTF_USESHOWWINDOW)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)

    def test_find_app_caches_automatic_discovery(self):
        import wbdiscover

        old_cache = dict(wbdiscover._DISCOVERY_CACHE)
        wbdiscover.clear_discovery_cache()
        try:
            with mock.patch.object(wbdiscover, "_app_from_registry", return_value=None) as registry, \
                    mock.patch.object(wbdiscover, "_app_from_process", return_value=None), \
                    mock.patch.object(wbdiscover, "_app_from_common_paths", return_value="C:/fake/WorkBuddy.exe"):
                self.assertEqual(wbdiscover.find_app("cn"), "C:/fake/WorkBuddy.exe")
                self.assertEqual(wbdiscover.find_app("cn"), "C:/fake/WorkBuddy.exe")
            registry.assert_called_once()
        finally:
            wbdiscover.clear_discovery_cache()
            wbdiscover._DISCOVERY_CACHE.update(old_cache)

    def test_unavailable_region_health_reuses_cached_paths(self):
        rt = _runtime("ai", creds=None, error="missing credential")
        rt._paths_cache = None
        with mock.patch.object(wbproxy.wbdiscover, "find_app", return_value="C:/fake/app.exe") as find_app, \
                mock.patch.object(wbproxy.wbdiscover, "find_auth_file", return_value=None) as find_auth:
            first = rt.health()
            second = rt.health()
        self.assertEqual(first["app"], "C:/fake/app.exe")
        self.assertEqual(second["app"], "C:/fake/app.exe")
        find_app.assert_called_once_with("ai")
        find_auth.assert_called_once_with("ai")


if __name__ == "__main__":
    unittest.main()
