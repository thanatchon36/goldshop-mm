import logging

import pytest

from goldshop.config import (
    AppConfig,
    ConfigError,
    EnvSettings,
    Mode,
    load_config,
    resolve_runtime,
)

ENV_VARS = [
    "MODE", "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET", "BINANCE_API_KEY",
    "BINANCE_API_SECRET", "LIVE_CONFIRM", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "TELEGRAM_COMMANDS",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    return monkeypatch


def env(**kw) -> EnvSettings:
    return EnvSettings(_env_file=None, **kw)


def test_default_mode_is_paper_and_needs_no_keys(clean_env):
    rt = resolve_runtime(env())
    assert rt.mode is Mode.PAPER
    assert rt.credentials is None
    assert rt.telegram is None


def test_empty_mode_string_is_paper(clean_env):
    clean_env.setenv("MODE", "")
    assert resolve_runtime(env()).mode is Mode.PAPER


def test_paper_ignores_keys_and_warns(clean_env, caplog):
    with caplog.at_level(logging.WARNING):
        rt = resolve_runtime(env(binance_api_key="realkey", binance_api_secret="realsecret"))
    assert rt.credentials is None
    assert "will NOT be used" in caplog.text
    assert "realkey" not in caplog.text


def test_testnet_uses_only_testnet_keys(clean_env):
    rt = resolve_runtime(env(
        mode="testnet",
        binance_testnet_api_key="tk", binance_testnet_api_secret="ts",
        binance_api_key="LIVEKEY", binance_api_secret="LIVESECRET",
    ))
    assert rt.credentials is not None
    assert rt.credentials.api_key.get_secret_value() == "tk"
    assert rt.credentials.api_secret.get_secret_value() == "ts"


def test_testnet_without_testnet_keys_never_falls_back_to_live(clean_env):
    with pytest.raises(ConfigError, match="never used as a fallback"):
        resolve_runtime(env(mode="testnet", binance_api_key="LIVEKEY", binance_api_secret="LIVESECRET"))


def test_testnet_partial_keys_rejected(clean_env):
    with pytest.raises(ConfigError):
        resolve_runtime(env(mode="testnet", binance_testnet_api_key="tk"))


def test_live_requires_confirm_phrase(clean_env):
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        resolve_runtime(env(mode="live", binance_api_key="k", binance_api_secret="s"))
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        resolve_runtime(env(mode="live", binance_api_key="k", binance_api_secret="s",
                            live_confirm="yes"))


def test_live_never_uses_testnet_keys(clean_env):
    with pytest.raises(ConfigError, match="BINANCE_API_KEY"):
        resolve_runtime(env(mode="live", live_confirm="I_UNDERSTAND_THE_RISK",
                            binance_testnet_api_key="tk", binance_testnet_api_secret="ts"))


def test_live_ok(clean_env):
    rt = resolve_runtime(env(mode="live", live_confirm="I_UNDERSTAND_THE_RISK",
                             binance_api_key="k", binance_api_secret="s"))
    assert rt.mode is Mode.LIVE and rt.credentials is not None


def test_mode_from_environment_variable(clean_env):
    clean_env.setenv("MODE", "TESTNET")
    clean_env.setenv("BINANCE_TESTNET_API_KEY", "tk")
    clean_env.setenv("BINANCE_TESTNET_API_SECRET", "ts")
    assert resolve_runtime(env()).mode is Mode.TESTNET


def test_invalid_mode_rejected(clean_env):
    with pytest.raises(ValueError):
        env(mode="production")


def test_secrets_not_in_repr(clean_env):
    rt = resolve_runtime(env(mode="testnet", binance_testnet_api_key="tk-secret-value",
                             binance_testnet_api_secret="ts-secret-value"))
    assert "secret-value" not in repr(rt)
    assert "secret-value" not in str(rt)


def test_telegram_needs_both_token_and_chat(clean_env):
    assert resolve_runtime(env(telegram_bot_token="t0ken")).telegram is None
    tg = resolve_runtime(env(telegram_bot_token="t0ken", telegram_chat_id="123")).telegram
    assert tg is not None and tg.commands_enabled is False


def test_telegram_commands_default_false_and_parsed(clean_env):
    clean_env.setenv("TELEGRAM_COMMANDS", "")
    assert env().telegram_commands is False
    clean_env.setenv("TELEGRAM_COMMANDS", "true")
    assert env().telegram_commands is True


def test_dotenv_file_is_read(clean_env, tmp_path):
    (tmp_path / ".env").write_text("MODE=testnet\nBINANCE_TESTNET_API_KEY=a\nBINANCE_TESTNET_API_SECRET=b\n")
    assert resolve_runtime(EnvSettings()).mode is Mode.TESTNET


def test_repo_config_yaml_is_valid():
    from pathlib import Path

    cfg = load_config(Path(__file__).parent.parent / "config.yaml")
    assert cfg.symbol.endswith("USDT")
    assert cfg.exchange.maker_fee_bps > 0


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("symbol: BTCUSDT\nsymbl_typo: 1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_config_rejects_non_usdt_symbol():
    with pytest.raises(ValueError):
        AppConfig(symbol="BTCBUSD")


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
