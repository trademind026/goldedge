"""
Task 13 main driver: baseline verification, pass-1 screening, classification, pass-2 verification,
HWM / cost-stress evaluation, and deliverable generation (JSON/CSV). Zero src/goldedge imports.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

import task13_entry_engine as eng
from task13_entry_engine import (
    AUDIT_DIR, FROZEN_V2_DIR, OUT_DIR, WINDOWS, H0_NAME, H4A_NAME, H0_APP, H4A_APP,
    FAMILIES, build_configs, run_window, summ, dir_pnl, paired_daily, ex_top, bh, _dump, t11a, t12p1, t12p2,
)

REFS = {
    "W1": {H0_NAME: (118, 116.54), H4A_NAME: (82, 892.29)},
    "W2": {H0_NAME: (131, 151.69), H4A_NAME: (91, 929.38)},
    "W3": {H0_NAME: (143, 211.48), H4A_NAME: (99, 1257.70)},
}
W3_REF_FULL = {H0_NAME: {"n": 143, "net": 211.48, "pf": 1.0414, "mtm": 12.78},
               H4A_NAME: {"n": 99, "net": 1257.70, "pf": 1.4005, "mtm": 6.18}}


def window_row(pf: Any) -> dict[str, Any]:
    s = summ(pf)
    bn, bp = dir_pnl(pf, "BUY")
    sn, sp = dir_pnl(pf, "SELL")
    return {"n": s["n"], "wr_pct": s["wr_pct"], "net_pnl": s["net_pnl"], "pf": s["pf"], "exp": s["exp"],
            "closed_dd": s["closed_dd"], "mtm_dd_pct": s["mtm_dd_pct"], "buy_n": bn, "buy_pnl": bp, "sell_n": sn, "sell_pnl": sp}


def key_set(pf: Any) -> dict[tuple, dict]:
    return {(t["entry_time"], t["direction"]): t for t in pf.trades}


def classify(code: str, base: str, res: dict[str, dict], pvals: dict[str, float]) -> dict[str, Any]:
    name = f"{code}_{base}"
    base_name = H0_NAME if base == "H0" else H4A_NAME
    cand = {w: res[w]["A"][name] for w in ("W1", "W2", "W3")}
    bas = {w: res[w]["A"][base_name] for w in ("W1", "W2", "W3")}
    cs = {w: summ(cand[w]) for w in cand}
    bs = {w: summ(bas[w]) for w in bas}
    imp_all = all(cs[w]["net_pnl"] > bs[w]["net_pnl"] for w in cs)
    pf_ok = cs["W3"]["pf"] > bs["W3"]["pf"]
    dd_ok = cs["W3"]["mtm_dd_pct"] <= bs["W3"]["mtm_dd_pct"] + 0.25
    n_ok = cs["W3"]["n"] >= 60
    cb = dir_pnl(cand["W3"], "BUY")[1]
    cse = dir_pnl(cand["W3"], "SELL")[1]
    bb = dir_pnl(bas["W3"], "BUY")[1]
    bse = dir_pnl(bas["W3"], "SELL")[1]
    dir_ok = (cb >= bb - 150.0) and (cse >= bse - 150.0)
    nb = [res["W3"]["A"][f"{code}n{i}_{base}"] for i in (1, 2)]
    nbs = [summ(x) for x in nb]
    plateau = all(x["net_pnl"] > bs["W3"]["net_pnl"] and x["pf"] > bs["W3"]["pf"] for x in nbs)
    w3_imp = cs["W3"]["net_pnl"] > bs["W3"]["net_pnl"]
    if not w3_imp:
        cls = "REJECTED"
    elif imp_all and pf_ok and dd_ok and n_ok and dir_ok and plateau:
        cls = "PROMISING_SHADOW_CANDIDATE"
    elif imp_all and pf_ok:
        cls = "EXPLORATORY"
    else:
        cls = "REQUIRES_MORE_DATA"
    return {"name": name, "cls": cls, "imp_all_windows": imp_all, "pf_ok": pf_ok, "dd_ok": dd_ok, "n_ok": n_ok,
            "dir_ok": dir_ok, "plateau": plateau, "neighbour_net": [x["net_pnl"] for x in nbs],
            "neighbour_pf": [x["pf"] for x in nbs], "neighbour_names": [f"{code}n{i}_{base}" for i in (1, 2)]}


def main() -> None:
    smoke = bool(os.environ.get("T13_SMOKE"))
    windows = list(WINDOWS)
    if smoke:
        from datetime import timedelta
        windows = [(w, s, s + timedelta(days=int(os.environ.get("T13_SMOKE_DAYS", "4")))) for w, s, e in WINDOWS]
        windows[0] = (windows[0][0], windows[0][1] + timedelta(days=14), windows[0][2] + timedelta(days=14))
        windows[1] = (windows[1][0], windows[1][1] + timedelta(days=14), windows[1][2] + timedelta(days=14))
        windows[2] = (windows[2][0], windows[2][1] + timedelta(days=14), windows[2][2] + timedelta(days=14))
    conn = psycopg2.connect(os.environ.get("DB_URL", "postgresql://postgres:password@db:5432/goldedge"))

    # ---------------------------------------------------------------- Phase 1: baseline fingerprints
    v2 = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
    t4_hash = t12p1._sha256(eng.ROOT / "backtest_results" / "frozen_111_baseline_trades.csv")
    hashes = {
        "task4": (t4_hash, v2["prior_frozen_task4_preserved"]["frozen_111_baseline_trades_sha256"].upper()),
        "w1": (t12p1._sha256(FROZEN_V2_DIR / "w1_trades.csv"), v2["frozen_artifacts_sha256"]["w1_trades.csv"].upper()),
        "w2": (t12p1._sha256(FROZEN_V2_DIR / "w2_trades.csv"), v2["frozen_artifacts_sha256"]["w2_trades.csv"].upper()),
        "w3": (t12p1._sha256(FROZEN_V2_DIR / "w3_trades.csv"), v2["frozen_artifacts_sha256"]["w3_trades.csv"].upper()),
    }
    for k, (got, exp) in hashes.items():
        assert got == exp, f"Frozen hash mismatch for {k}"
    prior = {}
    for fn in ("task11a_validation_tests.json",):
        prior[fn] = json.loads((AUDIT_DIR / fn).read_text(encoding="utf-8")).get("all_checks_passed")
    t12_rec = json.loads((AUDIT_DIR / "task12_session_analysis" / "task12_independent_reconciliation.json").read_text(encoding="utf-8"))
    prior["task12_independent_reconciliation"] = t12_rec["summary"]["all_reconciled"]
    settings_text = (eng.ROOT / "src" / "goldedge" / "config" / "settings.py").read_text(encoding="utf-8")
    import re
    safety = {
        "SHADOW_MODE_True": bool(re.search(r"SHADOW_MODE:\s*bool\s*=\s*Field\(\s*True\b", settings_text)),
        "TRADING_MODE_demo": bool(re.search(r'TRADING_MODE:\s*str\s*=\s*Field\(\s*"demo"', settings_text)),
        "HWM_8pct": bool(re.search(r"HWM_MAX_DRAWDOWN_PCT:\s*float\s*=\s*Field\(\s*8\.0\b", settings_text)),
    }
    assert all(safety.values()) and all(prior.values())

    # ---------------------------------------------------------------- Pass 1
    res1: dict[str, dict] = {}
    for wid, s_dt, e_dt in windows:
        cfgs = build_configs(wid)
        res1[wid] = run_window(conn, wid, s_dt, e_dt, cfgs, set(), collect_funnel=(wid == "W3"))

    # Baseline reproduction (stop on unexplained difference)
    repro: dict[str, Any] = {}
    for wid in ("W1", "W2", "W3"):
        repro[wid] = {}
        for nm in (H0_NAME, H4A_NAME):
            pf = res1[wid]["A"][nm]
            s = summ(pf)
            en, enet = REFS[wid][nm]
            repro[wid][nm] = {"n": s["n"], "net_pnl": s["net_pnl"], "pf": s["pf"], "mtm_dd_pct": s["mtm_dd_pct"],
                              "ref_n": en, "ref_net": enet, "n_match": s["n"] == en, "net_diff": round(abs(s["net_pnl"] - enet), 4)}
            assert smoke or (s["n"] == en and abs(s["net_pnl"] - enet) <= 0.01), f"Baseline reproduction mismatch {wid}/{nm}: {s}"
    for nm, ref in W3_REF_FULL.items():
        s = summ(res1["W3"]["A"][nm])
        assert smoke or (abs(s["pf"] - ref["pf"]) <= 5e-4 and abs(s["mtm_dd_pct"] - ref["mtm"]) <= 0.01), (nm, s)
    _dump(OUT_DIR / "task13_baseline_verification.json", {
        "frozen_hashes": {k: {"computed": g, "expected": e, "match": g == e} for k, (g, e) in hashes.items()},
        "prior_verification_flags": prior, "safety_locks": safety,
        "dataset_fingerprints": v2["dataset_fingerprints"],
        "reproduction": repro, "w3_reference_table": W3_REF_FULL,
        "application_semantics_variants": {
            w: {nm: {k: window_row(res1[w]["A"][nm])[k] for k in ("n", "net_pnl", "pf", "mtm_dd_pct")} for nm in (H0_APP, H4A_APP)}
            for w in ("W1", "W2", "W3")},
    })

    # ---------------------------------------------------------------- Hypothesis table
    fam = {code: (kind, v, nb, desc) for code, kind, v, nb, desc in FAMILIES}
    main_rows: list[dict[str, Any]] = []
    pvals: dict[str, float] = {}
    paired: dict[str, dict] = {}
    for code, *_ in FAMILIES:
        for base in ("H0", "H4a"):
            nm = f"{code}_{base}"
            base_nm = H0_NAME if base == "H0" else H4A_NAME
            paired[nm] = paired_daily(res1["W3"]["A"][nm], res1["W3"]["A"][base_nm])
            pvals[nm] = paired[nm]["wilcoxon_p"]
    names_main = list(pvals.keys())
    qvals = dict(zip(names_main, bh([pvals[n] for n in names_main])))

    cls_info: dict[str, dict] = {}
    for code, *_ in FAMILIES:
        for base in ("H0", "H4a"):
            cls_info[f"{code}_{base}"] = classify(code, base, res1, pvals)

    ctrl_names = [H0_NAME, H4A_NAME, H0_APP, H4A_APP]
    all_rows_names = ctrl_names + names_main
    for nm in all_rows_names:
        grp = "control" if nm in ctrl_names else "hypothesis"
        base = "H0" if (nm.endswith("_H0") or nm in (H0_NAME, H0_APP)) else "H4a"
        row: dict[str, Any] = {"hypothesis": nm, "group": grp, "base": base}
        if grp == "hypothesis":
            code = nm.split("_")[0]
            row["family"] = code
            row["rule"] = res1["W3"]["A"][nm].cfg.rule_desc
        for wid in ("W1", "W2", "W3"):
            r = window_row(res1[wid]["A"][nm])
            for k, v in r.items():
                row[f"{wid.lower()}_{k}"] = v
        pf3 = res1["W3"]["A"][nm]
        row.update({f"w3_{k}": v for k, v in ex_top(pf3).items()})
        if grp == "hypothesis":
            base_nm = H0_NAME if base == "H0" else H4A_NAME
            bset = key_set(res1["W3"]["A"][base_nm])
            cset = key_set(pf3)
            rem = [t for k, t in bset.items() if k not in cset]
            add = [t for k, t in cset.items() if k not in bset]
            row["w3_removed_vs_base_n"] = len(rem)
            row["w3_removed_winners"] = sum(1 for t in rem if t["net_pnl"] > 0)
            row["w3_removed_losers"] = sum(1 for t in rem if t["net_pnl"] <= 0)
            row["w3_removed_net_pnl"] = round(sum(t["net_pnl"] for t in rem), 2)
            row["w3_added_vs_base_n"] = len(add)
            row["w3_added_net_pnl"] = round(sum(t["net_pnl"] for t in add), 2)
            row["w3_delta_vs_base_net"] = round(row["w3_net_pnl"] - summ(res1["W3"]["A"][base_nm])["net_pnl"], 2)
            row["w3_delta_vs_H0_net"] = round(row["w3_net_pnl"] - summ(res1["W3"]["A"][H0_NAME])["net_pnl"], 2)
            row["w3_delta_vs_H4a_net"] = round(row["w3_net_pnl"] - summ(res1["W3"]["A"][H4A_NAME])["net_pnl"], 2)
            p = paired[nm]
            row.update({"paired_days": p["days"], "paired_mean_daily_diff": p["mean_daily_diff"],
                        "paired_ci95_lo": p["ci95_lo"], "paired_ci95_hi": p["ci95_hi"],
                        "wilcoxon_p": p["wilcoxon_p"], "bh_q": qvals[nm]})
            ci = cls_info[nm]
            row.update({"classification": ci["cls"], "improves_all_windows": ci["imp_all_windows"], "pf_ok": ci["pf_ok"],
                        "dd_ok": ci["dd_ok"], "n_ok": ci["n_ok"], "direction_ok": ci["dir_ok"], "plateau": ci["plateau"],
                        "neighbour_w3_net": json.dumps(ci["neighbour_net"])})
        main_rows.append(row)

    # Parameter sensitivity rows (W3)
    sens_rows = []
    for code, kind, v, nb, desc in FAMILIES:
        for base in ("H0", "H4a"):
            for label, nm, val in ((f"minus/lo", f"{code}n1_{base}", nb[0]), ("main", f"{code}_{base}", v), ("plus/hi", f"{code}n2_{base}", nb[1])):
                s = window_row(res1["W3"]["A"][nm])
                sens_rows.append({"family": code, "kind": kind, "base": base, "variant": label, "param": val, "config": nm,
                                  **{f"w3_{k}": x for k, x in s.items()}})
    pd.DataFrame(sens_rows).to_csv(OUT_DIR / "task13_parameter_sensitivity.csv", index=False)

    # ---------------------------------------------------------------- Retention rule (pre-registered)
    prom = [n for n, c in cls_info.items() if c["cls"] == "PROMISING_SHADOW_CANDIDATE"]
    expl = sorted([n for n, c in cls_info.items() if c["cls"] == "EXPLORATORY"],
                  key=lambda n: -summ(res1["W3"]["A"][n])["net_pnl"])[:2]
    retained = sorted(set(prom + expl), key=lambda n: -summ(res1["W3"]["A"][n])["net_pnl"])[:6]

    # ---------------------------------------------------------------- Pass 2
    cfg_by_name = {c.name: c for c in build_configs("W3")}
    p2: list[eng.EConfig] = [cfg_by_name[n] for n in ctrl_names] + [cfg_by_name[n] for n in retained]
    hwm_refs = [H0_NAME, H4A_NAME] + retained
    for n in hwm_refs:
        p2.append(dataclasses.replace(cfg_by_name[n], name=f"{n}__HWM8", group="hwm8", max_hwm_dd_pct=8.0, ref_name=n))
    cost_refs = [H0_NAME, H4A_NAME] + retained
    for n in cost_refs:
        p2.append(dataclasses.replace(cfg_by_name[n], name=f"{n}__COST15", group="cost_stress", slip_pips=0.080, comm_rate_per_m=15.0, ref_name=n))
    verify = {c.name for c in p2}
    res2: dict[str, dict] = {}
    for wid, s_dt, e_dt in windows:
        res2[wid] = run_window(conn, wid, s_dt, e_dt, p2, verify, collect_funnel=False)
    conn.close()

    # Determinism: pass-1 vs pass-2 stream A, and A vs B reconciliation
    recon: dict[str, Any] = {"windows": {}, "determinism_pass1_vs_pass2": {}}
    tot = ok_pairs = tot_tr = tot_su = 0
    for wid in ("W1", "W2", "W3"):
        recon["windows"][wid] = {}
        recon["determinism_pass1_vs_pass2"][wid] = {}
        for c in p2:
            nm = c.name
            a_pf, b_pf = res2[wid]["A"][nm], res2[wid]["B"][nm]
            at, bt = a_pf.trades, b_pf.trades
            matched = stop_mm = qty_mm = cost_mm = 0
            max_pnl = max_eq = 0.0
            su_cnt = 0
            for i in range(min(len(at), len(bt))):
                a, b = at[i], bt[i]
                pd_ = abs(a["net_pnl"] - b["net_pnl"])
                ed_ = abs(a["equity_after"] - b["equity_after"])
                max_pnl, max_eq = max(max_pnl, pd_), max(max_eq, ed_)
                if abs(a["qty"] - b["qty"]) > 1e-6:
                    qty_mm += 1
                if abs(a["commission_total"] - b["commission_total"]) > 1e-6 or abs(a["slippage_usd"] - b["slippage_usd"]) > 1e-6:
                    cost_mm += 1
                su_ok = len(a["stop_updates"]) == len(b["stop_updates"]) and all(
                    abs(x["old_sl"] - y["old_sl"]) <= 1e-5 and abs(x["new_sl"] - y["new_sl"]) <= 1e-5 and x["flag"] == y["flag"]
                    for x, y in zip(a["stop_updates"], b["stop_updates"]))
                if not su_ok:
                    stop_mm += 1
                su_cnt += len(b["stop_updates"])
                if (a["entry_time"] == b["entry_time"] and a["exit_time"] == b["exit_time"] and a["direction"] == b["direction"]
                        and a["exit_reason"] == b["exit_reason"] and a["signal_time"] == b["signal_time"]
                        and abs(a["entry_price"] - b["entry_price"]) <= 1e-9 and abs(a["exit_price"] - b["exit_price"]) <= 1e-9
                        and pd_ <= 1e-6 and su_ok):
                    matched += 1
            missing, extra = max(0, len(at) - len(bt)), max(0, len(bt) - len(at))
            mtm_diff = round(abs(a_pf.mtm.max_dd_pct - b_pf.mtm.max_dd_pct), 6)
            halt_a, halt_b = a_pf.halt_info, b_pf.halt_info
            halt_ok = (halt_a is None) == (halt_b is None) and (halt_a is None or halt_a["trigger_time_utc"] == halt_b["trigger_time_utc"])
            ok = (missing == 0 and extra == 0 and matched == len(bt) and stop_mm == 0 and qty_mm == 0 and cost_mm == 0
                  and max_pnl <= 1e-6 and max_eq <= 1e-6 and mtm_diff <= 1e-6 and halt_ok
                  and a_pf.constraint_violations == 0 and b_pf.constraint_violations == 0)
            tot += 1
            ok_pairs += int(ok)
            tot_tr += matched
            tot_su += su_cnt
            recon["windows"][wid][nm] = {
                "status": "FULLY_RECONCILED" if ok else "MISMATCH_FOUND", "matched_trades": matched, "total_trades": len(bt),
                "missing_trades": missing, "extra_trades": extra, "stop_update_mismatches": stop_mm,
                "stop_updates_verified": su_cnt, "qty_mismatches": qty_mm, "cost_mismatches": cost_mm,
                "max_net_pnl_diff_usd": round(max_pnl, 6), "max_equity_path_diff_usd": round(max_eq, 6),
                "mtm_dd_pct_diff": mtm_diff, "hwm_trigger_match": halt_ok,
                "portfolio_constraint_violations": a_pf.constraint_violations + b_pf.constraint_violations,
            }
            if nm in res1[wid]["A"]:
                p1 = res1[wid]["A"][nm]
                same = (len(p1.trades) == len(at) and all(
                    x["entry_time"] == y["entry_time"] and abs(x["net_pnl"] - y["net_pnl"]) <= 1e-9 for x, y in zip(p1.trades, at)))
                recon["determinism_pass1_vs_pass2"][wid][nm] = bool(same)
    recon["summary"] = {"total_config_window_pairs": tot, "fully_reconciled_pairs": ok_pairs,
                        "unverified_or_mismatched_pairs": tot - ok_pairs, "total_trades_reconciled": tot_tr,
                        "total_stop_updates_verified": tot_su, "all_reconciled": tot == ok_pairs,
                        "pass1_pass2_deterministic": all(all(v.values()) for v in recon["determinism_pass1_vs_pass2"].values()),
                        "feature_independence_note": ("Kalman, bar aggregation, regime detection, sizing, execution, costs, stops and equity are "
                                                      "independently implemented in streams A and B; entry-feature formulas (compute_fx, score_components) "
                                                      "and the t6c indicator/signal functions are shared by both streams.")}
    _dump(OUT_DIR / "task13_independent_reconciliation.json", recon)

    # ---------------------------------------------------------------- HWM comparison
    hwm: dict[str, Any] = {}
    for wid in ("W1", "W2", "W3"):
        hwm[wid] = {}
        for n in hwm_refs:
            u, p = res2[wid]["A"][n], res2[wid]["A"][f"{n}__HWM8"]
            loss_by_sess: dict[str, float] = {}
            for t in p.trades:
                if t["net_pnl"] < 0:
                    loss_by_sess[t["dst_session"]] = round(loss_by_sess.get(t["dst_session"], 0.0) + t["net_pnl"], 2)
            hwm[wid][f"{n}__HWM8"] = {
                "unprotected_ref": n, "hwm_triggered": p.halted, "halt_details": p.halt_info,
                "trades_executed": len(p.trades), "unprotected_trades": len(u.trades),
                "trades_prevented_by_halt": len(u.trades) - len(p.trades),
                "qualified_signals_blocked_post_halt": p.prevented_signals_after_halt,
                "net_pnl_protected_usd": round(sum(t["net_pnl"] for t in p.trades), 2),
                "net_pnl_unprotected_usd": round(sum(t["net_pnl"] for t in u.trades), 2),
                "mtm_dd_protected_pct": round(p.mtm.max_dd_pct, 2), "mtm_dd_unprotected_pct": round(u.mtm.max_dd_pct, 2),
                "losing_dollars_by_dst_session": loss_by_sess,
                "count_definitions": ("executed = trades closed incl. the trigger trade; prevented trades = unprotected trades minus executed (counterfactual); "
                                      "signals blocked = qualified post-halt 5m signals rejected by halt"),
            }
    _dump(OUT_DIR / "task13_hwm_comparison.json", hwm)

    # ---------------------------------------------------------------- Cost-stress rows appended to hypothesis CSV
    for n in cost_refs:
        row = {"hypothesis": f"{n}__COST15", "group": "cost_stress", "base": "H0" if "_H0" in n or n == H0_NAME else "H4a"}
        for wid in ("W1", "W2", "W3"):
            for k, v in window_row(res2[wid]["A"][f"{n}__COST15"]).items():
                row[f"{wid.lower()}_{k}"] = v
        main_rows.append(row)
    pd.DataFrame(main_rows).to_csv(OUT_DIR / "task13_entry_hypothesis_results.csv", index=False)

    # ---------------------------------------------------------------- Statistical validation
    sig = [n for n in names_main if qvals[n] < 0.05]
    stat = {
        "method": "Paired daily P&L difference vs same-base control (W3), Wilcoxon signed-rank; Benjamini-Hochberg FDR across 26 main hypotheses",
        "n_hypotheses": len(names_main), "n_significant_fdr_005": len(sig), "significant": sig,
        "min_raw_p": min(pvals.values()), "min_q": min(qvals.values()),
        "window_overlap_warning": "W1 is a subset of W2 and W2 a subset of W3 (same ticks); agreement across windows is NOT independent out-of-sample confirmation.",
        "retention_rule": ("PROMISING_SHADOW_CANDIDATE = improves net P&L in W1,W2,W3 vs same-base control AND W3 PF higher AND W3 MTM DD <= control+0.25pp "
                           "AND W3 n>=60 AND neither direction worse than control by >$150 AND both parameter neighbours beat control on W3 net and PF. "
                           "EXPLORATORY = improves all windows and PF but fails another test. REQUIRES_MORE_DATA = W3 net improves but fails window/PF consistency. "
                           "REJECTED = W3 net not above control. Retained for verification = all PROMISING + top-2 EXPLORATORY by W3 net (max 6)."),
        "per_hypothesis": {n: {**paired[n], "q": qvals[n], **{k: cls_info[n][k] for k in ("cls", "imp_all_windows", "pf_ok", "dd_ok", "n_ok", "dir_ok", "plateau")},
                               "neighbour_net": cls_info[n]["neighbour_net"],
                               "w3_n": summ(res1["W3"]["A"][n])["n"], **ex_top(res1["W3"]["A"][n])} for n in names_main},
        "retained": retained,
        "cost_stress_w3": {n: {"net": window_row(res2["W3"]["A"][f"{n}__COST15"])["net_pnl"], "pf": window_row(res2["W3"]["A"][f"{n}__COST15"])["pf"],
                               "mtm_dd_pct": window_row(res2["W3"]["A"][f"{n}__COST15"])["mtm_dd_pct"]} for n in cost_refs},
        "low_power_note": "Daily paired differences over ~65 trading days with mostly zero-difference days give low power; non-significance is not evidence of no effect.",
    }
    _dump(OUT_DIR / "task13_statistical_validation.json", stat)

    # ---------------------------------------------------------------- Funnel + dataset
    fun = pd.DataFrame(res1["W3"]["funnel"])
    fun.to_csv(OUT_DIR / "task13_signal_decision_funnel.csv", index=False)
    counters = res1["W3"]["counters"]

    h0_t = res1["W3"]["A"][H0_NAME].trades
    h4_t = res1["W3"]["A"][H4A_NAME].trades
    h4_keys = {(t["entry_time"], t["direction"]) for t in h4_t}
    h0_keys = {(t["entry_time"], t["direction"]) for t in h0_t}
    rows = []
    for t in h0_t:
        r = {k: v for k, v in t.items() if k not in ("stop_updates",)}
        r["variant_membership"] = "H0_and_H4a" if (t["entry_time"], t["direction"]) in h4_keys else "H0_only"
        rows.append(r)
    for t in h4_t:
        if (t["entry_time"], t["direction"]) not in h0_keys:
            r = {k: v for k, v in t.items() if k not in ("stop_updates",)}
            r["variant_membership"] = "H4a_only_cascade"
            rows.append(r)
    ds = pd.DataFrame(rows)
    sess = [t12p1.classify_dst_session(datetime.fromisoformat(x)) for x in ds["entry_time"]]
    ds["first30_any"] = [s["is_first_30m_any"] for s in sess]
    ds["session"] = [s["dst_session"] for s in sess]
    ds.to_csv(OUT_DIR / "task13_entry_feature_dataset.csv", index=False)

    analysis_inputs = {"counters": counters, "retained": retained, "cls": {n: c["cls"] for n, c in cls_info.items()},
                       "h0_app_vs_research": {w: {"H0_research": window_row(res1[w]["A"][H0_NAME]), "H0_app": window_row(res1[w]["A"][H0_APP]),
                                                    "H4a_research": window_row(res1[w]["A"][H4A_NAME]), "H4a_app": window_row(res1[w]["A"][H4A_APP])}
                                              for w in ("W1", "W2", "W3")}}
    _dump(OUT_DIR / "_task13_run_summary.json", analysis_inputs)

    print("\n[TASK 13 ENGINE COMPLETE]")
    print("Reconciliation:", json.dumps(recon["summary"], indent=2))
    print("Retained:", retained)
    for r in main_rows:
        if r["group"] in ("control", "hypothesis"):
            print(f"  {r['hypothesis']:16s} {r.get('classification', ''):28s} W1={r['w1_net_pnl']:8.1f} W2={r['w2_net_pnl']:8.1f} "
                  f"W3 n={r['w3_n']:3d} net={r['w3_net_pnl']:8.1f} pf={r['w3_pf']:6.3f} dd={r['w3_mtm_dd_pct']:5.2f} "
                  f"p={r.get('wilcoxon_p', '')} q={r.get('bh_q', '')}")


if __name__ == "__main__":
    main()
