from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from backtest.metrics import PerformanceMetrics
from backtest.monte_carlo import MonteCarloResult
from backtest.sim_engine import BacktestResult
from backtest.walk_forward import WalkForwardResult

logger = logging.getLogger(__name__)


class BacktestReport:
    """
    Generates comprehensive backtest performance reports.
    Outputs both human-readable text and structured JSON.
    """

    def __init__(self, result: BacktestResult):
        self._result = result
        self._mc_result: MonteCarloResult | None = None
        self._wf_result: WalkForwardResult | None = None

    def set_monte_carlo(self, mc: MonteCarloResult) -> None:
        self._mc_result = mc

    def set_walk_forward(self, wf: WalkForwardResult) -> None:
        self._wf_result = wf

    def generate_text(self) -> str:
        m = self._result.metrics
        lines = [
            "=" * 60,
            "  OFI Pro — Backtest Performance Report",
            "=" * 60,
            "",
            f"  Period:     {self._result.total_ticks} ticks processed",
            f"  Balance:    ${self._result.initial_balance:,.2f} -> ${self._result.final_balance:,.2f}",
            f"  Return:     {m.return_pct * 100:+.2f}%",
            "",
            "--- Trade Statistics ---",
            f"  Total Trades:      {m.total_trades}",
            f"  Winners:           {m.winners} ({m.win_rate * 100:.1f}%)",
            f"  Losers:            {m.losers}",
            f"  Long / Short:      {m.long_count} / {m.short_count}",
            f"  Veto Events:       {m.veto_count}",
            f"  Avg Daily Trades:  {m.avg_daily_trades:.1f}",
            "",
            "--- P&L Breakdown ---",
            f"  Net P&L:           ${m.net_pnl:+,.2f}",
            f"  Gross Profit:      ${m.gross_profit:,.2f}",
            f"  Gross Loss:        ${m.gross_loss:,.2f}",
            f"  Total Fees:        ${m.total_fees:,.2f}",
            f"  Largest Win:       ${m.largest_win:+,.2f}",
            f"  Largest Loss:      ${m.largest_loss:+,.2f}",
            "",
            "--- Risk Metrics ---",
            f"  Max Drawdown:      {m.max_drawdown_pct * 100:.2f}% (${m.max_drawdown_usd:,.2f})",
            f"  Sharpe Ratio:      {m.sharpe_ratio:.3f}",
            f"  Sortino Ratio:     {m.sortino_ratio:.3f}",
            f"  Calmar Ratio:      {m.calmar_ratio:.3f}",
            f"  Profit Factor:     {m.profit_factor:.3f}",
            f"  Avg Win/Loss:      {m.avg_rr:.2f}",
            f"  Avg Hold Time:     {m.avg_hold_time_seconds / 60:.0f} min",
            "",
        ]

        lines.extend(self._target_check_section(m))

        if self._mc_result:
            lines.extend(self._mc_section())

        if self._wf_result:
            lines.extend(self._wf_section())

        lines.extend(["", "=" * 60])
        return "\n".join(lines)

    def generate_json(self) -> dict:
        out: dict = {
            "backtest": {
                "total_ticks": self._result.total_ticks,
                "initial_balance": self._result.initial_balance,
                "final_balance": self._result.final_balance,
                "veto_count": self._result.veto_count,
                "signal_count": self._result.signal_count,
            },
            "metrics": _metrics_to_dict(self._result.metrics),
            "params": self._result.params,
        }

        if self._mc_result:
            out["monte_carlo"] = {
                "iterations": self._mc_result.iterations,
                "median_max_dd": round(self._mc_result.median_max_dd, 6),
                "percentile_95_dd": round(self._mc_result.percentile_95_dd, 6),
                "percentile_99_dd": round(self._mc_result.percentile_99_dd, 6),
                "worst_dd": round(self._mc_result.worst_dd, 6),
                "ruin_probability": round(self._mc_result.ruin_probability, 6),
            }

        if self._wf_result:
            out["walk_forward"] = {
                "is_robust": self._wf_result.is_robust,
                "train_ticks": self._wf_result.train_ticks,
                "test_ticks": self._wf_result.test_ticks,
                "degradation": self._wf_result.degradation,
                "train_metrics": _metrics_to_dict(self._wf_result.train_result.metrics),
                "test_metrics": _metrics_to_dict(self._wf_result.test_result.metrics),
            }

        return out

    def save(self, directory: str | Path, prefix: str = "backtest") -> tuple[Path, Path]:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)

        txt_path = d / f"{prefix}_report.txt"
        json_path = d / f"{prefix}_report.json"

        txt_path.write_text(self.generate_text(), encoding="utf-8")
        json_path.write_text(json.dumps(self.generate_json(), indent=2), encoding="utf-8")

        logger.info("Report saved: %s, %s", txt_path, json_path)
        return txt_path, json_path

    def _target_check_section(self, m: PerformanceMetrics) -> list[str]:
        lines = ["--- PRD Target Check ---"]
        targets = m.meets_targets
        for key, passed in targets.items():
            icon = "PASS" if passed else "FAIL"
            lines.append(f"  [{icon}] {key}")
        lines.append("")
        return lines

    def _mc_section(self) -> list[str]:
        mc = self._mc_result
        assert mc is not None
        return [
            "--- Monte Carlo Analysis ---",
            f"  Iterations:         {mc.iterations}",
            f"  Median Max DD:      {mc.median_max_dd * 100:.2f}%",
            f"  95th Pctile DD:     {mc.percentile_95_dd * 100:.2f}%",
            f"  99th Pctile DD:     {mc.percentile_99_dd * 100:.2f}%",
            f"  Worst Case DD:      {mc.worst_dd * 100:.2f}%",
            f"  Ruin Probability:   {mc.ruin_probability * 100:.2f}%",
            "",
        ]

    def _wf_section(self) -> list[str]:
        wf = self._wf_result
        assert wf is not None
        tm = wf.train_result.metrics
        om = wf.test_result.metrics
        return [
            "--- Walk-Forward Validation ---",
            f"  Train / Test:       {wf.train_ticks} / {wf.test_ticks} ticks",
            f"  Robust:             {'YES' if wf.is_robust else 'NO'}",
            "",
            f"  {'Metric':<20s} {'Train':>10s} {'OOS':>10s} {'Change':>10s}",
            f"  {'-' * 50}",
            f"  {'Win Rate':<20s} {tm.win_rate * 100:>9.1f}% {om.win_rate * 100:>9.1f}% {wf.degradation.get('win_rate_change', 0) * 100:>+9.1f}%",
            f"  {'Sharpe':<20s} {tm.sharpe_ratio:>10.3f} {om.sharpe_ratio:>10.3f} {wf.degradation.get('sharpe_change', 0) * 100:>+9.1f}%",
            f"  {'Profit Factor':<20s} {tm.profit_factor:>10.3f} {om.profit_factor:>10.3f} {wf.degradation.get('profit_factor_change', 0) * 100:>+9.1f}%",
            f"  {'Max DD':<20s} {tm.max_drawdown_pct * 100:>9.2f}% {om.max_drawdown_pct * 100:>9.2f}% {wf.degradation.get('max_dd_change', 0) * 100:>+9.1f}%",
            "",
        ]


def _metrics_to_dict(m: PerformanceMetrics) -> dict:
    return {
        "total_trades": m.total_trades,
        "win_rate": round(m.win_rate, 4),
        "net_pnl": round(m.net_pnl, 2),
        "profit_factor": round(m.profit_factor, 4) if m.profit_factor != float("inf") else 999.0,
        "sharpe_ratio": round(m.sharpe_ratio, 4),
        "sortino_ratio": round(m.sortino_ratio, 4),
        "calmar_ratio": round(m.calmar_ratio, 4),
        "max_drawdown_pct": round(m.max_drawdown_pct, 6),
        "avg_rr": round(m.avg_rr, 4) if m.avg_rr != float("inf") else 999.0,
        "return_pct": round(m.return_pct, 6),
        "total_fees": round(m.total_fees, 2),
        "avg_daily_trades": round(m.avg_daily_trades, 2),
        "veto_count": m.veto_count,
    }
