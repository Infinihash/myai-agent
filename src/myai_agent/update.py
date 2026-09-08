"""
Secure auto-update for myai-agent (T10769).

Downloads a new agent artifact from the coordinator-supplied URL, verifies a
detached Ed25519 signature against the pinned release public key, and atomically
replaces the running script. Refuses to write or exec anything unsigned or tampered.

Re-enabling this in legacy/myai-agent.py is intentionally deferred until callers
import and invoke check_for_update() from this module rather than the retired stub.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger("myai_agent.update")

# Pinned Ed25519 release public key (raw 32 bytes, hex-encoded).
# Same key used to sign release artifacts and shards.
# Rotation: update this constant, ship a new signed release, and bump the
# minimum-accepted version in the coordinator to drop old unsigned agents.
RELEASE_PUBKEY_HEX = (
    "7c53d5b5b96879bbcc637b31023c44f19e23d5d492e027cdff9095e34f2114e1"
)

# Maximum artifact size accepted (10 MiB). Prevents memory exhaustion from a
# malicious Content-Length or a redirect to a huge file.
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

# Ed25519 signatures are exactly 64 bytes.
_SIG_BYTES = 64


def _load_verify_key(pubkey_hex: str):
    """Return an Ed25519PublicKey from hex-encoded raw bytes. Raises on failure."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))


def verify_artifact(artifact: bytes, sig: bytes, pubkey_hex: str = RELEASE_PUBKEY_HEX) -> bool:
    """Return True iff sig is a valid Ed25519 signature over artifact for pubkey_hex."""
    if len(sig) != _SIG_BYTES:
        log.warning("update: signature is %d bytes, expected %d — rejected", len(sig), _SIG_BYTES)
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        key = _load_verify_key(pubkey_hex)
        key.verify(sig, artifact)
        return True
    except ImportError:
        log.error("update: `cryptography` package unavailable — cannot verify signature; refusing update")
        return False
    except InvalidSignature:
        log.warning("update: Ed25519 signature INVALID — artifact rejected")
        return False
    except Exception as exc:
        log.error("update: signature verification error: %s — rejected", exc)
        return False


def _fetch_bytes(url: str, max_bytes: int = _MAX_ARTIFACT_BYTES) -> bytes:
    """Download url, returning raw bytes. Raises urllib.error.URLError on failure."""
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"artifact exceeds {max_bytes} bytes limit")
    return data


def download_signed_artifact(artifact_url: str) -> Tuple[bytes, bytes]:
    """Download artifact and its detached signature (.sig).

    Returns (artifact_bytes, signature_bytes). Raises on network or size errors.
    The signature URL is artifact_url + '.sig'.
    """
    sig_url = artifact_url + ".sig"
    log.debug("update: fetching artifact %s", artifact_url)
    artifact = _fetch_bytes(artifact_url)
    log.debug("update: fetching signature %s", sig_url)
    sig = _fetch_bytes(sig_url, max_bytes=_SIG_BYTES)
    return artifact, sig


def _atomic_replace(dest_path: str, content: bytes) -> None:
    """Write content to dest_path atomically via a same-directory tempfile+rename.

    Saves the previous file as <dest_path>.bak (fsync'd) before replacing so
    that rollback() can restore it if the new version is broken.
    """
    dest_abs = os.path.abspath(dest_path)
    dest_dir = os.path.dirname(dest_abs)
    bak_path = dest_abs + ".bak"

    if os.path.exists(dest_abs):
        fd_bak, tmp_bak = tempfile.mkstemp(dir=dest_dir, suffix=".bak.tmp")
        try:
            with os.fdopen(fd_bak, "wb") as bak_fh:
                with open(dest_abs, "rb") as src:
                    bak_fh.write(src.read())
                bak_fh.flush()
                os.fsync(bak_fh.fileno())
            os.replace(tmp_bak, bak_path)
        except Exception:
            try:
                os.unlink(tmp_bak)
            except OSError:
                pass
            raise

    fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        if os.path.exists(dest_abs):
            shutil.copystat(dest_abs, tmp)
        os.replace(tmp, dest_abs)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rollback(path: str) -> bool:
    """Restore path from <path>.bak if it exists. Returns True on success."""
    path_abs = os.path.abspath(path)
    bak_path = path_abs + ".bak"
    if not os.path.exists(bak_path):
        log.warning("rollback: no backup at %s", bak_path)
        return False
    try:
        os.replace(bak_path, path_abs)
        log.info("rollback: restored %s from backup", path_abs)
        return True
    except Exception as exc:
        log.error("rollback: failed to restore %s: %s", path_abs, exc)
        return False


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return (0, 0, 0)


def check_for_update(
    coordinator_url: str,
    current_version: str,
    current_path: Optional[str] = None,
    pubkey_hex: str = RELEASE_PUBKEY_HEX,
    allow_insecure_http: bool = False,
) -> bool:
    """Check the coordinator for a newer agent version and apply it if signed.

    Returns True if the agent was updated (caller should exec the new binary).
    Returns False if already up-to-date, the new version is unsigned/tampered,
    or any network/verification error occurs (fail-closed).

    allow_insecure_http: test-only escape hatch; only honoured when
    MYAI_UPDATE_INSECURE_HTTP=1 is set AND the artifact host is 127.0.0.1.
    """
    try:
        import json as _json
        import urllib.request as _req
        resp_raw = _req.urlopen(
            f"{coordinator_url}/api/v1/agents/version", timeout=10
        ).read()
        meta = _json.loads(resp_raw)
    except Exception as exc:
        log.warning("update: version check failed: %s", exc)
        return False

    latest = meta.get("version", "")
    url = meta.get("url", "")
    if not latest or not url:
        log.debug("update: no version/url in coordinator response")
        return False

    latest_t = _version_tuple(latest)
    current_t = _version_tuple(current_version)

    if latest_t == current_t:
        log.debug("update: already at v%s", current_version)
        return False

    if latest_t < current_t:
        if os.environ.get("MYAI_ALLOW_DOWNGRADE") != "1":
            log.warning(
                "update: v%s is older than current v%s — downgrade refused "
                "(set MYAI_ALLOW_DOWNGRADE=1 to allow)",
                latest, current_version,
            )
            return False
        log.warning(
            "update: MYAI_ALLOW_DOWNGRADE=1 — allowing downgrade from v%s to v%s",
            current_version, latest,
        )

    parsed_url = urlparse(url)
    _insecure_ok = (
        allow_insecure_http
        and os.environ.get("MYAI_UPDATE_INSECURE_HTTP") == "1"
        and parsed_url.hostname == "127.0.0.1"
        and parsed_url.scheme == "http"
    )
    if not url.startswith("https://") and not _insecure_ok:
        log.error("update: artifact URL must be HTTPS, got %r — refusing", url[:64])
        return False

    log.info("update: new version %s available (current %s), verifying signature…",
             latest, current_version)
    try:
        artifact, sig = download_signed_artifact(url)
    except Exception as exc:
        log.warning("update: download failed: %s", exc)
        return False

    if not verify_artifact(artifact, sig, pubkey_hex=pubkey_hex):
        log.error("update: REFUSING unsigned/invalid artifact for v%s", latest)
        return False

    target = current_path or os.path.abspath(sys.argv[0])
    try:
        _atomic_replace(target, artifact)
    except Exception as exc:
        log.error("update: atomic replace failed: %s", exc)
        return False

    log.info("update: applied v%s successfully — restart required", latest)
    return True
