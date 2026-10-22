from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.strategies.trading_settings_drafts import (
    TradingSettingsDraftError,
    TradingSettingsDraftStore,
)


def settings(**buy_changes: object) -> TradingSettings:
    buy: dict[str, Any] = {
        "order_method": "ASK1_PROTECTED_LIMIT",
        "max_daily_buy_value": "50000",
        "max_symbol_buy_value": "10000",
        "max_order_value": "5000",
        "cancel_after_seconds": 10,
        "max_retries": 3,
    }
    buy.update(buy_changes)
    return TradingSettings.model_validate_json(
        canonical_bytes(
            {
                "buy": buy,
                "sell": {
                    "take_profit_pct": "5",
                    "stop_loss_pct": "3",
                    "sell_by": "14:30",
                },
                "safety": {
                    "max_daily_loss": "2000",
                    "max_positions": 5,
                    "emergency_liquidation_enabled": True,
                },
                "advanced": {
                    "max_order_quantity": 10000,
                    "max_chase_bps": 30,
                    "max_spread_bps": 20,
                    "quote_max_age_ms": 1000,
                },
            }
        )
    )


def test_trading_settings_are_versioned_and_persisted(tmp_path: Path) -> None:
    store = TradingSettingsDraftStore(tmp_path)
    first = store.save(settings(), expected_version=0)

    assert first.version == 1
    assert first.settings.buy.enabled
    assert store.read() == first
    with pytest.raises(TradingSettingsDraftError, match="VERSION_CONFLICT"):
        store.save(settings(cancel_after_seconds=20), expected_version=0)

    second = store.save(settings(cancel_after_seconds=20), expected_version=1)
    assert second.version == 2
    assert second.settings.buy.cancel_after_seconds == 20


@pytest.mark.parametrize(
    "changes",
    [
        {"max_daily_buy_value": "0"},
        {"max_symbol_buy_value": "4000", "max_order_value": "5000"},
        {"max_retries": 4},
    ],
)
def test_invalid_buy_settings_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        settings(**changes)


def test_all_zero_buy_ceilings_explicitly_disable_buying() -> None:
    value = settings(max_daily_buy_value="0", max_symbol_buy_value="0", max_order_value="0")

    assert not value.buy.enabled
