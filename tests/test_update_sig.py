"""Tests for secure auto-update signature verification (T10769).

Covers:
  * unsigned artifact rejected (no .sig / wrong-length sig)
  * tampered artifact rejected (valid sig for different content)
  * valid signed artifact accepted and atomically replaced
  * backup created and rollback restores previous file
  * downgrade refused by default; allowed when MYAI_ALLOW_DOWNGRADE=1
  * integration tests call the REAL check_for_update() via allow_insecure_http

Pure stdlib + cryptography (soft dep already used by attestation).
Run with: MYAI_ADMIN_SECRET=ci-stub-not-real python -m pytest tests/test_update_sig.py
"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

# src-layout: importable without install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent import update  # noqa: E402
from myai_agent.update import (  # noqa: E402
    _atomic_replace,
    _version_tuple,
    check_for_update,
    rollback,
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
        bak = path + ".bak"
        try:
            _atomic_replace(path, b"new content")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"new content")
        finally:
            os.unlink(path)
            if os.path.exists(bak):
                os.unlink(bak)

    def test_atomic_replace_no_tmp_left_on_success(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"original")
            path = f.name
        directory = os.path.dirname(path)
        bak = path + ".bak"
        before = set(os.listdir(directory))
        try:
            _atomic_replace(path, b"updated")
            after = set(os.listdir(directory))
            new_files = after - before
            # The only allowed new file is the .bak backup; no stray .tmp files.
            expected_new = {os.path.basename(bak)}
            stray = new_files - expected_new
            self.assertEqual(stray, set(), f"stray temp files left: {stray}")
        finally:
            os.unlink(path)
            if os.path.exists(bak):
                os.unlink(bak)

    def test_atomic_replace_creates_backup(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"original content")
            path = f.name
        bak = path + ".bak"
        try:
            _atomic_replace(path, b"new content")
            self.assertTrue(os.path.exists(bak), ".bak file must be created")
            with open(bak, "rb") as fh:
                self.assertEqual(fh.read(), b"original content",
                                 ".bak must contain the previous file contents")
        finally:
            os.unlink(path)
            if os.path.exists(bak):
                os.unlink(bak)

    def test_rollback_restores_original(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"original content")
            path = f.name
        bak = path + ".bak"
        try:
            _atomic_replace(path, b"new content")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"new content")
            result = rollback(path)
            self.assertTrue(result, "rollback() must return True on success")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"original content",
                                 "rollback must restore original content")
            self.assertFalse(os.path.exists(bak), ".bak must be removed after rollback")
        finally:
            os.unlink(path)
            if os.path.exists(bak):
                os.unlink(bak)

    def test_rollback_returns_false_without_backup(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"content")
            path = f.name
        try:
            result = rollback(path)
            self.assertFalse(result, "rollback() must return False when no .bak exists")
        finally:
            os.unlink(path)


class VersionTupleTests(unittest.TestCase):

    def test_newer_version_is_greater(self):
        self.assertGreater(_version_tuple("2.4.0"), _version_tuple("2.3.0"))

    def test_same_version_equal(self):
        self.assertEqual(_version_tuple("2.3.0"), _version_tuple("2.3.0"))

    def test_malformed_version_returns_zeros(self):
        self.assertEqual(_version_tuple("not-a-version"), (0, 0, 0))


def _make_handler(version, artifact_bytes, sig_bytes):
    """Return an HTTPRequestHandler class serving fixed version/artifact/sig."""
    import json

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            port = self.server.server_address[1]
            if self.path == "/api/v1/agents/version":
                body = json.dumps({
                    "version": version,
                    "url": f"http://127.0.0.1:{port}/dist/myai-agent.py",
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

    return Handler


class CheckForUpdateIntegrationTests(unittest.TestCase):
    """Full check_for_update() flow using a local HTTP test server.

    Uses the REAL check_for_update() via allow_insecure_http=True, which is
    honoured only when MYAI_UPDATE_INSECURE_HTTP=1 AND the host is 127.0.0.1.
    """

    def setUp(self):
        self.priv, self.pub_hex = _gen_keypair()
        self.artifact = b"#!/usr/bin/env python3\n# v2.4.0\n"
        self.sig = _sign(self.priv, self.artifact)

    def _run(self, current_version="2.3.0", tamper_artifact=False, drop_sig=False,
             wrong_key=False, serve_version="2.4.0", allow_downgrade=False):
        """Spin up a test HTTP server, call the real check_for_update(), return
        (updated: bool, dest_content: bytes)."""
        artifact_bytes = self.artifact + b"# tampered\n" if tamper_artifact else self.artifact
        sig_bytes = b"" if drop_sig else self.sig
        pub_hex = self.pub_hex if not wrong_key else _gen_keypair()[1]

        srv = HTTPServer(
            ("127.0.0.1", 0),
            _make_handler(serve_version, artifact_bytes, sig_bytes),
        )
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as fh:
            fh.write(b"old agent")
            dest = fh.name
        bak = dest + ".bak"

        env_patch = {"MYAI_UPDATE_INSECURE_HTTP": "1"}
        if allow_downgrade:
            env_patch["MYAI_ALLOW_DOWNGRADE"] = "1"
        saved_env = {}
        for k, v in env_patch.items():
            saved_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Ensure downgrade flag is absent when not requested.
        if not allow_downgrade and "MYAI_ALLOW_DOWNGRADE" in os.environ:
            saved_env.setdefault("MYAI_ALLOW_DOWNGRADE", os.environ.pop("MYAI_ALLOW_DOWNGRADE"))

        try:
            updated = check_for_update(
                base, current_version,
                current_path=dest,
                pubkey_hex=pub_hex,
                allow_insecure_http=True,
            )
            with open(dest, "rb") as fh2:
                content = fh2.read()
        finally:
            for k, orig in saved_env.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)
            if os.path.exists(bak):
                os.unlink(bak)
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass

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

    def test_downgrade_refused_by_default(self):
        # serve 2.4.0 but agent is already at 2.5.0 — must refuse without env var
        updated, content = self._run(current_version="2.5.0", serve_version="2.4.0")
        self.assertFalse(updated, "downgrade must be refused when MYAI_ALLOW_DOWNGRADE is unset")
        self.assertEqual(content, b"old agent", "original file must be unchanged on refused downgrade")

    def test_downgrade_allowed_with_env_var(self):
        # same scenario but with MYAI_ALLOW_DOWNGRADE=1 — must succeed
        updated, content = self._run(
            current_version="2.5.0", serve_version="2.4.0", allow_downgrade=True
        )
        self.assertTrue(updated, "downgrade must be allowed when MYAI_ALLOW_DOWNGRADE=1")
        self.assertEqual(content, self.artifact, "downgraded artifact must be written verbatim")

    def test_insecure_http_refused_without_env_var(self):
        # allow_insecure_http=True but MYAI_UPDATE_INSECURE_HTTP not set — must refuse
        import json

        srv = HTTPServer(
            ("127.0.0.1", 0),
            _make_handler("2.4.0", self.artifact, self.sig),
        )
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as fh:
            fh.write(b"old agent")
            dest = fh.name
        bak = dest + ".bak"

        saved = os.environ.pop("MYAI_UPDATE_INSECURE_HTTP", None)
        try:
            updated = check_for_update(
                base, "2.3.0",
                current_path=dest,
                pubkey_hex=self.pub_hex,
                allow_insecure_http=True,  # env var absent → still refused
            )
        finally:
            if saved is not None:
                os.environ["MYAI_UPDATE_INSECURE_HTTP"] = saved
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)
            if os.path.exists(bak):
                os.unlink(bak)
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass

        self.assertFalse(updated, "HTTP must be refused when MYAI_UPDATE_INSECURE_HTTP is not set")


if __name__ == "__main__":
    unittest.main()
