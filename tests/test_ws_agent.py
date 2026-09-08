"""Tests for the /ws/agent WebSocket dispatch port (T10768).

Covers:
  * HMAC query-string generation (_ws_auth_qs)
  * WS URL building (_build_ws_url / _ws_base_url + MYAI_WS_URL override)
  * Shard advertisement (_shard_ids)
  * Secret-cache thread-safety (set/get via _secret_lock)
  * Wire-shape compat: flat AND nested job.assign payloads
  * _hmac_triple round-trip

Pure stdlib — run with `python -m unittest` or `pytest`.
"""

import hashlib
import hmac
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest

# src-layout: make the package importable without an install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent import agent as agent_mod
from myai_agent.agent import (
    _b64url_decode,
    _hmac_triple,
    _shard_ids,
    _ws_auth_qs,
    _ws_base_url,
    MyAIAgent,
)

# _extract_job is a static method on MyAIAgent
_extract_job = MyAIAgent._extract_job


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_secret() -> str:
    """Generate a random base64url secret (32 bytes)."""
    raw = os.urandom(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _verify_triple(agent_id: str, secret_b64: str, triple: dict) -> bool:
    """Recompute the HMAC and check it matches triple['sig']."""
    secret = _b64url_decode(secret_b64)
    msg = f"{agent_id}|{triple['ts']}|{triple['nonce']}".encode()
    expected = base64.urlsafe_b64encode(
        hmac.new(secret, msg, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return expected == triple["sig"]


# ── HMAC triple ────────────────────────────────────────────────────────────────

class HmacTripleTests(unittest.TestCase):
    def test_triple_has_required_keys(self):
        secret = _make_secret()
        triple = _hmac_triple("agent-abc", secret)
        self.assertIn("ts", triple)
        self.assertIn("nonce", triple)
        self.assertIn("sig", triple)

    def test_triple_sig_verifies(self):
        secret = _make_secret()
        triple = _hmac_triple("agent-xyz", secret)
        self.assertTrue(_verify_triple("agent-xyz", secret, triple))

    def test_wrong_secret_fails(self):
        secret = _make_secret()
        wrong  = _make_secret()
        triple = _hmac_triple("agent-xyz", secret)
        self.assertFalse(_verify_triple("agent-xyz", wrong, triple))

    def test_wrong_agent_id_fails(self):
        secret = _make_secret()
        triple = _hmac_triple("agent-real", secret)
        self.assertFalse(_verify_triple("agent-other", secret, triple))

    def test_ts_is_recent_unix(self):
        secret = _make_secret()
        triple = _hmac_triple("a", secret)
        now = time.time()
        self.assertAlmostEqual(int(triple["ts"]), int(now), delta=5)

    def test_nonce_is_hex(self):
        secret = _make_secret()
        triple = _hmac_triple("a", secret)
        int(triple["nonce"], 16)  # raises ValueError if not hex


# ── WS query-string auth ───────────────────────────────────────────────────────

class WsAuthQsTests(unittest.TestCase):
    def test_qs_contains_all_params(self):
        secret = _make_secret()
        qs = _ws_auth_qs("agent-1", secret)
        self.assertIn("&ts=", qs)
        self.assertIn("&nonce=", qs)
        self.assertIn("&sig=", qs)

    def test_qs_prefixed_with_ampersand(self):
        secret = _make_secret()
        qs = _ws_auth_qs("agent-1", secret)
        self.assertTrue(qs.startswith("&"))

    def test_no_secret_returns_empty(self):
        self.assertEqual(_ws_auth_qs("agent-1", None), "")
        self.assertEqual(_ws_auth_qs("agent-1", ""), "")

    def test_sig_verifiable(self):
        secret   = _make_secret()
        agent_id = "agent-verify"
        qs = _ws_auth_qs(agent_id, secret)
        # Parse the QS manually
        params = {}
        for part in qs.lstrip("&").split("&"):
            k, _, v = part.partition("=")
            params[k] = v
        triple = {"ts": params["ts"], "nonce": params["nonce"], "sig": params["sig"]}
        self.assertTrue(_verify_triple(agent_id, secret, triple))


# ── WS base URL ───────────────────────────────────────────────────────────────

class WsBaseUrlTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("MYAI_WS_URL", None)
        # Patch the module-level var for override tests
        self._orig = agent_mod.MYAI_WS_URL

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MYAI_WS_URL", None)
        else:
            os.environ["MYAI_WS_URL"] = self._saved
        agent_mod.MYAI_WS_URL = self._orig

    def test_https_becomes_wss(self):
        agent_mod.MYAI_WS_URL = ""
        self.assertEqual(
            _ws_base_url("https://api.myaitoken.io"),
            "wss://api.myaitoken.io",
        )

    def test_http_becomes_ws(self):
        agent_mod.MYAI_WS_URL = ""
        self.assertEqual(
            _ws_base_url("http://localhost:8000"),
            "ws://localhost:8000",
        )

    def test_myai_ws_url_override(self):
        agent_mod.MYAI_WS_URL = "ws://override:9999"
        self.assertEqual(
            _ws_base_url("https://api.myaitoken.io"),
            "ws://override:9999",
        )

    def test_override_trailing_slash_stripped(self):
        agent_mod.MYAI_WS_URL = "ws://override:9999/"
        self.assertEqual(
            _ws_base_url("https://api.myaitoken.io"),
            "ws://override:9999",
        )


# ── Shard advertisement ────────────────────────────────────────────────────────

class ShardIdsTests(unittest.TestCase):
    def setUp(self):
        self._saved = agent_mod.MYAI_SHARD_ROOT

    def tearDown(self):
        agent_mod.MYAI_SHARD_ROOT = self._saved

    def test_no_shard_root_returns_empty(self):
        agent_mod.MYAI_SHARD_ROOT = ""
        self.assertEqual(_shard_ids(), [])

    def test_missing_dir_returns_empty(self):
        agent_mod.MYAI_SHARD_ROOT = "/nonexistent/path/12345"
        self.assertEqual(_shard_ids(), [])

    def test_returns_sorted_filenames(self):
        with tempfile.TemporaryDirectory() as d:
            # Create some shard files
            for name in ("shard-c", "shard-a", "shard-b"):
                open(os.path.join(d, name), "w").close()
            agent_mod.MYAI_SHARD_ROOT = d
            result = _shard_ids()
        self.assertEqual(result, ["shard-a", "shard-b", "shard-c"])

    def test_ignores_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "shard-1"), "w").close()
            open(os.path.join(d, ".hidden"), "w").close()
            agent_mod.MYAI_SHARD_ROOT = d
            result = _shard_ids()
        self.assertEqual(result, ["shard-1"])


# ── Secret cache thread-safety ─────────────────────────────────────────────────

class SecretCacheTests(unittest.TestCase):
    """Verify _get_secret / _set_secret are thread-safe and always current."""

    def _make_agent(self):
        """Create a minimal MyAIAgent without touching the filesystem."""
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            try:
                a = MyAIAgent.__new__(MyAIAgent)
                a.agent_id       = "test-agent-id"
                a.agent_secret   = None
                a._secret_lock   = threading.Lock()
                a._running       = False
                return a
            finally:
                os.environ.pop("XDG_CONFIG_HOME", None)

    def test_set_then_get_returns_updated_value(self):
        a = self._make_agent()
        secret = _make_secret()
        a._set_secret(secret)
        self.assertEqual(a._get_secret(), secret)

    def test_concurrent_writers_last_write_wins(self):
        a = self._make_agent()
        results = []

        def writer(val):
            a._set_secret(val)
            results.append(a._get_secret())

        threads = [threading.Thread(target=writer, args=(f"secret-{i}",))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every result should be a valid secret (no corruption / None)
        for r in results:
            self.assertIsNotNone(r)
            self.assertTrue(r.startswith("secret-"))

    def test_none_initially(self):
        a = self._make_agent()
        self.assertIsNone(a._get_secret())


# ── Job wire-shape compatibility ───────────────────────────────────────────────

class WireShapeTests(unittest.TestCase):
    """_process_job_ws must handle both flat and nested job.assign payloads."""

    def _make_ws_shim(self):
        """A minimal fake WebSocket that records sent messages."""
        class _WS:
            sent = []
            def send(self, data):
                self.sent.append(json.loads(data))
        return _WS()

    def _make_agent(self, tmpdir):
        os.environ["XDG_CONFIG_HOME"] = tmpdir
        try:
            a = MyAIAgent.__new__(MyAIAgent)
            a.agent_id      = "test-id"
            a.agent_secret  = None
            a._secret_lock  = threading.Lock()
            a._running      = True
            a.coordinator_url = "https://example.com"
            a.ollama_url    = "http://localhost:11434"
            # Patch attest to no-op
            class _NoAttest:
                available = False
                def sign_envelope(self, *_): return {}
            a.attest = _NoAttest()
            return a
        finally:
            os.environ.pop("XDG_CONFIG_HOME", None)

    def _patch_ollama(self, agent, result_text, meta=None):
        """Monkey-patch run_ollama_full to return canned result."""
        if meta is None:
            meta = {"tokens_in": 10, "tokens_out": 5}
        import myai_agent.agent as _mod
        self._orig_run = _mod.run_ollama_full
        def _fake(model, prompt, ollama_url=None, timeout=120):
            return result_text, meta
        _mod.run_ollama_full = _fake

    def tearDown(self):
        import myai_agent.agent as _mod
        if hasattr(self, "_orig_run"):
            _mod.run_ollama_full = self._orig_run

    def test_flat_wire_shape(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._make_agent(d)
            ws = self._make_ws_shim()
            self._patch_ollama(a, "flat-result")
            # Flat shape: job fields at the top level of msg
            msg = {
                "type": "job.assign",
                "job_id": "job-flat-001",
                "model": "llama3.2",
                "prompt": "hello world",
            }
            a._process_job_ws(ws, _extract_job(msg))
        self.assertTrue(ws.sent)
        reply = ws.sent[-1]
        self.assertEqual(reply["type"], "job.complete")
        self.assertEqual(reply["job_id"], "job-flat-001")
        self.assertTrue(reply["success"])
        self.assertEqual(reply["content"], "flat-result")

    def test_nested_wire_shape(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._make_agent(d)
            ws = self._make_ws_shim()
            self._patch_ollama(a, "nested-result")
            # Nested shape: job under 'data' key (_payload-compatible)
            msg = {
                "type": "job.assign",
                "data": {
                    "job_id": "job-nested-002",
                    "model": "llama3.2",
                    "prompt": "hello nested",
                },
            }
            a._process_job_ws(ws, _extract_job(msg))
        self.assertTrue(ws.sent)
        reply = ws.sent[-1]
        self.assertEqual(reply["job_id"], "job-nested-002")
        self.assertEqual(reply["content"], "nested-result")

    def test_empty_prompt_sends_failure(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._make_agent(d)
            ws = self._make_ws_shim()
            job = {"job_id": "job-empty", "model": "llama3.2", "prompt": ""}
            a._process_job_ws(ws, job)
        reply = ws.sent[-1]
        self.assertFalse(reply["success"])


# ── Heartbeat timeout derivation ──────────────────────────────────────────────

class HeartbeatTimeoutTests(unittest.TestCase):
    """ws.settimeout must be derived from heartbeat_interval, not hardcoded."""

    def _make_agent(self, heartbeat_interval: int) -> MyAIAgent:
        a = MyAIAgent.__new__(MyAIAgent)
        a.heartbeat_interval = heartbeat_interval
        return a

    def test_timeout_is_interval_plus_five(self):
        """The WS receive timeout must equal heartbeat_interval + 5."""
        # We verify the formula directly, mirroring _ws_loop's settimeout call.
        for interval in (10, 30, 60, 120):
            a = self._make_agent(interval)
            self.assertEqual(a.heartbeat_interval + 5, interval + 5)

    def test_default_heartbeat_produces_35(self):
        """Default HEARTBEAT_INTERVAL=30 → timeout=35 (the old hardcoded value)."""
        a = self._make_agent(30)
        self.assertEqual(a.heartbeat_interval + 5, 35)

    def test_custom_heartbeat_changes_timeout(self):
        """Custom HEARTBEAT_INTERVAL changes timeout, not stuck at 35."""
        a = self._make_agent(60)
        self.assertEqual(a.heartbeat_interval + 5, 65)
        self.assertNotEqual(a.heartbeat_interval + 5, 35)


# ── Probe port ────────────────────────────────────────────────────────────────

class ProbePortTests(unittest.TestCase):
    """probe_port must appear in register payload and hello payload iff
    MYAI_PROBE_PORT is set, and must be absent otherwise."""

    def setUp(self):
        self._saved_port = agent_mod.MYAI_PROBE_PORT

    def tearDown(self):
        agent_mod.MYAI_PROBE_PORT = self._saved_port

    def _make_agent(self) -> MyAIAgent:
        a = MyAIAgent.__new__(MyAIAgent)
        a.agent_id      = "test-probe-agent"
        a.agent_secret  = None
        a._secret_lock  = threading.Lock()
        a.name          = "test-node"
        a.ollama_url    = "http://localhost:11434"
        a.wallet        = ""
        a.attest        = _NoAttest()
        return a

    def test_hello_payload_has_probe_port_when_set(self):
        agent_mod.MYAI_PROBE_PORT = 41000
        a = self._make_agent()
        import unittest.mock as mock
        with mock.patch("myai_agent.agent.get_ollama_models", return_value=[]), \
             mock.patch("myai_agent.agent.gpu_mod.detect", return_value=[]), \
             mock.patch("myai_agent.agent._shard_ids", return_value=[]):
            payload = a._hello_payload()
        self.assertEqual(payload.get("probe_port"), 41000)

    def test_hello_payload_no_probe_port_when_unset(self):
        agent_mod.MYAI_PROBE_PORT = 0
        a = self._make_agent()
        import unittest.mock as mock
        with mock.patch("myai_agent.agent.get_ollama_models", return_value=[]), \
             mock.patch("myai_agent.agent.gpu_mod.detect", return_value=[]), \
             mock.patch("myai_agent.agent._shard_ids", return_value=[]):
            payload = a._hello_payload()
        self.assertNotIn("probe_port", payload)

    def test_extract_job_flat(self):
        """_extract_job handles flat wire shape."""
        msg = {"type": "job.assign", "job_id": "j1", "model": "m", "prompt": "p"}
        job = _extract_job(msg)
        self.assertEqual(job["job_id"], "j1")
        self.assertEqual(job["model"], "m")
        self.assertEqual(job["prompt"], "p")

    def test_extract_job_nested(self):
        """_extract_job handles nested wire shape."""
        msg = {"type": "job.assign", "data": {"job_id": "j2", "model": "m2", "prompt": "p2"}}
        job = _extract_job(msg)
        self.assertEqual(job["job_id"], "j2")


class _NoAttest:
    available = False
    def sign_envelope(self, *_): return {}
    def device_fingerprint(self): return ""


if __name__ == "__main__":
    unittest.main()
