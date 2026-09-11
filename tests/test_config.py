"""Config loading: env-override precedence and Path coercion."""

import json

from msgraph_tui.core.config import AppConfig, Mode, load_config


def test_env_overrides_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"mode": "mock", "tenant_id": "from-file", "allow_beta": False}))
    monkeypatch.setenv("GRAPHDECK_TENANT_ID", "from-env")
    monkeypatch.setenv("GRAPHDECK_ALLOW_BETA", "true")
    cfg = load_config(cfg_file)
    assert cfg.tenant_id == "from-env"     # env wins over file
    assert cfg.allow_beta is True          # string "true" coerced to bool


def test_unknown_keys_ignored(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"mode": "dry-run", "future_setting": 123}))
    cfg = load_config(cfg_file)
    assert cfg.mode is Mode.DRY_RUN
    assert not hasattr(cfg, "future_setting")


def test_path_fields_coerced(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"audit_hmac_key_path": str(tmp_path / "k.key")}))
    cfg = load_config(cfg_file)
    assert cfg.audit_hmac_key_path == tmp_path / "k.key"


def test_missing_config_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert isinstance(cfg, AppConfig)
    assert cfg.mode is Mode.MOCK  # dataclass default
