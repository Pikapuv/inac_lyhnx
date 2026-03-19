from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Iterable


SETTINGS_PATH = Path(__file__).with_name("settings.json")


@dataclass
class TradingSession:
    start_hour: int
    end_hour: int


@dataclass
class Settings:
    initial_capital_usdt: float = 20.0
    daily_limit_pct: float = 3.0
    max_trades_per_day: int = 3

    # Throttling for number of BUY proposals we emit to Telegram.
    # Since we keep only ONE risk position globally, these are not executed trades,
    # but they should prevent spamming and "no signal for days" issues.
    proposals_limit_global: int = 3
    proposals_limit_per_symbol: int = 1

    # Theo mục tiêu "winrate cao":
    # - TP gần hơn (dễ chạm)
    # - SL rộng hơn (để tỷ lệ đạt TP cao hơn)
    tp_pct_min: float = 1.8
    tp_pct_max: float = 2.5
    sl_pct: float = 4.0

    dump_threshold_pct: float = -0.3
    dump_threshold_1h_pct: float = -1.0
    pump_threshold_pct: float = 1.5

    # "Tín hiệu đẹp" (quality filters)
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    support_lookback_bars: int = 20
    support_margin_pct: float = 0.3
    ma_trend_period: int = 50

    # Mỗi ngày phải có ít nhất 1 lệnh:
    # nếu chưa có lệnh nào mà đến giờ UTC muộn -> nới yêu cầu chất lượng để vẫn có giao dịch.
    quality_min_conditions: int = 4
    quality_fallback_min_conditions: int = 2
    force_min_trades_from_hour_utc: int = 20

    # Khi gửi cảnh báo TP, ưu tiên chốt lời ngay (tăng winrate).
    close_on_tp_alert: bool = True

    # Khi gửi tín hiệu BUY mà user chưa ENTER trong khoảng thời gian này (giây),
    # bot sẽ nhắc nhở và (nếu còn valid) gửi tín hiệu mới.
    proposal_ttl_seconds: int = 300

    # Trend & entry quality (flow: Trend + Dip + Stabilizing)
    # EMA50_1h: chỉ cập nhật dựa trên 1h đã đóng (handled in strategy by grouping 5m candles)
    ema_trend_period_1h: int = 50
    rsi_reversal_threshold_5m: float = 35.0
    # Adaptive thresholds (trend strong -> relaxed RSI/dip)
    rsi_reversal_threshold_5m_trend_strong: float = 45.0
    dump_threshold_1h_pct_trend_strong: float = -0.5

    # Not near resistance: recent high trong 3h
    resistance_lookback_hours: int = 3
    resistance_distance_pct_min: float = 1.0  # >1% upside needed

    # Entry scoring
    entry_score_min: float = 2.0

    # Breakout mode
    breakout_enabled: bool = True
    breakout_lookback_hours: int = 3
    breakout_buffer_pct: float = 0.05
    breakout_volume_mult: float = 1.5

    # Cooldown: tính từ lúc bot phát hiện SELL fill (position_closed)
    cooldown_minutes_after_close: int = 30

    symbol: str = "ETH/USDT"
    # Multi-symbol scanning. If empty/missing, fallback to [symbol].
    symbols: List[str] | None = None

    trading_sessions: List[TradingSession] | None = None
    auto_trade_day: bool = False
    auto_trade_night: bool = False

    @property
    def daily_limit_usdt(self) -> float:
        return self.initial_capital_usdt * self.daily_limit_pct / 100.0

    @staticmethod
    def default_sessions() -> List[TradingSession]:
        return [TradingSession(start_hour=0, end_hour=23)]

    @staticmethod
    def _normalize_symbols(symbols: Iterable[str] | None) -> List[str]:
        if not symbols:
            return []
        out: List[str] = []
        seen: set[str] = set()
        for s in symbols:
            if not s:
                continue
            s2 = str(s).strip()
            if not s2:
                continue
            if s2 not in seen:
                out.append(s2)
                seen.add(s2)
        return out

    def effective_symbols(self) -> List[str]:
        syms = self._normalize_symbols(self.symbols)
        if syms:
            return syms
        return [self.symbol]

    def apply_config_overrides(self, cfg: Dict[str, Any]) -> "Settings":
        """
        Override settings from config.yaml (optional).
        Expected keys:
          - strategy.symbol / strategy.symbols / strategy.dump_threshold_pct / strategy.pump_threshold_pct
          - risk.capital_usdt / risk.daily_pnl_limit_pct / risk.max_trades_per_day
          - risk.max_loss_pct_per_trade / risk.target_profit_pct_min / risk.target_profit_pct_max
        """
        strategy = (cfg.get("strategy") or {}) if isinstance(cfg, dict) else {}
        risk = (cfg.get("risk") or {}) if isinstance(cfg, dict) else {}

        # symbols
        symbols_raw = strategy.get("symbols")
        if symbols_raw:
            self.symbols = self._normalize_symbols(symbols_raw)
            if self.symbols:
                self.symbol = self.symbols[0]
        else:
            sym = strategy.get("symbol")
            if sym:
                self.symbol = str(sym).strip()
                self.symbols = self._normalize_symbols([self.symbol])

        # thresholds
        if strategy.get("dump_threshold_pct") is not None:
            try:
                self.dump_threshold_pct = float(strategy.get("dump_threshold_pct"))
            except Exception:
                pass
        if strategy.get("dump_threshold_1h_pct") is not None:
            try:
                self.dump_threshold_1h_pct = float(strategy.get("dump_threshold_1h_pct"))
            except Exception:
                pass
        if strategy.get("pump_threshold_pct") is not None:
            try:
                self.pump_threshold_pct = float(strategy.get("pump_threshold_pct"))
            except Exception:
                pass
        if strategy.get("entry_score_min") is not None:
            try:
                self.entry_score_min = float(strategy.get("entry_score_min"))
            except Exception:
                pass

        # Trend / EMA
        if strategy.get("ema_trend_period_1h") is not None:
            try:
                self.ema_trend_period_1h = int(strategy.get("ema_trend_period_1h"))
            except Exception:
                pass

        # Adaptive thresholds
        if strategy.get("rsi_reversal_threshold_5m_trend_strong") is not None:
            try:
                self.rsi_reversal_threshold_5m_trend_strong = float(strategy.get("rsi_reversal_threshold_5m_trend_strong"))
            except Exception:
                pass
        if strategy.get("dump_threshold_1h_pct_trend_strong") is not None:
            try:
                self.dump_threshold_1h_pct_trend_strong = float(strategy.get("dump_threshold_1h_pct_trend_strong"))
            except Exception:
                pass

        # Breakout
        if strategy.get("breakout_enabled") is not None:
            self.breakout_enabled = bool(strategy.get("breakout_enabled"))
        if strategy.get("breakout_lookback_hours") is not None:
            try:
                self.breakout_lookback_hours = int(strategy.get("breakout_lookback_hours"))
            except Exception:
                pass
        if strategy.get("breakout_buffer_pct") is not None:
            try:
                self.breakout_buffer_pct = float(strategy.get("breakout_buffer_pct"))
            except Exception:
                pass
        if strategy.get("breakout_volume_mult") is not None:
            try:
                self.breakout_volume_mult = float(strategy.get("breakout_volume_mult"))
            except Exception:
                pass

        # risk -> settings mapping
        if risk.get("capital_usdt") is not None:
            try:
                self.initial_capital_usdt = float(risk.get("capital_usdt"))
            except Exception:
                pass
        if risk.get("daily_pnl_limit_pct") is not None:
            try:
                self.daily_limit_pct = float(risk.get("daily_pnl_limit_pct"))
            except Exception:
                pass
        if risk.get("max_trades_per_day") is not None:
            try:
                self.max_trades_per_day = int(risk.get("max_trades_per_day"))
            except Exception:
                pass

        # proposal limits (throttling)
        if strategy.get("proposals_limit_global") is not None:
            try:
                self.proposals_limit_global = int(strategy.get("proposals_limit_global"))
            except Exception:
                pass
        if strategy.get("proposals_limit_per_symbol") is not None:
            try:
                self.proposals_limit_per_symbol = int(strategy.get("proposals_limit_per_symbol"))
            except Exception:
                pass

        if risk.get("max_loss_pct_per_trade") is not None:
            try:
                self.sl_pct = float(risk.get("max_loss_pct_per_trade"))
            except Exception:
                pass
        if risk.get("target_profit_pct_min") is not None:
            try:
                self.tp_pct_min = float(risk.get("target_profit_pct_min"))
            except Exception:
                pass
        if risk.get("target_profit_pct_max") is not None:
            try:
                self.tp_pct_max = float(risk.get("target_profit_pct_max"))
            except Exception:
                pass

        return self

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Settings":
        """
        Build Settings purely from config.yaml (no settings.json).
        """
        return cls(trading_sessions=cls.default_sessions()).apply_config_overrides(cfg)

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        if not path.exists():
            settings = cls(trading_sessions=cls.default_sessions(), symbols=["ETH/USDT"])
            settings.save(path)
            return settings

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        sessions_raw = raw.get("trading_sessions") or []
        sessions = [
            TradingSession(start_hour=s["start_hour"], end_hour=s["end_hour"])
            for s in sessions_raw
        ]

        symbol = raw.get("symbol", "ETH/USDT")
        symbols_raw = raw.get("symbols")
        symbols = cls._normalize_symbols(symbols_raw if symbols_raw is not None else [symbol])
        if not symbols:
            symbols = [symbol]

        return cls(
            initial_capital_usdt=raw.get("initial_capital_usdt", 20.0),
            daily_limit_pct=raw.get("daily_limit_pct", 3.0),
            max_trades_per_day=raw.get("max_trades_per_day", 3),
            proposals_limit_global=raw.get("proposals_limit_global", raw.get("max_trades_per_day", 3)),
            proposals_limit_per_symbol=raw.get("proposals_limit_per_symbol", 1),
            tp_pct_min=raw.get("tp_pct_min", 1.8),
            tp_pct_max=raw.get("tp_pct_max", 2.5),
            sl_pct=raw.get("sl_pct", 4.0),
            dump_threshold_pct=raw.get("dump_threshold_pct", -0.3),
            dump_threshold_1h_pct=raw.get("dump_threshold_1h_pct", -1.0),
            pump_threshold_pct=raw.get("pump_threshold_pct", 1.5),
            rsi_period=raw.get("rsi_period", 14),
            rsi_oversold=raw.get("rsi_oversold", 30.0),
            support_lookback_bars=raw.get("support_lookback_bars", 20),
            support_margin_pct=raw.get("support_margin_pct", 0.3),
            ma_trend_period=raw.get("ma_trend_period", 50),
            quality_min_conditions=raw.get("quality_min_conditions", 4),
            quality_fallback_min_conditions=raw.get(
                "quality_fallback_min_conditions", 2
            ),
            force_min_trades_from_hour_utc=raw.get(
                "force_min_trades_from_hour_utc", 20
            ),
            close_on_tp_alert=raw.get("close_on_tp_alert", True),
            proposal_ttl_seconds=raw.get("proposal_ttl_seconds", 300),
            ema_trend_period_1h=raw.get("ema_trend_period_1h", 50),
            rsi_reversal_threshold_5m=raw.get(
                "rsi_reversal_threshold_5m", 35.0
            ),
            resistance_lookback_hours=raw.get("resistance_lookback_hours", 3),
            resistance_distance_pct_min=raw.get(
                "resistance_distance_pct_min", 1.0
            ),
            cooldown_minutes_after_close=raw.get(
                "cooldown_minutes_after_close", 30
            ),
            symbol=symbol,
            symbols=symbols,
            trading_sessions=sessions or cls.default_sessions(),
            auto_trade_day=raw.get("auto_trade_day", False),
            auto_trade_night=raw.get("auto_trade_night", False),
        )

    def to_json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["trading_sessions"] = [
            {"start_hour": s.start_hour, "end_hour": s.end_hour}
            for s in (self.trading_sessions or [])
        ]
        # Ensure symbols is persisted in normalized form.
        data["symbols"] = self.effective_symbols()
        # Keep `symbol` consistent (first element).
        if data["symbols"]:
            data["symbol"] = data["symbols"][0]
        return data

    def save(self, path: Path = SETTINGS_PATH) -> None:
        if self.trading_sessions is None:
            self.trading_sessions = self.default_sessions()

        data = self.to_json()
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

