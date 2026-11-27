# myai-agent

[![PyPI version](https://badge.fury.io/py/myai-agent.svg)](https://pypi.org/project/myai-agent/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Network](https://img.shields.io/badge/network-live-22c55e)](https://myaitoken.io/agents)

**Share your GPU (or CPU) with the MyAI decentralized inference network.**

Your machine runs local AI inference via [Ollama](https://ollama.com). The MyAI
coordinator routes inference jobs to your hardware; verified jobs are recorded
to your **pending MYAI balance**.

> **Earnings status (pre-TGE):** on-chain payouts on Base **have not started
> yet**. Accepted jobs accrue as *pending* MYAI. Reward rates are set at TGE,
> are not guaranteed, and may be zero. Nothing in this repo is an income
> projection. See [myaitoken.io/earn](https://myaitoken.io/earn) for the
> current terms.

---

## One-line install

The one-line install (Linux x86_64/aarch64 and macOS):

```bash
curl -fsSL https://myaitoken.io/install | bash
```

It runs the coordinator's signed installer, which verifies the agent's
Ed25519 signature and sha256 before installing anything, installs Ollama if it
is missing, pulls one model your hardware can run (`llama3.2`, or `qwen2.5:7b`
on a GPU with >= 8 GB VRAM / Apple Silicon with >= 16 GB), and runs the agent
as a background service (systemd or launchd).

It **never asks for a wallet private key**. You can optionally give a
*public* `0x…` address that payouts would go to once they begin, and add or
change it later:

```bash
curl -fsSL https://myaitoken.io/install | MYAI_PAYOUT_ADDRESS=0xYourPublicAddress bash
```

Preview what it would do without changing anything:

```bash
curl -fsSL https://myaitoken.io/install | MYAI_INSTALL_DRY_RUN=1 bash
```

Windows is not supported by the one-line install yet.

---

## Which agent should I run?

There are two agent implementations. **If you are a GPU owner who just wants
to join the network, use the one-line install above.**

| | Coordinator-served signed agent (**supported**) | This repo's pip / 2.x package (**experimental**) |
|---|---|---|
| How you get it | `curl -fsSL https://myaitoken.io/install \| bash` | `pip install myai-agent` or from source |
| Version | 1.18.x (what `https://api.myaitoken.io/api/v1/agents/version` reports) | 2.x (`src/myai_agent`, 2.4.0 in this repo; PyPI has only 2.0.0) |
| Signed release | Yes — Ed25519 manifest verified before install | No signed release |
| Auto-update | Yes — the coordinator ships signed updates | **No.** `src/myai_agent/update.py` exists but is not called by the agent, so a 2.x install never updates itself |
| Version gate | Coordinator can move it forward | 2.x reports a version above 1.18.x, so the coordinator's update gate cannot move it either |

The 2.x package is kept for development and experimentation. If you run it,
you are responsible for updating it by hand, and it may lag behind the
coordinator's protocol. `legacy/myai-agent.py` is a single-file agent kept for
operator parity; it is not an install path for new providers.

---

## Experimental: the pip / 2.x package

Only if you have read [Which agent should I run?](#which-agent-should-i-run)
and want the 2.x package anyway.

PyPI currently has **myai-agent 2.0.0 only**, which is older than this repo.
To run the code in this repo, install from source:

```bash
pip install "git+https://github.com/Infinihash/myai-agent"
# or: git clone https://github.com/Infinihash/myai-agent && pip install ./myai-agent
myai-agent start --wallet 0xYourPublicAddress   # foreground, to test
```

There are no packaged 2.x binaries on the
[Releases page](https://github.com/Infinihash/myai-agent/releases) at the
moment (it lists only `v1.0.0` and `mac-v2.0.1`). `make bundle-mac` /
`make bundle-win` build standalone executables locally with PyInstaller.

> **Note:** Your `agent_id` and `agent_secret` are stored in the config directory
> (`~/Library/Application Support/myai-agent` on macOS, `~/.config/myai-agent`
> on Linux, `%APPDATA%\myai-agent` on Windows) and are preserved across
> upgrades — no need to re-register.

---

## Requirements (2.x package)

| Requirement | Details |
|---|---|
| Python | 3.8+ (zero required dependencies — pure stdlib; `websocket-client` optional for WS dispatch) |
| [Ollama](https://ollama.com) | Runs local inference. |
| GPU | Optional — CPU works too, just slower |

**Supported GPU backends:** NVIDIA (CUDA) · Apple Silicon (Metal) · AMD (ROCm)

---

## Manual setup of the 2.x package (experimental)

### 1. Install Ollama

```bash
# macOS
brew install --cask ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows — download from https://ollama.com/download
```

### 2. Pull a model

Pull a model the coordinator routes jobs to:

```bash
ollama pull llama3.2          # ~2 GB, 3B — runs on CPU or any GPU
ollama pull qwen2.5:7b        # ~4.7 GB, 7B — needs ~8 GB VRAM / 16 GB unified memory
```

The agent auto-pulls `llama3.2` on first start unless `REQUIRED_MODELS` says
otherwise (see below).

### 3. Install the agent

```bash
pip install "git+https://github.com/Infinihash/myai-agent"

# Run as a background service (auto-starts on login/reboot)
myai-agent install --wallet 0xYourPublicAddress --name "My Rig"

# Or run in the foreground to test
myai-agent start --wallet 0xYourPublicAddress
```

---

## CLI reference

```
myai-agent start              Run in foreground
myai-agent install            Install as OS service (auto-start on login)
myai-agent uninstall          Remove OS service
myai-agent status             Show service status and agent ID
myai-agent logs               Tail live logs
myai-agent models             List Ollama models available on this machine
myai-agent show-pubkey        Print the attestation pubkey + fingerprint
myai-agent rotate-attest-key  Delete + regenerate the attestation key
myai-agent run-job <prompt>   Run a one-off local inference job (for testing)
myai-agent needle             Send a 6 k-token needle prompt and assert it is found (truncation test)
myai-agent --version          Print version
```

### Flags for `start` / `install`

| Flag | Default | Description |
|---|---|---|
| `--wallet 0x...` | — | Public Base address for MYAI rewards (used once payouts begin) |
| `--name NAME` | hostname | Display name on the network |
| `--coordinator URL` | `https://api.myaitoken.io` | Coordinator endpoint |
| `--ollama URL` | `http://localhost:11434` | Ollama server |
| `--model MODEL` (`install` only) | `llama3.2` | Default Ollama model |

To change which models are auto-pulled, use the `REQUIRED_MODELS`
environment variable (there is no CLI flag for it).

---

## Configuration via environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_WALLET` | — | Public Base address for MYAI rewards |
| `AGENT_NAME` | hostname | Node display name |
| `COORDINATOR_URL` | `https://api.myaitoken.io` | Coordinator API |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `REQUIRED_MODELS` | `llama3.2` | Comma-separated models to auto-pull, e.g. `llama3.2,qwen2.5:7b` |
| `POLL_INTERVAL` | `5` | Seconds between job polls |
| `HEARTBEAT_INTERVAL` | `30` | Seconds between heartbeats |
| `MYAI_WS_URL` | _(derived)_ | Override WebSocket base URL (e.g. `ws://localhost:8000`) |
| `MYAI_SHARD_ROOT` | — | Directory of shard files advertised in agent hello; omit to disable |
| `MYAI_NUM_CTX` | `8192` | Ollama context window floor — prevents silent prompt truncation |

**Disable auto-pull:**
```bash
REQUIRED_MODELS="" myai-agent start
```

---

## How jobs flow

```
User request
    │
    ▼
MyAI Coordinator  (https://api.myaitoken.io)
    │  dispatches job
    ▼
Your agent polls  GET /api/v1/agents/{id}/jobs/pending
    │
    ▼
Ollama runs inference locally  POST /api/generate
    │
    ▼
Result returned  POST /api/v1/agents/{id}/jobs/{job_id}/complete
    │
    ▼
Verified, then recorded to your pending MYAI balance
```

---

## Platform notes

### macOS
- Service: launchd plist at `~/Library/LaunchAgents/io.myaitoken.agent.plist`
- Logs: `~/Library/Logs/myai-agent/agent.log` or `myai-agent logs`

### Linux
- Service: systemd user unit at `~/.config/systemd/user/myai-agent.service`
- Logs: `journalctl --user -u myai-agent -f` or `myai-agent logs`

### Windows
- Service: Task Scheduler task (triggers on login)
- Logs: check Task Scheduler history or redirect stdout in the task action

---

## Troubleshooting

**Agent registers but gets no jobs**
- Confirm Ollama is running: `ollama ps`
- Confirm at least one routed model is pulled (`llama3.2` or `qwen2.5:7b`): `ollama list`
- Check coordinator reachability: `curl https://api.myaitoken.io/health`

**Ollama not found**
- Default URL is `http://localhost:11434`. Override with `--ollama` or `OLLAMA_URL` if needed.

**No MYAI showing in my wallet**
- Expected: on-chain payouts have not started yet (pre-TGE). Verified jobs
  accrue as a pending MYAI balance; nothing is sent to your wallet today.
  Reward terms are set at TGE and may be zero. See
  [myaitoken.io/earn](https://myaitoken.io/earn).

**Service won't start after reboot**
- macOS: `launchctl list | grep myaitoken`
- Linux: `systemctl --user status myai-agent`
- Run `myai-agent install` again to re-register the service.

---

## MYAI token

| | |
|---|---|
| Contract | `0xaff22cc20434ce43b3ea10efe10e9360390d327c` |
| Chain | Base · Chain ID 8453 |
| Symbol / Supply | MYAI · 1,000,000,000 |

MYAI is a utility token used to pay for and reward network compute. Rewards
are earned for verified work, are set at TGE, are not guaranteed, and may be
zero.

---

## Links

- [myaitoken.io](https://myaitoken.io) — main site
- [Provider guide](https://myaitoken.io/earn) — how providing works and current reward terms
- [Live network](https://myaitoken.io/agents) — see active nodes right now
- [Docs](https://myaitoken.io/docs) — full documentation

---

## License

MIT © [MyAI Token](https://myaitoken.io)
