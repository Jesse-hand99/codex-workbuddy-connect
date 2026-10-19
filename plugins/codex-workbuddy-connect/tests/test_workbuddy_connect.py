from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = PLUGIN_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))

import setup  # noqa: E402
import wbproxy  # noqa: E402
import wbresponses  # noqa: E402


class ChatHeaderTests(unittest.TestCase):
    def test_chat_headers_include_fresh_conversation_trio(self) -> None:
        creds = wbproxy.Credentials.__new__(wbproxy.Credentials)
        creds.access_token = "local-test-token"
        creds.app_version = "1.2.3"
        creds.uid = "test-user"
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
        self.assertIn("& codex app", text)
        self.assertIn('model_provider="workbuddy"', text)
        self.assertIn("model_catalog_json=", text)

    def test_profile_catalog_path_handles_platform_native_home(self) -> None:
        home = Path.cwd() / "example-home"
        body = setup.profile_body(home)
        expected = (home / "workbuddy-catalog.json").as_posix()
        self.assertIn(f'model_catalog_json = "{expected}"', body)



if __name__ == "__main__":
    unittest.main()
