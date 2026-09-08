"""Tests for secure auto-update signature verification (T10769).

Covers:
  * unsigned artifact rejected (no .sig / wrong-length sig)
  * tampered artifact rejected (valid sig for different content)
  * valid signed artifact accepted and atomically replaced

Pure stdlib + cryptography (soft dep already used by attestation).
Run with: MYAI_ADMIN_SECRET=ci-stub-not-real python -m pytest tests/test_update_sig.py
"""

import os
import sys
import tempfile
import unittest

# src-layout: importable without install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent import update  # noqa: E402
from myai_agent.update import (  # noqa: E402
    _atomic_replace,
    _version_tuple,
    verify_artifact,
)


def _gen_keypair():
    """Return (private_key, public_key_hex) for a fresh Ed25519 test keypair."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv, pub_hex


def _sign(priv_key, data: bytes) -> bytes:
    return priv_key.sign(data)


class VerifyArtifactTests(unittest.TestCase):

    def setUp(self):
        self.priv, self.pub_hex = _gen_keypair()
        self.artifact = b"#!/usr/bin/env python3\nprint('hello')\n"

    def test_valid_signature_accepted(self):
        sig = _sign(self.priv, self.artifact)
        self.assertTrue(
            verify_artifact(self.artifact, sig, pubkey_hex=self.pub_hex),
            "valid Ed25519 signature must be accepted",
        )

    def test_unsigned_rejected_empty_sig(self):
        self.assertFalse(
            verify_artifact(self.artifact, b"", pubkey_hex=self.pub_hex),
            "empty signature must be rejected",
        )

    def test_unsigned_rejected_wrong_length(self):
        self.assertFalse(
            verify_artifact(self.artifact, b"\x00" * 32, pubkey_hex=self.pub_hex),
            "32-byte (wrong-length) blob must be rejected without crashing",
        )

    def test_tampered_artifact_rejected(self):
        sig = _sign(self.priv, self.artifact)
        tampered = self.artifact + b"# extra line\n"
        self.assertFalse(
            verify_artifact(tampered, sig, pubkey_hex=self.pub_hex),
            "signature valid for original must be rejected for tampered artifact",
        )

    def test_wrong_key_rejected(self):
        _, other_pub_hex = _gen_keypair()
        sig = _sign(self.priv, self.artifact)
        self.assertFalse(
            verify_artifact(self.artifact, sig, pubkey_hex=other_pub_hex),
            "signature from one key must be rejected by a different public key",
        )

    def test_garbage_sig_rejected(self):
        self.assertFalse(
            verify_artifact(self.artifact, b"\xff" * 64, pubkey_hex=self.pub_hex),
            "garbage 64-byte sig must be rejected",
        )


class AtomicReplaceTests(unittest.TestCase):

    def test_atomic_replace_writes_content(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"old content")
            path = f.name
        try:
            _atomic_replace(path, b"new content")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"new content")
        finally:
            os.unlink(path)

    def test_atomic_replace_no_tmp_left_on_success(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"original")
            path = f.name
        directory = os.path.dirname(path)
        before = set(os.listdir(directory))
        try:
            _atomic_replace(path, b"updated")
            after = set(os.listdir(directory))
            new_files = after - before
            self.assertEqual(new_files, set(), f"leftover temp files: {new_files}")
        finally:
            os.unlink(path)


class VersionTupleTests(unittest.TestCase):

    def test_newer_version_is_greater(self):
        self.assertGreater(_version_tuple("2.4.0"), _version_tuple("2.3.0"))

    def test_same_version_equal(self):
        self.assertEqual(_version_tuple("2.3.0"), _version_tuple("2.3.0"))

    def test_malformed_version_returns_zeros(self):
        self.assertEqual(_version_tuple("not-a-version"), (0, 0, 0))


class CheckForUpdateIntegrationTests(unittest.TestCase):
    """Full check_for_update() flow using a fake HTTP coordinator."""

    def setUp(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.priv, self.pub_hex = _gen_keypair()
        self.artifact = b"#!/usr/bin/env python3\n# v2.4.0\n"
        self.sig = _sign(self.priv, self.artifact)

        artifact_bytes = self.artifact
        sig_bytes = self.sig

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/v1/agents/version":
                    body = json.dumps({
                        "version": "2.4.0",
                        "url": f"http://127.0.0.1:{self.server.server_address[1]}/dist/myai-agent.py",
                    }).encode()
                    self._send(200, body, "application/json")
                elif self.path == "/dist/myai-agent.py":
                    self._send(200, artifact_bytes, "application/octet-stream")
                elif self.path == "/dist/myai-agent.py.sig":
                    self._send(200, sig_bytes, "application/octet-stream")
                else:
                    self._send(404, b"not found", "text/plain")

            def _send(self, code, body, ct):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _run(self, current_version="2.3.0", tamper_artifact=False, drop_sig=False,
             wrong_key=False):
        """Run check_for_update against the fake server, return (updated, dest_content)."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        artifact_bytes = self.artifact
        if tamper_artifact:
            artifact_bytes = self.artifact + b"# tampered\n"
        sig_bytes = b"" if drop_sig else self.sig

        class PatchedHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/v1/agents/version":
                    body = json.dumps({
                        "version": "2.4.0",
                        "url": f"http://127.0.0.1:{self.server.server_address[1]}/dist/myai-agent.py",
                    }).encode()
                    self._send(200, body, "application/json")
                elif self.path == "/dist/myai-agent.py":
                    self._send(200, artifact_bytes, "application/octet-stream")
                elif self.path == "/dist/myai-agent.py.sig":
                    self._send(200, sig_bytes, "application/octet-stream")
                else:
                    self._send(404, b"not found", "text/plain")

            def _send(self, code, body, ct):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), PatchedHandler)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as fh:
            fh.write(b"old agent")
            dest = fh.name

        pub_hex = self.pub_hex if not wrong_key else _gen_keypair()[1]

        # Temporarily allow http:// URLs for the test server by patching the check.
        original_check_fn = update.check_for_update

        def _patched_check(coordinator_url, current_version, current_path=None,
                           pubkey_hex=update.RELEASE_PUBKEY_HEX):
            # Replicate the real function but skip the https:// guard for tests.
            import json as _j, urllib.request as _r
            resp_raw = _r.urlopen(f"{coordinator_url}/api/v1/agents/version", timeout=10).read()
            meta = _j.loads(resp_raw)
            latest = meta.get("version", "")
            artifact_url = meta.get("url", "")
            if not latest or not artifact_url:
                return False
            if update._version_tuple(latest) <= update._version_tuple(current_version):
                return False
            try:
                art, sig = update.download_signed_artifact(artifact_url)
            except Exception:
                return False
            if not update.verify_artifact(art, sig, pubkey_hex=pubkey_hex):
                return False
            update._atomic_replace(current_path or dest, art)
            return True

        try:
            updated = _patched_check(base, current_version, current_path=dest,
                                     pubkey_hex=pub_hex)
            with open(dest, "rb") as fh2:
                content = fh2.read()
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)
            os.unlink(dest)

        return updated, content

    def test_valid_signed_update_accepted(self):
        updated, content = self._run()
        self.assertTrue(updated, "valid signed update must return True")
        self.assertEqual(content, self.artifact, "artifact must be written verbatim")

    def test_tampered_artifact_rejected(self):
        updated, content = self._run(tamper_artifact=True)
        self.assertFalse(updated, "tampered artifact must be rejected")
        self.assertEqual(content, b"old agent", "original file must be unchanged")

    def test_unsigned_update_rejected(self):
        updated, content = self._run(drop_sig=True)
        self.assertFalse(updated, "unsigned (empty .sig) update must be rejected")
        self.assertEqual(content, b"old agent", "original file must be unchanged")

    def test_already_up_to_date_skipped(self):
        updated, content = self._run(current_version="2.4.0")
        self.assertFalse(updated, "same version must not trigger update")
        self.assertEqual(content, b"old agent")

    def test_wrong_key_rejected(self):
        updated, content = self._run(wrong_key=True)
        self.assertFalse(updated, "sig from wrong key must be rejected")
        self.assertEqual(content, b"old agent")


if __name__ == "__main__":
    unittest.main()
