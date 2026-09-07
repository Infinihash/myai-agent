"""
MyAI GPU Agent — core logic.

Registers with the coordinator, polls for jobs, runs inference via Ollama,
reports results back. Zero external dependencies — pure stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import base64
import hashlib
import hmac
import secrets
import urllib.error
import urllib.request
import uuid
from typing import List, Dict, Any, Optional

from . import gpu as gpu_mod
from .attestation import Attestation
from .config import get_config_dir

log = logging.getLogger("myai_agent")

VERSION = "2.3.0"  # T10694: coordinator options envelope (num_ctx/format/keep_alive)


# ── Config ─────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


COORDINATOR_URL    = _env("COORDINATOR_URL",    "https://api.myaitoken.io")
OLLAMA_URL         = _env("OLLAMA_URL",         "http://localhost:11434")
AGENT_NAME         = _env("AGENT_NAME",         socket.gethostname())
AGENT_WALLET       = _env("AGENT_WALLET",       "")
POLL_INTERVAL      = int(_env("POLL_INTERVAL",  "5"))
HEARTBEAT_INTERVAL = int(_env("HEARTBEAT_INTERVAL", "30"))

# Comma-separated list of models to auto-pull on startup (if not already present).
# e.g. REQUIRED_MODELS=bonsai-8b:latest,deepseek-r1:7b
# Default includes bonsai-8b as the lightweight required model.
REQUIRED_MODELS_RAW = _env("REQUIRED_MODELS", "bonsai-8b:latest")
REQUIRED_MODELS: List[str] = [
    m.strip() for m in REQUIRED_MODELS_RAW.split(",") if m.strip()
]


# ── HTTP ───────────────────────────────────────────────────────────────────────

def http(method: str, url: str, body: dict = None, timeout: int = 30,
         extra_headers: dict = None) -> dict:
    """Minimal HTTP client — no external deps."""
    data = json.dumps(body).encode() if body else None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"myai-agent/{VERSION}",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning(f"HTTP {e.code} {method} {url}: {e.read().decode()[:200]}")
        return {}
    except Exception as e:
        log.debug(f"Request failed {method} {url}: {e}")
        return {}


# ── Agent ID ───────────────────────────────────────────────────────────────────

def _agent_id_path() -> str:
    config_dir = get_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "agent_id")


def load_agent_id() -> str:
    path = _agent_id_path()
    if os.path.exists(path):
        return open(path).read().strip()
    agent_id = str(uuid.uuid4())
    open(path, "w").write(agent_id)
    return agent_id


# ── Agent secret / HMAC auth ────────────────────────────────────────────────
# The coordinator issues a base64url agent_secret at /register. We persist it
# and sign agent-plane calls (jobs/pending, jobs/complete, heartbeat) with an
# HMAC triple so the coordinator can authenticate us as THIS agent. The
# signature exactly mirrors coordinator api/security/hmac_auth.sign():
#   base64url( HMAC_SHA256(raw_secret, f"{agent_id}|{ts}|{nonce}") )  (no pad)

def _agent_secret_path() -> str:
    config_dir = get_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "agent_secret")


def load_agent_secret() -> Optional[str]:
    """Base64url agent_secret issued at registration (None until issued)."""
    path = _agent_secret_path()
    if os.path.exists(path):
        return open(path).read().strip() or None
    return None


def save_agent_secret(secret_b64: str) -> None:
    try:
        path = _agent_secret_path()
        with open(path, "w") as f:
            f.write(secret_b64.strip())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        log.warning("could not persist agent_secret: %s", e)


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def agent_auth_headers(agent_id: str, secret_b64: Optional[str]) -> Dict[str, str]:
    """HMAC-triple headers for agent-plane calls; {} when no secret is held."""
    if not secret_b64:
        return {}
    try:
        secret = _b64url_decode(secret_b64)
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        msg = f"{agent_id}|{ts}|{nonce}".encode()
        sig = base64.urlsafe_b64encode(
            hmac.new(secret, msg, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        return {"X-Agent-Ts": ts, "X-Agent-Nonce": nonce, "X-Agent-Sig": sig}
    except Exception as e:
        log.debug("agent_auth_headers failed: %s", e)
        return {}


# ── Ollama ─────────────────────────────────────────────────────────────────────

def get_ollama_models(ollama_url: str = OLLAMA_URL) -> List[str]:
    try:
        resp = http("GET", f"{ollama_url}/api/tags", timeout=5)
        return [m["name"] for m in resp.get("models", [])]
    except Exception:
        return []


def pull_model(model: str, ollama_url: str = OLLAMA_URL) -> bool:
    """
    Pull a model via Ollama streaming pull API.
    Streams progress lines and returns True when 'success' status received.
    Falls back gracefully if Ollama is unreachable.
    """
    log.info(f"Pulling model: {model} ...")
    url = f"{ollama_url}/api/pull"
    data = json.dumps({"name": model, "stream": True}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            last_status = ""
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode().strip())
                    status = obj.get("status", "")
                    if status != last_status:
                        log.info(f"  [{model}] {status}")
                        last_status = status
                    if status == "success":
                        log.info(f"  ✓ {model} pulled successfully")
                        return True
                    # Error in stream
                    if "error" in obj:
                        log.error(f"  ✗ Pull error for {model}: {obj['error']}")
                        return False
                except json.JSONDecodeError:
                    continue
        # If we got here without 'success', treat as complete (older Ollama versions)
        log.info(f"  ✓ {model} pull stream ended")
        return True
    except urllib.error.HTTPError as e:
        log.error(f"  ✗ Pull HTTP {e.code} for {model}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        log.error(f"  ✗ Pull failed for {model}: {e}")
        return False


def ensure_models(required: List[str], ollama_url: str = OLLAMA_URL) -> None:
    """
    Check which required models are missing from Ollama and pull them.
    Skips models that are already present. Non-blocking on failure.
    """
    if not required:
        return

    existing = set(get_ollama_models(ollama_url))
    if not existing and not _ollama_reachable(ollama_url):
        log.warning("Ollama not reachable — skipping model pre-pull")
        return

    # Normalize: treat "model:latest" == "model" as equal
    def norm(m: str) -> str:
        return m if ":" in m else f"{m}:latest"

    existing_norm = {norm(m) for m in existing}

    for model in required:
        if norm(model) in existing_norm:
            log.info(f"  ✓ {model} already present — skip")
        else:
            log.info(f"  ↓ {model} not found — pulling now")
            pull_model(model, ollama_url)


def _ollama_reachable(ollama_url: str = OLLAMA_URL) -> bool:
    try:
        urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=5)
        return True
    except Exception:
        return False


# Sentinel used by the coordinator (api/routes/openai_compat.py) to smuggle
# Ollama-native options (format, options, keep_alive) through the existing
# messages pipeline without a DB schema change. The first message — if it's a
# system role with this prefix — is lifted out and its JSON payload is merged
# into the Ollama request body. Must stay byte-for-byte identical to the
# coordinator's MYAI_OPTS_SENTINEL and the reference agent
# (coordinator/static/agent/agent.py).
MYAI_OPTS_SENTINEL = "__MYAI_OPTS__:"


def _extract_opts_envelope(messages: list) -> tuple:
    """Detect and strip the __MYAI_OPTS__ envelope from the first message.

    Returns (cleaned_messages, extras_dict). extras_dict is empty if no
    envelope was present. Malformed JSON is tolerated (ignored).
    """
    if not messages:
        return messages, {}
    first = messages[0]
    content = first.get("content") if isinstance(first, dict) else None
    if (isinstance(first, dict)
            and first.get("role") == "system"
            and isinstance(content, str)
            and content.startswith(MYAI_OPTS_SENTINEL)):
        try:
            extras = json.loads(content[len(MYAI_OPTS_SENTINEL):])
            if not isinstance(extras, dict):
                extras = {}
        except Exception as e:
            log.warning(f"MYAI_OPTS envelope parse failed: {e}")
            extras = {}
        return messages[1:], extras
    return messages, {}


def _apply_opts_to_payload(payload: dict, extras: dict) -> dict:
    """Merge extras (format / options / keep_alive) into an Ollama payload."""
    if not extras:
        return payload
    if "format" in extras and extras["format"] is not None:
        payload["format"] = extras["format"]
    if "keep_alive" in extras and extras["keep_alive"] is not None:
        payload["keep_alive"] = extras["keep_alive"]
    if "options" in extras and isinstance(extras["options"], dict) and extras["options"]:
        payload["options"] = extras["options"]
    return payload


def _num_ctx_default() -> int:
    """num_ctx floor from MYAI_NUM_CTX (default 8192)."""
    try:
        return int(_env("MYAI_NUM_CTX", "8192"))
    except (TypeError, ValueError):
        return 8192


def _ensure_num_ctx(payload: dict) -> dict:
    """Belt-and-braces: guarantee num_ctx is set on the Ollama payload.

    Ollama defaults to a 4096-token context and truncates prompts silently
    from the front when they exceed it. An old coordinator that predates the
    options envelope sends no num_ctx, so without this the packaged agents
    would still truncate. Only fills in a default; never overrides a num_ctx
    the coordinator already sent via the envelope.
    """
    opts = payload.get("options")
    if not isinstance(opts, dict):
        opts = {}
    if "num_ctx" not in opts:
        opts["num_ctx"] = _num_ctx_default()
    payload["options"] = opts
    return payload


def _usage_from_response(resp: dict) -> Dict[str, int]:
    """Extract Ollama token counts and map them to the coordinator's field
    names. Ollama reports prompt_eval_count (input) and eval_count (output);
    we forward both the raw names and tokens_in/tokens_out so the coordinator
    earnings path (_reported_tokens / _provider_usage) sees them either way."""
    meta: Dict[str, int] = {}
    if not isinstance(resp, dict):
        return meta
    pin = resp.get("prompt_eval_count")
    pout = resp.get("eval_count")
    if isinstance(pin, int):
        meta["tokens_in"] = pin
        meta["prompt_eval_count"] = pin
    if isinstance(pout, int):
        meta["tokens_out"] = pout
        meta["eval_count"] = pout
    return meta


def run_ollama_full(model: str, prompt: str, ollama_url: str = OLLAMA_URL,
                    timeout: int = 120) -> tuple:
    """Run inference and return (text, usage_meta).

    Detects a JSON messages array → /api/chat, else → /api/generate. Strips
    the coordinator options envelope from the first message and merges its
    format/options/keep_alive into the Ollama payload, then guarantees a
    num_ctx floor. usage_meta carries token counts for the completion report.
    """
    if prompt.strip().startswith("["):
        try:
            messages = json.loads(prompt)
            messages, extras = _extract_opts_envelope(messages)
            payload = {"model": model, "messages": messages, "stream": False}
            _apply_opts_to_payload(payload, extras)
            _ensure_num_ctx(payload)
            resp = http("POST", f"{ollama_url}/api/chat", payload, timeout=timeout)
            return resp.get("message", {}).get("content", "").strip(), _usage_from_response(resp)
        except Exception:
            pass
    payload = {"model": model, "prompt": prompt, "stream": False}
    _ensure_num_ctx(payload)
    resp = http("POST", f"{ollama_url}/api/generate", payload, timeout=timeout)
    return resp.get("response", "").strip(), _usage_from_response(resp)


def run_ollama(model: str, prompt: str, ollama_url: str = OLLAMA_URL, timeout: int = 120) -> str:
    """Run inference and return only the text (thin wrapper over run_ollama_full)."""
    text, _ = run_ollama_full(model, prompt, ollama_url=ollama_url, timeout=timeout)
    return text


# ── Agent class ────────────────────────────────────────────────────────────────

class MyAIAgent:
    def __init__(
        self,
        coordinator_url: str = COORDINATOR_URL,
        ollama_url: str = OLLAMA_URL,
        name: str = AGENT_NAME,
        wallet: str = AGENT_WALLET,
        poll_interval: int = POLL_INTERVAL,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
        required_models: List[str] = None,
    ):
        self.coordinator_url    = coordinator_url.rstrip("/")
        self.ollama_url         = ollama_url.rstrip("/")
        self.name               = name
        self.wallet             = wallet
        self.poll_interval      = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.agent_id           = load_agent_id()
        self.agent_secret       = load_agent_secret()
        self.required_models    = required_models if required_models is not None else REQUIRED_MODELS
        self._running           = False
        # v3-C attestation -- per-node ECDSA P-256 key, persisted at
        # $config_dir/.attest-key.pem mode 600. Coordinator falls back to
        # 0.5x earnings multiplier if attestation fields are absent.
        self.attest             = Attestation(
            os.path.join(get_config_dir(), ".attest-key.pem")
        )

    # ── Model Pre-pull ─────────────────────────────────────────────────────────

    def ensure_models(self) -> None:
        """Pull any required models not already present in Ollama."""
        if self.required_models:
            log.info(f"Checking required models: {', '.join(self.required_models)}")
            ensure_models(self.required_models, self.ollama_url)

    # ── Registration ───────────────────────────────────────────────────────────

    def register(self) -> bool:
        gpus   = gpu_mod.detect()
        models = get_ollama_models(self.ollama_url)

        payload = {
            "agent_id":           self.agent_id,
            "agent_name":         self.name,
            "version":            VERSION,
            # v3-A/v3-C: coordinator enum is {browser-webgpu, mobile, native}
            "platform":           "native",
            "platform_os":        platform.system(),
            "ollama_url":         self.ollama_url,
            "gpus":               gpus,
            "models":             models,
            "wallet_address":     self.wallet,
            "price_per_hour_myai": 1.0,
        }
        # v3-C attestation envelope
        if self.attest.available:
            payload["attestation_pubkey_b64"] = self.attest.pubkey_b64
            payload["device_fingerprint"]     = self.attest.device_fingerprint()
            payload["attest_alg"]             = "ecdsa-p256-sha256"
            payload.update(self.attest.sign_envelope(self.agent_id))

        # Use a raw HTTP call so we can detect 409 (device-already-bound) and exit hard.
        import urllib.request as _ur, urllib.error as _ue
        try:
            data = json.dumps(payload).encode()
            req = _ur.Request(
                f"{self.coordinator_url}/api/v1/agents/register",
                data=data, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"myai-agent/{VERSION}",
                },
            )
            with _ur.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
        except _ue.HTTPError as e:
            if e.code == 409:
                body = e.read().decode()[:400]
                log.error(
                    "v3c-attest: REGISTRATION REJECTED 409 -- device fingerprint "
                    "or attestation pubkey already bound to a DIFFERENT wallet: %s", body
                )
                log.error(
                    "If you moved hardware, delete the stale agent row via brain admin "
                    "then run `myai-agent --rotate-attest-key` before retrying."
                )
                sys.exit(1)
            log.warning("Register HTTP %d: %s", e.code, e.read().decode()[:200])
            resp = {}
        except Exception as e:
            log.debug("Register request failed: %s", e)
            resp = {}
        if resp.get("success"):
            _data = resp.get("data", {}) or {}
            _new_secret = _data.get("agent_secret_b64")
            if _new_secret:
                self.agent_secret = _new_secret
                save_agent_secret(_new_secret)
                log.info("agent_secret issued + persisted (HMAC auth enabled)")
            log.info(f"Registered as '{self.name}' (id={self.agent_id})")
            if gpus:
                log.info(f"  GPUs   : {', '.join(g['name'] for g in gpus)}")
            if models:
                log.info(f"  Models : {', '.join(models)}")
            return True
        log.error(f"Registration failed: {resp}")
        return False

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(self.heartbeat_interval)
            if not self._running:
                break
            body = {"status": "online"}
            if self.attest.available:
                body.update(self.attest.sign_envelope(self.agent_id))
            resp = http("POST",
                        f"{self.coordinator_url}/api/v1/agents/{self.agent_id}/heartbeat",
                        body,
                        extra_headers=agent_auth_headers(self.agent_id, self.agent_secret))
            if resp.get("success"):
                log.debug("Heartbeat ok")
            else:
                log.warning("Heartbeat failed — re-registering")
                self.register()

    # ── Job handling ───────────────────────────────────────────────────────────

    def _complete_job(self, job_id: str, result: str, success: bool = True,
                      meta: Optional[Dict[str, Any]] = None):
        body = {"success": success, "result": result}
        if meta:
            body.update(meta)
        if self.attest.available:
            body.update(self.attest.sign_envelope(self.agent_id))
        http("POST",
             f"{self.coordinator_url}/api/v1/agents/{self.agent_id}/jobs/{job_id}/complete",
             body,
             extra_headers=agent_auth_headers(self.agent_id, self.agent_secret))

    def _process_job(self, job: dict):
        job_id = job.get("job_id", "unknown")
        model  = job.get("model", "llama3.2")
        prompt = job.get("prompt", "")

        if not prompt:
            log.warning(f"Job {job_id} has empty prompt — skipping")
            self._complete_job(job_id, "", success=False)
            return

        log.info(f"Job {job_id} | model={model} | {prompt[:60]}...")
        result, meta = run_ollama_full(model, prompt, self.ollama_url)

        if result:
            log.info(
                f"Job {job_id} done ({len(result)} chars, "
                f"in={meta.get('tokens_in')}, out={meta.get('tokens_out')})"
            )
            self._complete_job(job_id, result, success=True, meta=meta)
        else:
            log.warning(f"Job {job_id} returned empty result")
            self._complete_job(job_id, "No response from model", success=False)

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _poll_loop(self):
        log.info(f"Polling {self.coordinator_url} every {self.poll_interval}s...")
        while self._running:
            try:
                resp = http("GET",
                            f"{self.coordinator_url}/api/v1/agents/{self.agent_id}/jobs/pending",
                            timeout=10,
                            extra_headers=agent_auth_headers(self.agent_id, self.agent_secret))
                for job in resp.get("data", {}).get("jobs", []):
                    self._process_job(job)
            except Exception as e:
                log.debug(f"Poll error: {e}")
            time.sleep(self.poll_interval)

    def start(self):
        """Start the agent — blocks until stopped."""
        log.info(f"MyAI GPU Agent v{VERSION} — {self.name} ({platform.system()})")
        log.info(f"  Coordinator : {self.coordinator_url}")
        log.info(f"  Ollama      : {self.ollama_url}")
        log.info(f"  Agent ID    : {self.agent_id}")
        if self.attest.available:
            log.info(f"  Attestation : ECDSA P-256 pubkey {self.attest.pubkey_b64[:24]}...")
        else:
            log.info(f"  Attestation : DISABLED (set MYAI_DISABLE_ATTESTATION=0 or install `cryptography`)")

        # Pre-pull required models before registering
        self.ensure_models()

        # Register with retries
        for attempt in range(5):
            if self.register():
                break
            log.warning(f"Registration attempt {attempt + 1}/5 failed — retrying in 10s")
            time.sleep(10)
        else:
            log.error("Could not register after 5 attempts. Exiting.")
            sys.exit(1)

        self._running = True

        hb = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
        hb.start()

        try:
            self._poll_loop()
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self._running = False
