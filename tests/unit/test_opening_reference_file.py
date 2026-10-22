from datetime import date
from pathlib import Path

from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.runtime.opening_reference_file import (
    load_opening_reference,
    save_opening_reference,
)


def test_opening_reference_is_atomically_round_tripped(tmp_path: Path) -> None:
    path = tmp_path / "critical" / "opening-reference.json"
    assert load_opening_reference(path) is None
    reference = OpeningReference(trade_date=date(2026, 10, 20), payload_hash="sha256:" + "a" * 64)
    save_opening_reference(path, reference)
    assert load_opening_reference(path) == reference
    assert not tuple(path.parent.glob("*.tmp"))
