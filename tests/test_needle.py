"""Tests for the needle self-test subcommand (T10768).

Tests the needle command's prompt-building logic and the assertion logic
using a fake Ollama server — does NOT require a real Ollama instance.

Pure stdlib — run with `python -m unittest` or `pytest`.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent.agent import run_ollama_full, _ensure_num_ctx


# ── fake Ollama server ─────────────────────────────────────────────────────────

class _NeedleOllamaHandler(BaseHTTPRequestHandler):
    """Echos the needle back from the request prompt."""

    last_body: dict = {}

    def log_message(self, *_): pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        body   = json.loads(raw.decode())
        type(self).last_body = body

        # Extract needle from prompt
        prompt = body.get("prompt", "")
        needle = ""
        for part in prompt.split():
            if part.startswith("NEEDLE-"):
                needle = part
                break

        response = json.dumps({
            "response":           needle or "not-found",
            "prompt_eval_count":  len(prompt) // 4,
            "eval_count":         4,
            "done":               True,
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class NeedlePromptTests(unittest.TestCase):
    """Unit-level: validate prompt building without a server."""

    def _build_needle_prompt(self, needle: str, filler_tokens=5000, tail_tokens=1000) -> str:
        """Same logic as cmd_needle — copy here so tests don't depend on CLI parsing."""
        filler = ("The quick brown fox jumps over the lazy dog. " * 120).strip()
        filler_block = (filler + " ") * (filler_tokens * 4 // len(filler) + 1)
        filler_block = filler_block[: filler_tokens * 4]

        tail_block = ("Paris is the capital of France. " * 80).strip()
        tail_block = (tail_block + " ") * (tail_tokens * 4 // len(tail_block) + 1)
        tail_block = tail_block[: tail_tokens * 4]

        return (
            f"{filler_block}\n\n"
            f"The secret code is: {needle}\n\n"
            f"{tail_block}\n\n"
            f"What is the secret code mentioned in the text above? "
            f"Reply with only the secret code, nothing else."
        )

    def test_prompt_contains_needle(self):
        needle = "NEEDLE-abc123deadbeef"
        prompt = self._build_needle_prompt(needle)
        self.assertIn(needle, prompt)

    def test_prompt_is_approximately_6k_tokens(self):
        needle = "NEEDLE-abc123deadbeef"
        prompt = self._build_needle_prompt(needle)
        token_estimate = len(prompt) // 4
        # Should be between 5k and 8k tokens
        self.assertGreater(token_estimate, 5000)
        self.assertLess(token_estimate, 8000)

    def test_needle_in_middle_not_at_start_or_end(self):
        needle = "NEEDLE-abc123deadbeef"
        prompt = self._build_needle_prompt(needle)
        idx = prompt.index(needle)
        # Needle must not be in first 10% or last 10% of prompt
        self.assertGreater(idx, len(prompt) * 0.1)
        self.assertLess(idx, len(prompt) * 0.9)

    def test_unique_needle_id(self):
        import uuid
        needle_id = uuid.uuid4().hex[:16]
        needle    = f"NEEDLE-{needle_id}"
        self.assertEqual(len(needle_id), 16)
        self.assertTrue(needle.startswith("NEEDLE-"))


class NeedleIntegrationTests(unittest.TestCase):
    """Integration: run against a fake Ollama that echoes the needle back."""

    def setUp(self):
        _NeedleOllamaHandler.last_body = {}
        self.server = HTTPServer(("127.0.0.1", 0), _NeedleOllamaHandler)
        port = self.server.server_address[1]
        self.ollama_url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ.pop("MYAI_NUM_CTX", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _build_prompt(self, needle: str) -> str:
        filler = ("The quick brown fox jumps over the lazy dog. " * 120).strip()
        filler_block = (filler + " ") * (5000 * 4 // len(filler) + 1)
        filler_block = filler_block[:5000 * 4]
        tail_block = ("Paris is the capital of France. " * 80).strip()
        tail_block = (tail_block + " ") * (1000 * 4 // len(tail_block) + 1)
        tail_block = tail_block[:1000 * 4]
        return (
            f"{filler_block}\n\n"
            f"The secret code is: {needle}\n\n"
            f"{tail_block}\n\n"
            f"What is the secret code mentioned in the text above? "
            f"Reply with only the secret code, nothing else."
        )

    def test_needle_found_in_response(self):
        import uuid
        needle = f"NEEDLE-{uuid.uuid4().hex[:16]}"
        prompt = self._build_prompt(needle)

        result, meta = run_ollama_full("llama3.2", prompt,
                                       ollama_url=self.ollama_url, timeout=10)
        self.assertIn(needle, result,
                      f"Needle '{needle}' not found in response '{result[:80]}'")

    def test_num_ctx_set_on_payload(self):
        """Fake server verifies that the request carries options.num_ctx >= 8192."""
        import uuid
        needle = f"NEEDLE-{uuid.uuid4().hex[:16]}"
        prompt = self._build_prompt(needle)

        run_ollama_full("llama3.2", prompt, ollama_url=self.ollama_url, timeout=10)

        body = _NeedleOllamaHandler.last_body
        opts = body.get("options", {})
        self.assertGreaterEqual(
            opts.get("num_ctx", 0), 8192,
            "num_ctx must be >= 8192 for 6k-token needle prompts"
        )

    def test_token_counts_reported(self):
        import uuid
        needle = f"NEEDLE-{uuid.uuid4().hex[:16]}"
        prompt = self._build_prompt(needle)
        _, meta = run_ollama_full("llama3.2", prompt,
                                  ollama_url=self.ollama_url, timeout=10)
        self.assertIn("tokens_in", meta)
        self.assertIn("tokens_out", meta)
        self.assertGreater(meta["tokens_in"], 0)


if __name__ == "__main__":
    unittest.main()
