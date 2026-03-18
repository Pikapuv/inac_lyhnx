from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


DAY_START_PREFIX = "=== DAY START"
DAY_END_PREFIX = "=== DAY END"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_id(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ScanRow:
    ts_utc: str
    symbol: str
    price: float | None
    chg_5m: float | None
    chg_1h: float | None
    rsi_5m: float | None
    vol_spike: float | None
    trend_strong: bool | None
    dip_score: float | None
    breakout_score: float | None
    best_kind: str | None
    best_score: float | None
    passed: bool
    reason: str
    gated: str  # e.g. "OK", "HAS_POSITION", "PENDING", "ENTRY_OFF"


class DataLogger:
    def __init__(self, path: str = "data.txt", retention_days: int = 3):
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days))
        self._ensure_file()
        self._last_day = self._get_last_day_in_file() or _day_id(_utc_now())

    def _ensure_file(self) -> None:
        if self.path.exists():
            return
        now = _utc_now()
        self.path.write_text(
            f"START_UTC={_iso(now)}\n{DAY_START_PREFIX} {_day_id(now)} UTC={_iso(now)} ===\n",
            encoding="utf-8",
        )

    def _get_last_day_in_file(self) -> Optional[str]:
        if not self.path.exists():
            return None
        try:
            txt = self.path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
        last = None
        for line in txt.splitlines():
            if line.startswith(DAY_START_PREFIX):
                parts = line.split()
                if len(parts) >= 4:
                    last = parts[3] if parts[2] == "START" else parts[2]
                else:
                    # fallback parse: "=== DAY START YYYY-MM-DD ..."
                    if len(parts) >= 4:
                        last = parts[3]
        # more robust:
        for line in txt.splitlines():
            if line.startswith(DAY_START_PREFIX):
                # "=== DAY START 2026-03-18 ..."
                parts = line.split()
                if len(parts) >= 4:
                    last = parts[3]
        return last

    def _count_day_starts(self, lines: List[str]) -> int:
        return sum(1 for ln in lines if ln.startswith(DAY_START_PREFIX))

    def _trim_to_retention_days(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        except Exception:
            return
        # Keep the first START_UTC line always (line0), and last N day blocks.
        if not lines:
            return
        start_line = lines[0:1]
        rest = lines[1:]
        day_start_idx = [i for i, ln in enumerate(rest) if ln.startswith(DAY_START_PREFIX)]
        if len(day_start_idx) <= self.retention_days:
            return
        cut_from = day_start_idx[-self.retention_days]
        new_lines = start_line + rest[cut_from:]
        try:
            self.path.write_text("".join(new_lines), encoding="utf-8")
        except Exception:
            return

    def _roll_day_if_needed(self) -> None:
        now = _utc_now()
        day = _day_id(now)
        if day == self._last_day:
            return
        # Close previous day and start new day
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"{DAY_END_PREFIX} {self._last_day} UTC={_iso(now)} ===\n")
            f.write(f"{DAY_START_PREFIX} {day} UTC={_iso(now)} ===\n")
        self._last_day = day
        self._trim_to_retention_days()

    def append_scan(self, rows: Iterable[ScanRow]) -> None:
        self._roll_day_if_needed()
        # CSV-ish format for easy grepping/parsing
        # ts_utc|symbol|price|chg5m|chg1h|rsi5m|volSpike|trendStrong|dipScore|breakScore|bestKind|bestScore|passed|gated|reason
        with self.path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(
                    f"{r.ts_utc}|{r.symbol}|{_fmt(r.price)}|{_fmt(r.chg_5m)}|{_fmt(r.chg_1h)}|"
                    f"{_fmt(r.rsi_5m)}|{_fmt(r.vol_spike)}|{_fmt_bool(r.trend_strong)}|"
                    f"{_fmt(r.dip_score)}|{_fmt(r.breakout_score)}|{r.best_kind or ''}|{_fmt(r.best_score)}|"
                    f"{'1' if r.passed else '0'}|{r.gated}|{r.reason}\n"
                )


def _fmt(x: float | None) -> str:
    if x is None:
        return ""
    try:
        return f"{float(x):.6f}"
    except Exception:
        return ""


def _fmt_bool(x: bool | None) -> str:
    if x is None:
        return ""
    return "1" if x else "0"

