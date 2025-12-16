# =====================================================================
#  pattern_engine.py — PATTERN SUMMARY ENGINE (FINAL, STABLE)
# =====================================================================

from config import MOMENTUM_THRESHOLD, PATTERN_PCT


def generate_pattern_analysis(
    metrics,
    price_pct,
    rsi=None,
    rsi_3m=None,
    ob_ratio=None,
    ob_label=None,
    stage_label=None,
    mode="full"
):
    """
    FULL ORIGINAL Variant-C Pattern Summary
    mode = "full" / "short" / "exit"
    """
    try:
        def to_float(val, default=None):
            try:
                if val in ["-", None]:
                    return default
                return float(val)
            except Exception:
                return default

        oi_chg = to_float(metrics.get("oi_chg"), None)
        not_chg = to_float(metrics.get("not_chg"), None)
        acc_r = to_float(metrics.get("acc_r"), None)
        pos_r = to_float(metrics.get("pos_r"), None)
        glb_r = to_float(metrics.get("glb_r"), None)
        fund_chg = to_float(metrics.get("funding_change"), 0.0)

        price_pct = price_pct or 0.0
        abs_p = abs(price_pct)

        # ----- OI Pattern -----
        if oi_chg is None:
            oi_label = "Flat / unknown"
        elif oi_chg > 2:
            oi_label = "Increasing (leverage coming in)"
        elif oi_chg < -2:
            oi_label = "Decreasing (positions closing)"
        else:
            oi_label = "Stable / sideway"

        # ----- Pro Traders -----
        if pos_r is None:
            pro_label = "Neutral / unknown"
        elif pos_r > 1.1:
            pro_label = "Long-biased (Pos L/S > 1)"
        elif pos_r < 0.9:
            pro_label = "Short-biased (Pos L/S < 1)"
        else:
            pro_label = "Balanced"

        # ----- Retail -----
        if acc_r is None:
            retail_label = "Neutral / unknown"
        elif acc_r > 1.1:
            retail_label = "Long-heavy (FOMO risk)"
        elif acc_r < 0.9:
            retail_label = "Short-heavy (squeeze fuel)"
        else:
            retail_label = "Balanced / mixed"

        # ----- Global -----
        if glb_r is None:
            global_label = "Neutral"
        elif glb_r > 1.05:
            global_label = "Bullish-leaning"
        elif glb_r < 0.95:
            global_label = "Bearish-leaning"
        else:
            global_label = "Neutral"

        # ----- Momentum -----
        if abs_p < MOMENTUM_THRESHOLD * 0.5:
            momentum_label = "Weak / choppy"
        elif abs_p < MOMENTUM_THRESHOLD * 1.2:
            momentum_label = "Building"
        else:
            momentum_label = "Strong move"

        # ----- Divergence -----
        if price_pct > 0 and oi_chg is not None and oi_chg < 0:
            divergence_label = "Price↑ & OI↓ → Short squeeze potential"
            squeeze_bias = 2
        elif price_pct < 0 and oi_chg is not None and oi_chg > 0:
            divergence_label = "Price↓ & OI↑ → Leverage trap / breakdown risk"
            squeeze_bias = -1
        else:
            divergence_label = "Price & OI aligned"
            squeeze_bias = 0

        # ----- Bias -----
        if price_pct > 0:
            bias = "LONG"
            trend_dir_label = "Bullish"
        elif price_pct < 0:
            bias = "SHORT"
            trend_dir_label = "Bearish"
        else:
            bias = "NONE"
            trend_dir_label = "Sideways"

        # ----- Fake score -----
        fake_score = 0
        if bias == "LONG" and acc_r and glb_r and acc_r > 1.1 and glb_r > 1.05:
            fake_score += 2
        if bias == "SHORT" and acc_r and glb_r and acc_r < 0.9 and glb_r < 0.95:
            fake_score += 2
        if abs_p >= PATTERN_PCT and (oi_chg is not None and abs(oi_chg) < 1.0):
            fake_score += 1
        if squeeze_bias > 0 and bias == "LONG":
            fake_score = max(fake_score - 1, 0)

        fake_label = "LOW" if fake_score <= 0 else "MEDIUM" if fake_score <= 2 else "HIGH"

        # ----- Squeeze probability -----
        squeeze_prob = "LOW"
        if (
            price_pct > 0 and oi_chg is not None and oi_chg <= 0
            and acc_r is not None and acc_r < 0.9
            and glb_r is not None and glb_r < 0.9
        ):
            squeeze_prob = "HIGH"
        elif squeeze_bias > 0:
            squeeze_prob = "MEDIUM"

        # ----- Final score -----
        score = 0.0
        score += min(abs_p / PATTERN_PCT, 2.0) * 20

        if oi_chg is not None:
            same_dir = (price_pct > 0 and oi_chg > 0) or (price_pct < 0 and oi_chg < 0)
            score += 20 if same_dir else 10 if squeeze_bias != 0 else 0

        if bias == "LONG" and pos_r and pos_r > 1.05:
            score += 15
        if bias == "SHORT" and pos_r and pos_r < 0.95:
            score += 15

        if bias == "LONG" and acc_r and acc_r < 0.9:
            score += 10
        if bias == "SHORT" and acc_r and acc_r > 1.1:
            score += 10

        if momentum_label == "Strong move":
            score += 10

        if fake_label == "HIGH":
            score -= 20
        elif fake_label == "MEDIUM":
            score -= 5

        score = max(0, min(int(round(score)), 100))

        final_bias = "NO TRADE" if score < 40 or bias == "NONE" else bias
        icon = "🟧" if final_bias == "NO TRADE" else ("🟢" if bias == "LONG" else "🔴")

        verdict = (
            f"{trend_dir_label} & Strong" if momentum_label == "Strong move"
            else f"{trend_dir_label} (developing)"
            if trend_dir_label != "Sideways"
            else "Sideways / No clear trend"
        )

        reasons = []
        if oi_chg is not None:
            if price_pct > 0 and oi_chg > 0:
                reasons.append("OI↑ + Price↑ (real trend)")
            elif price_pct < 0 and oi_chg < 0:
                reasons.append("OI↓ + Price↓ (real unwinding)")
        if pos_r:
            reasons.append("pro positioning")
        if acc_r:
            reasons.append("retail positioning")
        if ob_ratio and ob_label:
            reasons.append(ob_label)

        if not reasons:
            reasons.append("mixed sentiment")

        lines = ["\n📊 PATTERN SUMMARY"]

        if stage_label:
            lines.append(f"• Impulse Stage: {stage_label}")

        if mode == "full":
            lines.extend([
                f"• OI Pattern: {oi_label}",
                f"• Pro Traders: {pro_label}",
                f"• Retail: {retail_label}",
                f"• Global Sentiment: {global_label}",
                f"• Momentum Context: {momentum_label}",
                f"• Divergence: {divergence_label}",
            ])
        elif mode == "short":
            lines.extend([
                f"• OI: {oi_label}",
                f"• Pro: {pro_label}",
                f"• Retail: {retail_label}",
                f"• Divergence: {divergence_label}",
            ])
        elif mode == "exit":
            lines.extend([
                f"• OI: {oi_label}",
                f"• Retail: {retail_label}",
                f"• Divergence: {divergence_label}",
            ])

        lines.extend([
            f"\n🎯 TREND VERDICT: {verdict}",
            f"Fake-out risk: {fake_label}",
            f"Squeeze probability: {squeeze_prob}",
            "\n🔮 FINAL CONFIRMATION:",
            f"{icon} {final_bias} — {score}%",
            f"Reason: {', '.join(reasons)}",
        ])

        return "\n".join(lines)

    except Exception as e:
        print("generate_pattern_analysis error:", e)
        return ""
