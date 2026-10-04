"""Tests for the plain-writing (anti-AI-slop) token saver.

PLAIN_WRITING_BAN_LIST + _apply_plain_writing fold a "write plainly, no filler,
avoid these words" directive into the system message of prose-producing model
calls (/api/chat by default, ask lane, clarify lane). The point is FEWER billed
tokens, not a style law: shorter replies mean fewer output tokens, and the
directive itself is the only input cost. JSON-structured prompts are exempt so
a schema is never truncated mid-object.

Hermetic: DB redirected to a throwaway temp dir; model calls mocked so nothing
is ever billed.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import serve  # noqa: E402

_MODULE_TMP_DIR = tempfile.mkdtemp(prefix="think-tank-plain-writing-")


def setUpModule():
    global _MODULE_PATCHER
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, "think-tank.db"),
        THINK_TANK_DIR=os.path.join(_MODULE_TMP_DIR, "think-tank"),
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, "agents"),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, "library"),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, "passport.json"),
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _client():
    return TestClient(serve.app)


class PlainWritingConstants(unittest.TestCase):
    def test_ban_list_covers_the_high_signal_ai_tells(self):
        lowered = [w.lower() for w in serve.PLAIN_WRITING_BAN_LIST]
        for word in ("leverage", "utilize", "seamless", "robust", "delve",
                     "moreover", "furthermore", "overall", "it is worth noting"):
            self.assertIn(word, lowered)

    def test_directive_interpolates_the_ban_list(self):
        d = serve.PLAIN_WRITING_DIRECTIVE.format(banned=", ".join(serve.PLAIN_WRITING_BAN_LIST))
        self.assertIn("leverage", d)
        self.assertIn("Write plainly", d)


class ApplyPlainWriting(unittest.TestCase):
    def test_merges_directive_into_first_system_message(self):
        msgs = [{"role": "system", "content": "You are Ada."},
                {"role": "user", "content": "hello"}]
        out = serve._apply_plain_writing(msgs)
        self.assertEqual(len(out), 2)
        self.assertIn("You are Ada.", out[0]["content"])
        self.assertIn("Write plainly", out[0]["content"])
        self.assertIn("leverage", out[0]["content"])
        self.assertEqual(out[1], {"role": "user", "content": "hello"})
        # Original list untouched (returns a copy with the system msg replaced).
        self.assertEqual(msgs[0]["content"], "You are Ada.")

    def test_no_system_message_returns_list_unchanged(self):
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(serve._apply_plain_writing(msgs), msgs)

    def test_non_dict_entries_tolerated(self):
        msgs = ["junk", {"role": "system", "content": "sys"}]
        out = serve._apply_plain_writing(msgs)
        self.assertIn("Write plainly", out[1]["content"])


class ChatEndpointPlainWriting(unittest.TestCase):
    def _post(self, body, captured):
        def fake(model, messages, max_tokens, best_of=1):
            captured["messages"] = messages
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.0}}
        with unittest.mock.patch.object(serve, "OPENROUTER_API_KEY", "k"), \
             unittest.mock.patch.object(serve, "verify_session", return_value=True), \
             unittest.mock.patch.object(serve, "_call_openrouter_sync", side_effect=fake), \
             unittest.mock.patch.object(serve, "_accrue_spend"), \
             unittest.mock.patch.object(serve, "log_action"):
            return _client().post("/api/chat", json=body)

    def test_applies_by_default_to_prose(self):
        captured = {}
        body = {"model": "m", "messages": [{"role": "system", "content": "You are Ada."},
                                           {"role": "user", "content": "hello"}]}
        r = self._post(body, captured)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Write plainly", captured["messages"][0]["content"])

    def test_plain_false_opts_out(self):
        captured = {}
        body = {"model": "m", "plain": False,
                "messages": [{"role": "system", "content": "You are Ada."},
                             {"role": "user", "content": "hello"}]}
        r = self._post(body, captured)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("Write plainly", captured["messages"][0]["content"])

    def test_json_prompt_is_exempt(self):
        captured = {}
        body = {"model": "m",
                "messages": [{"role": "system",
                              "content": "Respond with ONLY valid JSON, no other text."},
                             {"role": "user", "content": "extract"}]}
        r = self._post(body, captured)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("Write plainly", captured["messages"][0]["content"])


class LanesPlainWriting(unittest.TestCase):
    def test_clarify_messages_include_the_directive(self):
        agent = {"id": "ben", "name": "Ben", "role": "Banking",
                 "profile": {"mission": "keep the books straight"}}
        msgs = serve._clarify_in_character_messages(agent, [], "Parser", "q")
        self.assertIn("Write plainly", msgs[0]["content"])
        self.assertIn("leverage", msgs[0]["content"])

    def test_ask_core_include_the_directive(self):
        seen = {}

        def fake_post(model, messages, tools=None, max_tokens=None, tool_choice=None):
            seen["system"] = messages[0]["content"]
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        state = {
            "sim": {"owner": "server"},
            "agentRoster": [{"id": "ada", "name": "Ada", "role": "worker"}],
            "agents": {"ada": {"id": "ada", "name": "Ada", "role": "worker", "busy": False,
                               "offDuty": False, "profile": {"mission": "x"}}},
        }
        c = _client()
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        with unittest.mock.patch.object(serve, "get_state_from_db", return_value=state), \
             unittest.mock.patch.object(serve, "_coding_tier_slug", return_value="m"), \
             unittest.mock.patch.object(serve, "_mid_tier_slug", return_value="m"), \
             unittest.mock.patch.object(serve, "_low_tier_slug", return_value="m"), \
             unittest.mock.patch.object(serve, "_post_openrouter_raw", side_effect=fake_post), \
             unittest.mock.patch.object(serve, "get_or_create_agent_key", return_value="k"):
            r = c.post("/api/intent/ask", json={"question": "what's the weather?"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Write plainly", seen["system"])
        self.assertIn("leverage", seen["system"])


if __name__ == "__main__":
    unittest.main()
