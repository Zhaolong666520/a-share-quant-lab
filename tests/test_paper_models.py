from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from finance_lab.ledger import LedgerConfig
from finance_lab.paper_models import make_default_accounts, validate_portfolio_id


def test_default_accounts_are_isolated_and_configuration_is_locked() -> None:
    accounts = make_default_accounts(
        portfolio_id="default",
        created_market_date=date(2026, 8, 28),
        ledger_config=LedgerConfig(),
    )

    assert [item.account_id for item in accounts] == [
        "default-sma-20-60-v1",
        "default-momentum-120-v1",
    ]
    assert [item.strategy.name for item in accounts] == ["sma", "momentum"]
    assert accounts[0].config_hash != accounts[1].config_hash
    assert all(len(item.config_hash) == 64 for item in accounts)
    assert accounts[0].initial_cash == accounts[1].initial_cash == 100_000.0
    with pytest.raises(FrozenInstanceError):
        accounts[0].portfolio_id = "changed"  # type: ignore[misc]


def test_same_locked_parameters_have_same_hash_in_another_portfolio() -> None:
    first = make_default_accounts("first", date(2026, 8, 28), LedgerConfig())
    second = make_default_accounts("second", date(2026, 8, 28), LedgerConfig())

    assert first[0].config_hash == second[0].config_hash
    assert first[1].config_hash == second[1].config_hash


@pytest.mark.parametrize(
    "value",
    ["", "../escape", r"C:\escape", "Upper", "has space", "dot.name"],
)
def test_portfolio_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="账户组"):
        validate_portfolio_id(value)

