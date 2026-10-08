import json

import pytest

from stock_trader.swing_final import collect_final, frozen_reference


@pytest.mark.asyncio
async def test_final_collection_cannot_open_without_development_stress(settings, tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text(
        json.dumps(
            {"completed_at": "2026-10-03", "development_stress_passed": False, "chosen": "trend_rule_10"}
        )
    )

    def forbidden_broker(_):
        raise AssertionError("final market data must remain unopened")

    monkeypatch.setattr("stock_trader.swing_final.Alpaca", forbidden_broker)
    with pytest.raises(ValueError, match="first pass"):
        await collect_final(settings, tmp_path)
    assert not settings.data_dir.exists()


def test_final_candidate_source_hash_cannot_change(tmp_path):
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "completed_at": "2026-10-03",
                "development_stress_passed": True,
                "chosen": "trend_rule_10",
                "source": str(tmp_path),
                "source_report_sha256": "incorrect",
            }
        )
    )
    with pytest.raises(ValueError, match="report changed"):
        frozen_reference(tmp_path)
