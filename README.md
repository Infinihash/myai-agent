# myai-agent

[![PyPI version](https://badge.fury.io/py/myai-agent.svg)](https://pypi.org/project/myai-agent/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Network](https://img.shields.io/badge/network-live-22c55e)](https://myaitoken.io/agents)

**Earn MYAI tokens by sharing your GPU (or CPU) with the MyAI decentralized inference network.**

Your machine runs local AI inference via [Ollama](https://ollama.com). The MyAI coordinator dispatches jobs from paying users and your wallet receives MYAI on Base for every completed request — no staking, no lock-ups, no middlemen.

```
Install → Run → Earn
```

---

## One-line install

```bash
curl -fsSL https://myaitoken.io/install | bash
```

With your wallet pre-set (start earning immediately):

```bash
WALLET=0xYourAddress curl -fsSL https://myaitoken.io/install | bash
```

Or via pip:

```bash
pip install myai-agent
myai-agent install --wallet 0xYourAddress
```

---

## Packaged binaries (no Python required)

Starting with v2.4.0 each GitHub release ships standalone executables for macOS
(Apple Silicon arm64) and Windows (amd64).  They bundle Python + websocket-client
so you don't need a local Python environment.

### Install (macOS arm64)

```bash
VERSION=2.4.0
curl -L -o myai-agent \
  https://github.com/myaitoken/myai-agent/releases/download/v${VERSION}/myai-agent-${VERSION}-mac-arm64
chmod +x myai-agent
./myai-agent start --wallet 0xYourAddress
```

### Install (Windows amd64)

Download `myai-agent-<version>-win-amd64.exe` from the [Releases page](https://github.com/myaitoken/myai-agent/releases), then:

```powershell
.\myai-agent-2.4.0-win-amd64.exe start --wallet 0xYourAddress
```

### Verify checksums

A `.sha256` file is published alongside each binary:

```bash
sha256sum -c myai-agent-2.4.0-mac-arm64.sha256
```

### Upgrade

To upgrade, stop the running agent, download the new binary, and restart:

```bash
# macOS / Linux
myai-agent stop 2>/dev/null || true
curl -L -o myai-agent \
  https://github.com/myaitoken/myai-agent/releases/download/v2.4.0/myai-agent-2.4.0-mac-arm64
chmod +x myai-agent
./myai-agent start --wallet 0xYourAddress
```

```powershell
# Windows — stop via Task Manager or:
Stop-Process -Name "myai-agent*" -Force 2>$null
# Download new .exe and run
.\myai-agent-2.4.0-win-amd64.exe start --wallet 0xYourAddress
```

> **Note:** Your `agent_id` and `agent_secret` are stored in the config directory
> (`~/Library/Application Support/myai-agent` on macOS, `%APPDATA%\myai-agent` on Windows)
> and are preserved across upgrades — no need to re-register.

---

## Requirements

| Requirement | Details |
|---|---|
| Python | 3.8+ (zero external dependencies — pure stdlib) |
| [Ollama](https://ollama.ai) | Runs local inference. Any version. |
| GPU | Optional — CPU works too, just slower |

**Supported GPU backends:** NVIDIA (CUDA) · Apple Silicon (Metal) · AMD (ROCm)

---

## Manual setup (3 steps)

### 1. Install Ollama

```bash
# macOS
brew install --cask ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows — download from https://ollama.com/download
```

### 2. Pull a model

```bash
ollama pull llama3.2          # 2 GB — fast, recommended for most rigs
ollama pull qwen2.5:14b       # 9 GB — higher quality, better earnings
ollama pull mistral           # 4 GB — solid all-rounder
```

The agent auto-pulls `bonsai-8b:latest` on first start if nothing else is configured.

### 3. Install the agent

```bash
pip install myai-agent

# Run as a persistent background service (auto-starts on login/reboot)
myai-agent install --wallet 0xYourAddress --name "My Rig"

# Or run in the foreground to test
myai-agent start --wallet 0xYourAddress
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
myai-agent run-job <prompt>   Run a one-off local inference job (for testing)
myai-agent needle             Send a 6 k-token needle prompt and assert it is found (truncation test)
myai-agent --version          Print version
```

### Flags for `start` / `install`

| Flag | Default | Description |
|---|---|---|
| `--wallet 0x...` | — | Base EVM address for MYAI rewards |
| `--name NAME` | hostname | Display name on the network |
| `--model MODEL` | `llama3.2` | Default Ollama model |
| `--required-models M1,M2` | `bonsai-8b:latest` | Models to auto-pull on startup |
| `--coordinator URL` | `https://api.myaitoken.io` | Coordinator endpoint |
| `--ollama URL` | `http://localhost:11434` | Ollama server |

---

## Configuration via environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_WALLET` | — | Base wallet address for MYAI payouts |
| `AGENT_NAME` | hostname | Node display name |
| `COORDINATOR_URL` | `https://api.myaitoken.io` | Coordinator API |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `REQUIRED_MODELS` | `bonsai-8b:latest` | Comma-separated models to auto-pull |
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
MYAI paid to your wallet on Base
```

Large jobs are automatically sharded across multiple agents for lower latency. Each contributing node gets a proportional share of the fee.

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
- Confirm at least one model is pulled: `myai-agent models`
- Check coordinator reachability: `curl https://api.myaitoken.io/healthz`

**Ollama not found**
- Default URL is `http://localhost:11434`. Override with `--ollama` or `OLLAMA_URL` if needed.

**No MYAI earnings showing**
- Payouts batch weekly and appear on Base. Check your address on [Basescan](https://basescan.org) or the [earnings dashboard](https://myaitoken.io/dashboard).
- Make sure `--wallet` is set to a valid Base EVM address.

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
| DEX | [Aerodrome Finance](https://aerodrome.finance) |

---

## Links

- [myaitoken.io](https://myaitoken.io) — main site
- [Provider guide](https://myaitoken.io/earn) — earnings details and expected MYAI per job
- [Live network](https://myaitoken.io/agents) — see active nodes right now
- [Docs](https://myaitoken.io/docs) — full documentation

---

## License

MIT © [MyAI Token](https://myaitoken.io)
