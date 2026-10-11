"""Configuration: non-secret settings from config.yaml, secrets from the environment.

Secrets are read only from environment variables, a `.env` file, or Docker secrets
(files in /run/secrets named after the lower-cased variable). They are held as
SecretStr so they never show up in repr/logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISK"
DOCKER_SECRETS_DIR = Path("/run/secrets")


class ConfigError(Exception):
    """Raised when configuration or environment is invalid. The process must stop."""


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"

    @property
    def tag(self) -> str:
        return f"[{self.value.upper()}]"


# --------------------------------------------------------------------------- yaml


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Endpoints:
    rest: str
    ws_public: str
    ws_market: str
    ws_private: str


class ExchangeConfig(_Strict):
    # Live (also used for paper market data). Binance splits USDⓈ-M futures streams:
    # /public carries bookTicker+depth, /market carries aggTrade+markPrice, /private the
    # user data stream. The legacy /stream URL no longer delivers all of them.
    rest_base_url: str = "https://fapi.binance.com"
    ws_public_url: str = "wss://fstream.binance.com/public/stream"
    ws_market_url: str = "wss://fstream.binance.com/market/stream"
    ws_private_url: str = "wss://fstream.binance.com/private/ws"
    testnet_rest_base_url: str = "https://demo-fapi.binance.com"
    testnet_ws_public_url: str = "wss://fstream.binancefuture.com/public/stream"
    testnet_ws_market_url: str = "wss://fstream.binancefuture.com/market/stream"
    testnet_ws_private_url: str = "wss://fstream.binancefuture.com/private/ws"
    maker_fee_bps: float = Field(default=2.0, ge=0)
    taker_fee_bps: float = Field(default=5.0, ge=0)  # paper flatten (market order) cost
    request_timeout_s: float = Field(default=10.0, gt=0)
    # Back off when used weight / order count exceeds this fraction of the limit.
    weight_backoff_ratio: float = Field(default=0.8, gt=0, le=1)
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    time_sync_interval_s: float = Field(default=300.0, ge=10)

    def endpoints(self, mode: Mode) -> Endpoints:
        """Testnet never touches live URLs; paper reads live public market data only."""
        if mode is Mode.TESTNET:
            return Endpoints(self.testnet_rest_base_url, self.testnet_ws_public_url,
                             self.testnet_ws_market_url, self.testnet_ws_private_url)
        return Endpoints(self.rest_base_url, self.ws_public_url, self.ws_market_url,
                         self.ws_private_url)


class MarketDataConfig(_Strict):
    depth_levels: Literal[5, 10, 20] = 20
    depth_speed_ms: Literal[100, 250, 500] = 100
    stale_after_s: float = Field(default=5.0, gt=0)
    reconnect_backoff_initial_s: float = Field(default=1.0, gt=0)
    reconnect_backoff_max_s: float = Field(default=60.0, gt=0)


class RecorderConfig(_Strict):
    enabled: bool = True
    data_dir: Path = Path("data/recordings")
    flush_interval_s: float = Field(default=10.0, gt=0)


class ScreenerConfig(_Strict):
    min_quote_volume_usdt: float = Field(default=50_000_000, ge=0)
    kline_minutes: int = Field(default=60, ge=10, le=499)
    top_n: int = Field(default=20, ge=1)
    max_concurrency: int = Field(default=5, ge=1, le=20)


class StrategyConfig(_Strict):
    """Avellaneda–Stoikov in bps units: σ is per-√second in bps, κ is per bps."""

    gamma: float = Field(default=0.05, gt=0)        # risk aversion (per bps per lot)
    kappa: float = Field(default=0.5, gt=0)         # order-arrival decay (per bps)
    horizon_s: float = Field(default=60.0, gt=0)    # τ = T − t, fixed for a 24/7 market
    vol_halflife_s: float = Field(default=120.0, gt=0)
    vol_warmup_s: float = Field(default=60.0, ge=0)  # no quoting until σ has this much data
    min_spread_buffer_bps: float = Field(default=1.0, ge=0)  # min spread = 2×maker_fee + buffer
    order_notional_usdt: float = Field(default=100.0, gt=0)
    max_inventory_notional_usdt: float = Field(default=500.0, gt=0)
    requote_threshold_bps: float = Field(default=0.5, ge=0)
    requote_min_interval_ms: int = Field(default=250, ge=0)


class PaperConfig(_Strict):
    latency_ms: int = Field(default=100, ge=0)


class ReportConfig(_Strict):
    interval_s: int = Field(default=3600, ge=60)   # periodic report, aligned to the clock
    daily_summary: bool = True                     # extra summary at 00:00 UTC


class NotifyConfig(_Strict):
    queue_size: int = Field(default=100, ge=1)
    min_interval_s: float = Field(default=1.0, ge=0)
    max_retries: int = Field(default=5, ge=0)
    reconnect_alert_count: int = Field(default=5, ge=1)  # reconnects within 10 min → alert


class RiskConfig(_Strict):
    """Hard limits. Risk overrides the strategy and Telegram commands."""

    max_position_notional_usdt: float = Field(default=600.0, gt=0)
    max_order_notional_usdt: float = Field(default=200.0, gt=0)
    daily_loss_limit_usdt: float = Field(default=20.0, gt=0)  # net PnL today ≤ −limit → flatten + halt
    circuit_breaker_pct: float = Field(default=0.5, gt=0)      # price move ≥ X% ...
    circuit_breaker_window_s: float = Field(default=10.0, gt=0)  # ... within Y seconds
    circuit_breaker_cooldown_s: float = Field(default=60.0, ge=0)
    # |funding rate| above this against our position → quote only the reducing side
    funding_guard_rate: float = Field(default=0.0005, gt=0)
    dead_man_countdown_s: int = Field(default=60, ge=10)   # countdownCancelAll (testnet/live)
    dead_man_refresh_s: int = Field(default=15, ge=1)
    leverage: int = Field(default=2, ge=1, le=20)
    margin_type: Literal["ISOLATED"] = "ISOLATED"
    position_check_interval_s: float = Field(default=30.0, ge=5)  # exchange position/order resync


class StateConfig(_Strict):
    db_dir: Path = Path("data")  # state-{mode}.sqlite
    flush_interval_s: float = Field(default=2.0, gt=0)
    pnl_snapshot_interval_s: float = Field(default=60.0, gt=0)


class OpsConfig(_Strict):
    health_enabled: bool = True
    health_port: int = Field(default=8080, ge=1, le=65535)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AppConfig(_Strict):
    symbol: str = "BTCUSDT"
    exchange: ExchangeConfig = ExchangeConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    recorder: RecorderConfig = RecorderConfig()
    screener: ScreenerConfig = ScreenerConfig()
    strategy: StrategyConfig = StrategyConfig()
    paper: PaperConfig = PaperConfig()
    report: ReportConfig = ReportConfig()
    notify: NotifyConfig = NotifyConfig()
    risk: RiskConfig = RiskConfig()
    state: StateConfig = StateConfig()
    ops: OpsConfig = OpsConfig()
    logging: LoggingConfig = LoggingConfig()

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.endswith("USDT"):
            raise ValueError("only USDT-margined perpetual symbols are supported")
        return v


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    try:
        return AppConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"invalid config {p}: {e}") from e


# ------------------------------------------------------------------------ secrets


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        secrets_dir=DOCKER_SECRETS_DIR if DOCKER_SECRETS_DIR.is_dir() else None,
    )

    mode: Mode = Mode.PAPER
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    live_confirm: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_commands: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _empty_mode_is_paper(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return Mode.PAPER
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator(
        "binance_testnet_api_key",
        "binance_testnet_api_secret",
        "binance_api_key",
        "binance_api_secret",
        "telegram_bot_token",
        "telegram_chat_id",
        "live_confirm",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("telegram_commands", mode="before")
    @classmethod
    def _empty_bool_is_false(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return False
        return v


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True)
    api_key: SecretStr
    api_secret: SecretStr


class TelegramSettings(BaseModel):
    model_config = ConfigDict(frozen=True)
    bot_token: SecretStr
    chat_id: str
    commands_enabled: bool


class Runtime(BaseModel):
    """Resolved runtime environment. `credentials` is None in paper mode."""

    model_config = ConfigDict(frozen=True)
    mode: Mode
    credentials: Credentials | None
    telegram: TelegramSettings | None


def resolve_runtime(env: EnvSettings) -> Runtime:
    """Pick credentials strictly by mode. Never falls back across modes."""
    if env.mode is Mode.PAPER:
        if any(
            v is not None
            for v in (
                env.binance_testnet_api_key,
                env.binance_testnet_api_secret,
                env.binance_api_key,
                env.binance_api_secret,
            )
        ):
            log.warning("MODE=paper: API keys are set but will NOT be used")
        creds = None
    elif env.mode is Mode.TESTNET:
        if env.binance_testnet_api_key is None or env.binance_testnet_api_secret is None:
            raise ConfigError(
                "MODE=testnet requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET "
                "(live keys are never used as a fallback)"
            )
        creds = Credentials(
            api_key=env.binance_testnet_api_key, api_secret=env.binance_testnet_api_secret
        )
    else:  # LIVE
        if env.live_confirm != LIVE_CONFIRM_PHRASE:
            raise ConfigError(f"MODE=live requires LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}")
        if env.binance_api_key is None or env.binance_api_secret is None:
            raise ConfigError("MODE=live requires BINANCE_API_KEY and BINANCE_API_SECRET")
        creds = Credentials(api_key=env.binance_api_key, api_secret=env.binance_api_secret)

    telegram: TelegramSettings | None = None
    if env.telegram_bot_token is not None and env.telegram_chat_id is not None:
        telegram = TelegramSettings(
            bot_token=env.telegram_bot_token,
            chat_id=env.telegram_chat_id,
            commands_enabled=env.telegram_commands,
        )
    return Runtime(mode=env.mode, credentials=creds, telegram=telegram)


def load_runtime() -> Runtime:
    try:
        env = EnvSettings()
    except ValueError as e:
        raise ConfigError(f"invalid environment: {e}") from e
    return resolve_runtime(env)


def secret_values(runtime: Runtime) -> list[str]:
    """Raw secret strings, for the log redaction filter only."""
    out: list[str] = []
    if runtime.credentials:
        out += [
            runtime.credentials.api_key.get_secret_value(),
            runtime.credentials.api_secret.get_secret_value(),
        ]
    if runtime.telegram:
        out.append(runtime.telegram.bot_token.get_secret_value())
    return [s for s in out if s]
