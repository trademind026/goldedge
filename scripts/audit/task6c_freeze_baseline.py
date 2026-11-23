"""
Task 6C (Phase 7) — Baseline Freeze (`RECOVERED_FULL_HISTORY_BASELINE_V2`).

Safety & Gate Enforcement:
  - Refuses to freeze unless Task 4 preservation checks are BOTH True AND
    all 3 windows (W1=118, W2=131, W3=143) show ZERO trade-level mismatches AND ZERO
    trailing-journey mismatches between RunBacktestUseCase and the independent raw-tick validator.
  - Never modifies original Task 4 frozen artifacts (`frozen_111_baseline_trades.csv`,
    `task4_run_1.csv`, `task4_baseline_manifest.json`, etc.).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if not (_ROOT / "src").exists() and Path("/app/src").exists():
    _ROOT = Path("/app")
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.config.settings import settings
from goldedge.domain.services.broker_cost_service import (
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
)

AUDIT_DIR = _ROOT / "backtest_results" / "audit"
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"

SOURCE_FILES = [
    "src/goldedge/application/use_cases/run_backtest.py",
    "src/goldedge/domain/services/denoise_service.py",
    "src/goldedge/domain/services/indicator_service.py",
    "src/goldedge/domain/services/regime_detector.py",
    "src/goldedge/domain/services/signal_generator.py",
    "src/goldedge/domain/services/trade_scorer.py",
    "src/goldedge/domain/services/sizing_service.py",
    "src/goldedge/domain/services/mtf_trend.py",
    "src/goldedge/domain/services/broker_cost_service.py",
    "src/goldedge/adapters/secondary/db/tick_repository.py",
    "src/goldedge/adapters/secondary/db/backtest_repository.py",
    "src/goldedge/config/risk_profiles.py",
    "src/goldedge/config/settings.py",
]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ds = json.loads((AUDIT_DIR / "task6c_dataset_integrity.json").read_text(encoding="utf-8"))
    rec = json.loads((AUDIT_DIR / "task6c_validation_summary.json").read_text(encoding="utf-8"))
    app_sum = json.loads((AUDIT_DIR / "task6c_app_summary.json").read_text(encoding="utf-8"))

    # Strict validation gates before freezing
    assert ds["task4_preservation"]["warmup_exact_match"] is True, "Task 4 warmup hash mismatch!"
    assert ds["task4_preservation"]["replay_exact_match"] is True, "Task 4 replay hash mismatch!"
    assert ds["recovered_span"]["duplicate_timestamps"] == 0, "Duplicate timestamps detected!"
    assert ds["recovered_span"]["non_null_volume_rows"] == 0, "Unexpected non-null volume rows!"
    assert len(ds["unexplained_intraday_gaps_over_600s"]) == 0, "Unexplained intraday gaps present!"

    expected_counts = {"W1": 118, "W2": 131, "W3": 143}
    for wid, exp_n in expected_counts.items():
        w = rec[wid]
        assert w["app_trades"] == exp_n and w["ind_trades"] == exp_n, f"{wid} count mismatch: {w}"
        assert w["mismatch_count"] == 0, f"{wid} has trade mismatches: {w['mismatch_count']}"
        assert w["trailing_journey_mismatches"] == 0, f"{wid} has trailing journey mismatches!"

    FROZEN_V2_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Exact strategy & cost configuration snapshot
    cfg_snapshot = {
        "baseline_version": "RECOVERED_FULL_HISTORY_BASELINE_V2",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": "XAUUSD",
        "semantics_version": "QUOTE_ARRIVAL_SEMANTICS_V1",
        "safety_locks": {
            "SHADOW_MODE": bool(settings.SHADOW_MODE),
            "TRADING_MODE": str(settings.TRADING_MODE),
            "BROKER_TYPE": str(settings.BROKER_TYPE),
        },
        "windows": {
            "W1_original_task4": {
                "warmup_start_utc": "2026-08-20T00:00:00+00:00",
                "replay_start_utc": "2026-08-24T00:00:00+00:00",
                "replay_end_utc": "2026-11-16T20:01:23+00:00",
                "expected_trades": 118,
            },
            "W2_recovered_original_span": {
                "warmup_start_utc": "2026-08-09T00:00:00+00:00 (DB data starts 2026-08-13T00:00:00.153Z)",
                "replay_start_utc": "2026-08-13T00:00:00+00:00",
                "replay_end_utc": "2026-11-16T20:01:23+00:00",
                "expected_trades": 131,
            },
            "W3_recovered_warmed_full_history": {
                "warmup_start_utc": "2026-08-13T00:00:00+00:00",
                "warmup_end_utc": "2026-08-17T00:00:00+00:00",
                "replay_start_utc": "2026-08-17T00:00:00+00:00",
                "replay_end_utc": "2026-11-18T18:00:00+00:00 (DB end 2026-11-18T18:09:38.022225Z)",
                "expected_trades": 143,
            },
        },
        "risk_profile": asdict(MEDIUM_RISK_BACKTEST),
        "execution_and_indicators": {
            "initial_capital": 10000.0,
            "adapt_to_volume": False,
            "use_tick_intensity_features": True,
            "use_kalman_exit": False,
            "session_close_utc": None,
            "early_fail_mins": None,
            "early_fail_min_mfe_atr": None,
            "cooldown_bars": 15,
            "trailing_be_atr": 1.0,
            "trailing_t1_trigger_atr": 1.6,
            "trailing_t1_dist_atr": 1.10,
            "trailing_t2_trigger_atr": 2.5,
            "trailing_t2_dist_atr": 0.75,
            "effective_max_hold_london_ny": 60,
            "effective_max_hold_offhours": 90,
            "time_squeeze_start_ratio": 0.75,
            "slippage_pips": 0.0533,
            "apply_swap": True,
        },
        "commission_schedule": asdict(FUSION_CTRADER_DEMO_48439552),
        "swap_schedule": asdict(FUSION_XAUUSD_SWAP_SPEC),
    }
    cfg_path = FROZEN_V2_DIR / "strategy_config.json"
    cfg_path.write_text(json.dumps(cfg_snapshot, indent=2, default=str), encoding="utf-8")

    # 2. Freeze trade ledgers, journeys, reconciliation CSVs, and performance tables
    files_to_copy = [
        ("w1_trades.csv", AUDIT_DIR / "task6c_app_w1_trades.csv"),
        ("w1_journeys.json", AUDIT_DIR / "task6c_app_w1_journeys.json"),
        ("w1_reconciliation.csv", AUDIT_DIR / "task6c_reconciliation_w1.csv"),
        ("w2_trades.csv", AUDIT_DIR / "task6c_app_w2_trades.csv"),
        ("w2_journeys.json", AUDIT_DIR / "task6c_app_w2_journeys.json"),
        ("w2_reconciliation.csv", AUDIT_DIR / "task6c_reconciliation_w2.csv"),
        ("w3_trades.csv", AUDIT_DIR / "task6c_app_w3_trades.csv"),
        ("w3_journeys.json", AUDIT_DIR / "task6c_app_w3_journeys.json"),
        ("w3_reconciliation.csv", AUDIT_DIR / "task6c_reconciliation_w3.csv"),
        ("performance_and_risk.csv", AUDIT_DIR / "task6c_performance_and_risk.csv"),
        ("dataset_integrity.json", AUDIT_DIR / "task6c_dataset_integrity.json"),
        ("validation_reconciliation_summary.json", AUDIT_DIR / "task6c_validation_summary.json"),
        ("app_backtest_summary.json", AUDIT_DIR / "task6c_app_summary.json"),
    ]
    frozen_artifacts = {"strategy_config.json": _sha256(cfg_path)}
    for dest_name, src_path in files_to_copy:
        dest = FROZEN_V2_DIR / dest_name
        shutil.copy2(src_path, dest)
        frozen_artifacts[dest_name] = _sha256(dest)

    source_hashes = {rel: _sha256(_ROOT / rel) for rel in SOURCE_FILES}

    manifest = {
        "baseline_version": "RECOVERED_FULL_HISTORY_BASELINE_V2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "prior_frozen_task4_preserved": {
            "frozen_111_baseline_trades_sha256": _sha256(_ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"),
            "task4_run_1_sha256": _sha256(AUDIT_DIR / "task4_run_1.csv"),
            "task4_baseline_manifest_sha256": _sha256(AUDIT_DIR / "task4_baseline_manifest.json"),
            "task4_warmup_rows": ds["task4_preservation"]["actual_warmup_rows"],
            "task4_warmup_legacy_sha256": ds["task4_preservation"]["actual_warmup_legacy_t4_sha256"],
            "task4_replay_rows": ds["task4_preservation"]["actual_replay_rows"],
            "task4_replay_legacy_sha256": ds["task4_preservation"]["actual_replay_legacy_t4_sha256"],
        },
        "dataset_fingerprints": ds["window_fingerprints"],
        "application_summary": app_sum,
        "reconciliation_summary": rec,
        "frozen_artifacts_sha256": frozen_artifacts,
        "source_files_sha256": source_hashes,
    }
    manifest_path = FROZEN_V2_DIR / "task6c_frozen_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[FREEZE V2 COMPLETE] Manifest written to {manifest_path} (sha256={_sha256(manifest_path)})")


if __name__ == "__main__":
    main()
