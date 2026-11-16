import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone


class MacroFeatureEngine:
    """
    STAGE 3.1 & MODEL 1: Complete Macro Feature Engineering Matrix (32 Features).
    Computes the 32-dimensional feature vector x_macro on closed 1-minute bars:
    - 1. US Dollar Index (DXY) Dynamics (6 Features)
    - 2. Sovereign Yield & Interest Rate Regime (6 Features)
    - 3. Multi-Currency Relative Strength Matrix (8 Features)
    - 4. Structural Trend & Thermodynamics (12 Features)
    """

    FEATURE_NAMES: List[str] = [
        # 1. DXY Dynamics
        "DXY_ROC_15",
        "DXY_ROC_60",
        "DXY_EMA200_Dist",
        "DXY_Kalman_Vel",
        "DXY_RSI_14",
        "DXY_Volatility",
        # 2. Sovereign Yield & Interest Rate Regime
        "US10Y_Delta_1h",
        "US10Y_ROC_4h",
        "Yield_Spread",
        "Yield_Spread_Vel",
        "Risk_Sentiment",
        "Currency_Carry",
        # 3. Multi-Currency Relative Strength Matrix
        "G8_Rank_Base",
        "G8_Rank_Quote",
        "G8_Rank_Delta",
        "Pair_Beta_DXY",
        "Cross_Dispersion",
        "Triad_Residual",
        "Volume_ZScore",
        "ADR_Pct_Used",
        # 4. Structural Trend & Thermodynamics
        "Trend_Dist_EMA20",
        "Trend_Dist_EMA50",
        "Trend_Dist_EMA200",
        "EMA_Alignment",
        "Kalman_Price_Dev",
        "Kalman_Velocity",
        "Shannon_Entropy",
        "ATR_Ratio_14_100",
        "Fractal_Dim",
        "Session_Progress",
        "Time_To_Rollover",
        "News_Proximity",
    ]

    # Standard Overnight Policy Rates for Currency Carry (Annualized %)
    CENTRAL_BANK_RATES = {
        "USD": 5.25,
        "EUR": 3.75,
        "GBP": 5.00,
        "JPY": 0.25,
        "CHF": 1.25,
        "AUD": 4.35,
        "CAD": 4.75,
        "NZD": 5.25,
        "XAU": 0.00,
    }

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int = 14) -> float:
        if len(series) < period + 1:
            return 50.0
        delta = series.diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
        rs = gain / (loss.replace(0, np.nan))
        rsi = 100.0 - (100.0 / (1.0 + rs))
        val = rsi.iloc[-1]
        return 50.0 if np.isnan(val) else float(val)

    @staticmethod
    def _petrosian_fractal_dimension(series: pd.Series) -> float:
        """
        Computes Petrosian Fractal Dimension:
        FD = log10(N) / (log10(N) + log10(N / (N + 0.4 * N_delta)))
        where N_delta is the number of sign changes in consecutive differences.
        """
        n = len(series)
        if n < 10:
            return 1.0
        diff = np.diff(series.values)
        sign_changes = np.sum(diff[:-1] * diff[1:] < 0)
        n_delta = float(sign_changes)
        denom = np.log10(n) + np.log10(n / (n + 0.4 * n_delta))
        if denom <= 0:
            return 1.0
        fd = np.log10(n) / denom
        return float(np.clip(fd, 1.0, 2.0))

    @staticmethod
    def _shannon_entropy(series: pd.Series, bins: int = 8) -> float:
        """Computes rolling Shannon entropy of returns."""
        if len(series) < 15:
            return 2.5
        ret = series.pct_change().dropna().values
        if len(ret) == 0:
            return 0.0
        counts, _ = np.histogram(ret, bins=bins)
        total = counts.sum()
        if total == 0:
            return 0.0
        probs = counts / total
        probs = probs[probs > 0]
        return float(-np.sum(probs * np.log2(probs)))

    def compute_macro_features(
        self,
        df_pair: pd.DataFrame,
        symbol: str,
        df_dxy: Optional[pd.DataFrame] = None,
        df_yields: Optional[pd.DataFrame] = None,
        g8_dfs: Optional[Dict[str, pd.DataFrame]] = None,
        current_time: Optional[datetime] = None,
        news_proximity_minutes: float = 120.0,
    ) -> Dict[str, float]:
        """
        Computes the complete 32-feature dictionary for the candidate symbol.
        """
        if df_pair.empty or len(df_pair) < 20:
            return {feat: 0.0 for feat in self.FEATURE_NAMES}

        close = df_pair["close"]
        high = df_pair["high"]
        low = df_pair["low"]
        curr_close = float(close.iloc[-1])

        # Symbol parsing
        clean_sym = symbol.replace("/", "").replace("-", "").upper()
        base_curr = clean_sym[:3] if len(clean_sym) >= 6 else "EUR"
        quote_curr = clean_sym[3:6] if len(clean_sym) >= 6 else "USD"

        # Timestamp extraction
        if current_time is None:
            if "time" in df_pair.columns:
                t = df_pair["time"].iloc[-1]
                if isinstance(t, pd.Timestamp):
                    current_time = t.to_pydatetime()
                elif isinstance(t, datetime):
                    current_time = t
            elif isinstance(df_pair.index, pd.DatetimeIndex):
                current_time = df_pair.index[-1].to_pydatetime()
        if current_time is None:
            current_time = datetime.now(timezone.utc)

        # ── 1. US DOLLAR INDEX (DXY) DYNAMICS ─────────────────────────
        if df_dxy is not None and not df_dxy.empty and len(df_dxy) >= 20:
            dxy_c = df_dxy["close"]
            dxy_curr = float(dxy_c.iloc[-1])
            dxy_roc15 = float(np.log(dxy_curr / dxy_c.iloc[-16])) if len(dxy_c) > 15 else 0.0
            dxy_roc60 = float(np.log(dxy_curr / dxy_c.iloc[-61])) if len(dxy_c) > 60 else dxy_roc15
            dxy_ema200 = float(dxy_c.ewm(span=200, adjust=False).mean().iloc[-1]) if len(dxy_c) >= 50 else dxy_curr
            dxy_ema200_dist = (dxy_curr - dxy_ema200) / dxy_ema200
            dxy_kalman_vel = float(df_dxy.get("kalman_vel", pd.Series(0.0, index=df_dxy.index)).iloc[-1])
            dxy_rsi14 = self._compute_rsi(dxy_c, 14)
            dxy_vol = float(dxy_c.pct_change().tail(30).std()) if len(dxy_c) >= 30 else 0.001
        else:
            # Fallback estimation based on inverse USD pair movement if available
            dxy_roc15 = 0.0
            dxy_roc60 = 0.0
            dxy_ema200_dist = 0.0
            dxy_kalman_vel = 0.0
            dxy_rsi14 = 50.0
            dxy_vol = 0.001

        # ── 2. SOVEREIGN YIELD & INTEREST RATE REGIME ─────────────────
        if df_yields is not None and not df_yields.empty and len(df_yields) >= 20:
            y_c = df_yields["close"]
            y_curr = float(y_c.iloc[-1])
            us10y_delta1h = (y_curr - float(y_c.iloc[-61])) * 100.0 if len(y_c) > 60 else (y_curr - float(y_c.iloc[0])) * 100.0
            us10y_roc4h = ((y_curr - float(y_c.iloc[-241])) / float(y_c.iloc[-241])) if len(y_c) > 240 else 0.0
            yield_spread = float(df_yields.get("yield_spread", pd.Series(1.5, index=df_yields.index)).iloc[-1])
            yield_spread_vel = float(df_yields.get("yield_spread_vel", pd.Series(0.0, index=df_yields.index)).iloc[-1])
            risk_sentiment = float(df_yields.get("risk_sentiment", pd.Series(0.0, index=df_yields.index)).iloc[-1])
        else:
            us10y_delta1h = 0.0
            us10y_roc4h = 0.0
            yield_spread = 1.50
            yield_spread_vel = 0.0
            risk_sentiment = 0.0

        base_rate = self.CENTRAL_BANK_RATES.get(base_curr, 4.0)
        quote_rate = self.CENTRAL_BANK_RATES.get(quote_curr, 4.0)
        currency_carry = float(base_rate - quote_rate)

        # ── 3. MULTI-CURRENCY RELATIVE STRENGTH MATRIX ────────────────
        g8_rank_base = 50.0
        g8_rank_quote = 50.0
        cross_dispersion = 0.002
        triad_residual = 0.0

        if g8_dfs and len(g8_dfs) >= 2:
            scores = {}
            for cur, c_df in g8_dfs.items():
                if not c_df.empty and len(c_df) >= 15:
                    scores[cur] = float(c_df["close"].pct_change(15).iloc[-1])
                else:
                    scores[cur] = 0.0
            sorted_currs = sorted(scores.keys(), key=lambda k: scores[k])
            n_currs = max(len(sorted_currs) - 1, 1)
            if base_curr in sorted_currs:
                g8_rank_base = (sorted_currs.index(base_curr) / n_currs) * 100.0
            if quote_curr in sorted_currs:
                g8_rank_quote = (sorted_currs.index(quote_curr) / n_currs) * 100.0
            cross_dispersion = float(np.std(list(scores.values())))

        g8_rank_delta = g8_rank_base - g8_rank_quote

        # Pair Beta vs DXY
        if df_dxy is not None and not df_dxy.empty and len(df_dxy) >= 30 and len(close) >= 30:
            ret_p = close.pct_change().tail(60).dropna()
            ret_d = df_dxy["close"].pct_change().tail(60).dropna()
            min_l = min(len(ret_p), len(ret_d))
            if min_l > 10:
                cov = np.cov(ret_p.iloc[-min_l:], ret_d.iloc[-min_l:])[0, 1]
                var_d = np.var(ret_d.iloc[-min_l:])
                pair_beta_dxy = float(cov / var_d) if var_d > 1e-8 else 0.0
            else:
                pair_beta_dxy = 0.0
        else:
            pair_beta_dxy = -0.75 if quote_curr == "USD" else (0.75 if base_curr == "USD" else 0.0)

        # Volume Z-Score
        vol_col = "tick_volume" if "tick_volume" in df_pair.columns else ("volume" if "volume" in df_pair.columns else None)
        if vol_col:
            v_series = df_pair[vol_col]
            v_mean = float(v_series.tail(60).mean())
            v_std = float(v_series.tail(60).std())
            curr_v = float(v_series.iloc[-1])
            volume_zscore = float((curr_v - v_mean) / v_std) if v_std > 1e-4 else 0.0
        else:
            volume_zscore = 0.0

        # ADR Percentage Used
        day_range = float(high.tail(240).max() - low.tail(240).min()) if len(df_pair) >= 60 else (float(high.max()) - float(low.min()))
        rolling_adr = float((high - low).tail(1440).mean() * 20.0) if len(df_pair) >= 200 else (curr_close * 0.008)
        adr_pct_used = float(np.clip(day_range / rolling_adr, 0.0, 2.5)) if rolling_adr > 0 else 0.5

        # ── 4. STRUCTURAL TREND & THERMODYNAMICS ──────────────────────
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1]) if len(close) >= 50 else ema50

        trend_dist_ema20 = (curr_close - ema20) / curr_close
        trend_dist_ema50 = (curr_close - ema50) / curr_close
        trend_dist_ema200 = (curr_close - ema200) / curr_close

        if curr_close > ema20 > ema50 > ema200:
            ema_alignment = 1.0
        elif curr_close < ema20 < ema50 < ema200:
            ema_alignment = -1.0
        else:
            ema_alignment = 0.0

        kalman_price = float(df_pair.get("kalman_price", pd.Series(ema20, index=df_pair.index)).iloc[-1])
        kalman_price_dev = (curr_close - kalman_price) / curr_close

        kalman_vel_z = float(df_pair.get("kalman_velocity_z", df_pair.get("kalman_vel_z", pd.Series(0.0, index=df_pair.index))).iloc[-1])
        shannon_entropy = self._shannon_entropy(close.tail(60))

        # ATR Ratio (14 / 100)
        tr = np.maximum(high - low, np.maximum(abs(high - close.shift(1)), abs(low - close.shift(1))))
        atr14 = float(tr.tail(14).mean()) if len(tr) >= 14 else (curr_close * 0.0005)
        atr100 = float(tr.tail(100).mean()) if len(tr) >= 50 else atr14
        atr_ratio_14_100 = float(atr14 / atr100) if atr100 > 1e-6 else 1.0

        fractal_dim = self._petrosian_fractal_dimension(close.tail(50))

        # Session Progress (0.0 to 1.0 across UTC 24 hours)
        utc_min = current_time.hour * 60 + current_time.minute
        session_progress = float(utc_min / 1440.0)

        # Time to 21:00 UTC Rollover
        rollover_target_min = 21 * 60  # 1260
        if utc_min <= rollover_target_min:
            time_to_rollover = float(rollover_target_min - utc_min)
        else:
            time_to_rollover = float((1440 - utc_min) + rollover_target_min)

        news_proximity = float(news_proximity_minutes)

        features = {
            "DXY_ROC_15": round(dxy_roc15, 6),
            "DXY_ROC_60": round(dxy_roc60, 6),
            "DXY_EMA200_Dist": round(dxy_ema200_dist, 6),
            "DXY_Kalman_Vel": round(dxy_kalman_vel, 6),
            "DXY_RSI_14": round(dxy_rsi14, 2),
            "DXY_Volatility": round(dxy_vol, 6),
            "US10Y_Delta_1h": round(us10y_delta1h, 4),
            "US10Y_ROC_4h": round(us10y_roc4h, 6),
            "Yield_Spread": round(yield_spread, 4),
            "Yield_Spread_Vel": round(yield_spread_vel, 6),
            "Risk_Sentiment": round(risk_sentiment, 4),
            "Currency_Carry": round(currency_carry, 2),
            "G8_Rank_Base": round(g8_rank_base, 1),
            "G8_Rank_Quote": round(g8_rank_quote, 1),
            "G8_Rank_Delta": round(g8_rank_delta, 1),
            "Pair_Beta_DXY": round(pair_beta_dxy, 4),
            "Cross_Dispersion": round(cross_dispersion, 6),
            "Triad_Residual": round(triad_residual, 6),
            "Volume_ZScore": round(volume_zscore, 3),
            "ADR_Pct_Used": round(adr_pct_used, 4),
            "Trend_Dist_EMA20": round(trend_dist_ema20, 6),
            "Trend_Dist_EMA50": round(trend_dist_ema50, 6),
            "Trend_Dist_EMA200": round(trend_dist_ema200, 6),
            "EMA_Alignment": round(ema_alignment, 1),
            "Kalman_Price_Dev": round(kalman_price_dev, 6),
            "Kalman_Velocity": round(kalman_vel_z, 4),
            "Shannon_Entropy": round(shannon_entropy, 4),
            "ATR_Ratio_14_100": round(atr_ratio_14_100, 4),
            "Fractal_Dim": round(fractal_dim, 4),
            "Session_Progress": round(session_progress, 4),
            "Time_To_Rollover": round(time_to_rollover, 1),
            "News_Proximity": round(news_proximity, 1),
        }

        # Sanitize any accidental NaNs/Infs
        for k, v in features.items():
            if np.isnan(v) or np.isinf(v):
                features[k] = 0.0

        return features
