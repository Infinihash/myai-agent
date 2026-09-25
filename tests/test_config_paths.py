import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from myai_agent import config


def test_linux_uses_xdg_dirs(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-cfg")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")

    assert config.get_config_dir() == "/tmp/xdg-cfg/myai-agent"
    assert config.get_log_dir() == "/tmp/xdg-state/myai-agent"
    assert config.get_env_file() == "/tmp/xdg-cfg/myai-agent/config.env"


def test_linux_defaults_without_xdg(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")

    assert config.get_config_dir() == "/home/tester/.config/myai-agent"
    assert config.get_log_dir() == "/home/tester/.local/state/myai-agent"


def test_darwin_paths(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", "/home/tester")

    assert config.get_config_dir() == "/home/tester/Library/Application Support/myai-agent"
    assert config.get_log_dir() == "/home/tester/Library/Logs/myai-agent"
    assert config.get_env_file() == "/home/tester/Library/Application Support/myai-agent/config.env"


def test_windows_appdata(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", "/appdata")

    assert config.get_config_dir() == "/appdata/myai-agent"
    assert config.get_log_dir() == "/appdata/myai-agent/logs"


def test_windows_without_appdata(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Windows")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")

    assert config.get_config_dir() == "/home/tester/myai-agent"


def test_linux_unicode_xdg(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/cfg-ü")

    assert config.get_config_dir() == "/tmp/cfg-ü/myai-agent"
