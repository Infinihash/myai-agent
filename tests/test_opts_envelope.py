"""Tests for the coordinator options envelope port (T10694).

Covers:
  * envelope stripping (_extract_opts_envelope) incl. malformed JSON tolerance
  * option merging (_apply_opts_to_payload)
  * num_ctx belt-and-braces default (_ensure_num_ctx / MYAI_NUM_CTX)
  * token reporting via a real fake Ollama HTTP server (run_ollama_full)

Pure stdlib — run with `python -m unittest` or `pytest`.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

# src-layout: make the package importable without an install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent import agent  # noqa: E402
from myai_agent.agent import (  # noqa: E402
    MYAI_OPTS_SENTINEL,
    _apply_opts_to_payload,
    _ensure_num_ctx,
    _extract_opts_envelope,
    _num_ctx_default,
    _usage_from_response,
    run_ollama_full,
)


def _envelope_msg(extras: dict) -> dict:
    return {"role": "system",
            "content": MYAI_OPTS_SENTINEL + json.dumps(extras, separators=(",", ":"))}


class ExtractEnvelopeTests(unittest.TestCase):
    def test_strips_first_system_sentinel_message(self):
        extras = {"format": "json", "options": {"num_ctx": 16384}, "keep_alive": "5m"}
        msgs = [_envelope_msg(extras), {"role": "user", "content": "hi"}]
        cleaned, got = _extract_opts_envelope(msgs)
        self.assertEqual(cleaned, [{"role": "user", "content": "hi"}])
        self.assertEqual(got, extras)

    def test_no_envelope_returns_unchanged(self):
        msgs = [{"role": "user", "content": "hi"}]
        cleaned, got = _extract_opts_envelope(msgs)
        self.assertEqual(cleaned, msgs)
        self.assertEqual(got, {})

    def test_only_first_message_considered(self):
        # A sentinel that is NOT first must be left untouched.
        second = _envelope_msg({"format": "json"})
        msgs = [{"role": "user", "content": "hi"}, second]
        cleaned, got = _extract_opts_envelope(msgs)
        self.assertEqual(cleaned, msgs)
        self.assertEqual(got, {})

    def test_non_system_role_ignored(self):
        msg = {"role": "user",
               "content": MYAI_OPTS_SENTINEL + json.dumps({"format": "json"})}
        cleaned, got = _extract_opts_envelope([msg])
        self.assertEqual(cleaned, [msg])
        self.assertEqual(got, {})

    def test_malformed_json_tolerated(self):
        bad = {"role": "system", "content": MYAI_OPTS_SENTINEL + "{not valid json"}
        user = {"role": "user", "content": "hi"}
        cleaned, got = _extract_opts_envelope([bad, user])
        # Envelope is still stripped, extras is empty (ignored).
        self.assertEqual(cleaned, [user])
        self.assertEqual(got, {})

    def test_non_dict_json_tolerated(self):
        bad = {"role": "system", "content": MYAI_OPTS_SENTINEL + "[1,2,3]"}
        cleaned, got = _extract_opts_envelope([bad])
        self.assertEqual(cleaned, [])
        self.assertEqual(got, {})

    def test_empty_messages(self):
        cleaned, got = _extract_opts_envelope([])
        self.assertEqual(cleaned, [])
        self.assertEqual(got, {})


class ApplyOptsTests(unittest.TestCase):
    def test_merges_all_fields(self):
        payload = {"model": "m", "messages": []}
        extras = {"format": "json", "keep_alive": "10m",
                  "options": {"num_ctx": 32768, "temperature": 0.2}}
        _apply_opts_to_payload(payload, extras)
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(payload["options"], {"num_ctx": 32768, "temperature": 0.2})

    def test_empty_extras_noop(self):
        payload = {"model": "m"}
        _apply_opts_to_payload(payload, {})
        self.assertEqual(payload, {"model": "m"})

    def test_none_and_empty_options_skipped(self):
        payload = {"model": "m"}
        _apply_opts_to_payload(payload, {"format": None, "keep_alive": None, "options": {}})
        self.assertEqual(payload, {"model": "m"})


class NumCtxTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("MYAI_NUM_CTX", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MYAI_NUM_CTX", None)
        else:
            os.environ["MYAI_NUM_CTX"] = self._saved

    def test_default_is_8192(self):
        self.assertEqual(_num_ctx_default(), 8192)

    def test_env_override(self):
        os.environ["MYAI_NUM_CTX"] = "16384"
        self.assertEqual(_num_ctx_default(), 16384)

    def test_bad_env_falls_back(self):
        os.environ["MYAI_NUM_CTX"] = "not-a-number"
        self.assertEqual(_num_ctx_default(), 8192)

    def test_ensure_adds_default_when_absent(self):
        payload = {"model": "m", "prompt": "x"}
        _ensure_num_ctx(payload)
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    def test_ensure_does_not_override_existing(self):
        payload = {"model": "m", "options": {"num_ctx": 32768}}
        _ensure_num_ctx(payload)
        self.assertEqual(payload["options"]["num_ctx"], 32768)

    def test_ensure_preserves_other_options(self):
        payload = {"model": "m", "options": {"temperature": 0.5}}
        _ensure_num_ctx(payload)
        self.assertEqual(payload["options"]["temperature"], 0.5)
        self.assertEqual(payload["options"]["num_ctx"], 8192)


class UsageParseTests(unittest.TestCase):
    def test_maps_ollama_counts(self):
        meta = _usage_from_response({"prompt_eval_count": 44, "eval_count": 91})
        self.assertEqual(meta["tokens_in"], 44)
        self.assertEqual(meta["tokens_out"], 91)
        self.assertEqual(meta["prompt_eval_count"], 44)
        self.assertEqual(meta["eval_count"], 91)

    def test_missing_counts(self):
        self.assertEqual(_usage_from_response({}), {})
        self.assertEqual(_usage_from_response(None), {})


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    """Records the last request body and returns a canned Ollama response."""

    last_path = None
    last_body = None
    chat_response = {
        "message": {"content": "  hello world  "},
        "prompt_eval_count": 123,
        "eval_count": 45,
        "done": True,
    }

    def log_message(self, *_a):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).last_path = self.path
        type(self).last_body = json.loads(raw.decode())
        body = json.dumps(self.chat_response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeServerTests(unittest.TestCase):
    """Exercises the full urllib request path against a real HTTP server."""

    def setUp(self):
        _FakeOllamaHandler.last_path = None
        _FakeOllamaHandler.last_body = None
        self.server = HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ.pop("MYAI_NUM_CTX", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_chat_strips_envelope_sets_num_ctx_reports_tokens(self):
        messages = [
            _envelope_msg({"format": "json", "options": {"num_ctx": 16384}}),
            {"role": "user", "content": "needle?"},
        ]
        text, meta = run_ollama_full("bonsai-8b", json.dumps(messages),
                                     ollama_url=self.url, timeout=10)

        # Response text is trimmed.
        self.assertEqual(text, "hello world")
        # Tokens forwarded under both naming schemes.
        self.assertEqual(meta["tokens_in"], 123)
        self.assertEqual(meta["tokens_out"], 45)

        # The sentinel message was stripped before hitting Ollama.
        sent = _FakeOllamaHandler.last_body
        self.assertEqual(_FakeOllamaHandler.last_path, "/api/chat")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "needle?"}])
        # Envelope options honored (num_ctx came from the coordinator).
        self.assertEqual(sent["options"]["num_ctx"], 16384)
        self.assertEqual(sent["format"], "json")

    def test_chat_without_envelope_gets_num_ctx_default(self):
        messages = [{"role": "user", "content": "hi"}]
        run_ollama_full("bonsai-8b", json.dumps(messages),
                        ollama_url=self.url, timeout=10)
        sent = _FakeOllamaHandler.last_body
        # No envelope → belt-and-braces default applied.
        self.assertEqual(sent["options"]["num_ctx"], 8192)

    def test_generate_path_gets_num_ctx_default(self):
        run_ollama_full("bonsai-8b", "plain prompt", ollama_url=self.url, timeout=10)
        sent = _FakeOllamaHandler.last_body
        self.assertEqual(_FakeOllamaHandler.last_path, "/api/generate")
        self.assertEqual(sent["options"]["num_ctx"], 8192)


if __name__ == "__main__":
    unittest.main()
