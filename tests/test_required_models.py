"""REQUIRED_MODELS default + override behaviour (auto-pull on first start)."""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from myai_agent import agent  # noqa: E402

# Models the coordinator's canonical installer picks (static/install.sh
# MODEL_SMALL / MODEL_LARGE) and that exist on ollama.com.
ROUTED_MODELS = {"llama3.2", "llama3.2:latest", "qwen2.5:7b"}


def _reload_with_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("REQUIRED_MODELS", raising=False)
    else:
        monkeypatch.setenv("REQUIRED_MODELS", value)
    return importlib.reload(agent)


def test_default_is_a_routed_model(monkeypatch):
    mod = _reload_with_env(monkeypatch, None)
    assert mod.REQUIRED_MODELS == ["llama3.2"]
    assert set(mod.REQUIRED_MODELS) <= ROUTED_MODELS


def test_default_never_reintroduces_bonsai(monkeypatch):
    # bonsai-8b:latest does not exist on ollama.com; first-start pull failed.
    mod = _reload_with_env(monkeypatch, None)
    assert not any("bonsai" in m for m in mod.REQUIRED_MODELS)


def test_env_override(monkeypatch):
    mod = _reload_with_env(monkeypatch, " qwen2.5:7b , llama3.2 ,")
    assert mod.REQUIRED_MODELS == ["qwen2.5:7b", "llama3.2"]


def test_empty_env_disables_auto_pull(monkeypatch):
    mod = _reload_with_env(monkeypatch, "")
    assert mod.REQUIRED_MODELS == []


def test_parse_drops_blanks():
    assert agent.parse_required_models("a,,b, ") == ["a", "b"]


def test_ensure_models_pulls_default_when_missing(monkeypatch):
    mod = _reload_with_env(monkeypatch, None)
    pulled = []
    monkeypatch.setattr(mod, "get_ollama_models", lambda url=None: ["mistral:latest"])
    monkeypatch.setattr(mod, "pull_model", lambda m, url=None: pulled.append(m))
    mod.ensure_models(mod.REQUIRED_MODELS, "http://127.0.0.1:1")
    assert pulled == ["llama3.2"]


def test_ensure_models_skips_default_when_present_as_latest(monkeypatch):
    mod = _reload_with_env(monkeypatch, None)
    pulled = []
    monkeypatch.setattr(mod, "get_ollama_models", lambda url=None: ["llama3.2:latest"])
    monkeypatch.setattr(mod, "pull_model", lambda m, url=None: pulled.append(m))
    mod.ensure_models(mod.REQUIRED_MODELS, "http://127.0.0.1:1")
    assert pulled == []


def teardown_module(module):
    os.environ.pop("REQUIRED_MODELS", None)
    importlib.reload(agent)
