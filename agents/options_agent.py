"""
agents/options_agent.py — Deribit BTC Options GEX & Structural Market Layer.

P2-konform: Vollstaendig deterministisch, kein LLM.
Liest die oeffentliche Deribit BTC-Optionskette (REST API), berechnet Black-Scholes Gamma
und Gamma Exposure (GEX in USD) unter Beruecksichtigung inverser Kontrakte (S^2-Skalierung)
und publiziert strukturierte Snapshots auf den Broker.

Mathematische Referenzen:
- Black-Scholes (1973): Gamma = exp(-d1^2 / 2) / (S * sigma * sqrt(2*pi*T))
- Inverse Deribit Kontrakte: GEX_USD = gamma * OI * S^2 * 0.01 * sign (1 Kontrakt = 1 BTC)
- Weiss, Gaudiosi, Zhou & Webb (2026, Finance Research Letters 107, 110340):
  08:00 UTC Verfall-Reversal bei Top-Dezil ATM-OI + negativem Netto-GEX.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

from core.broker import Broker, AGGREGATED, TRADES, OPTIONS, OPTIONS_SNAPSHOT

logger = logging.getLogger(__name__)

# ── Konstanten ─────────────────────────────────────────────────────────────────
DERIBIT_BOOK_SUMMARY_URL = (
    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option"
)

# 15 Minuten Floor gegen Gamma-Divergenz kurz vor Verfall (T -> 0)
EXPIRY_FLOOR_SECONDS = 900.0
SECONDS_PER_YEAR = 31_536_000.0  # 365 * 86400

# Ab diesem Ketten-Alter wird nichts mehr angezeigt (Charter §2: keine Zahl
# ohne Datengrundlage). Darunter: letzter echter Snapshot mit sichtbarem Alter.
MAX_CHAIN_AGE_SECONDS = 600.0

# Persistenzpfad fuer historisches 07:00 UTC ATM-OI (Top-Dezil-Klassifikation)
DATA_DIR = Path("data")
ATM_OI_HISTORY_FILE = DATA_DIR / "options_atm_oi_history.json"
MIN_HISTORY_FOR_EXPIRY_DECILE = 60

# Monatskuerzel fuer Deribit Instrumenten-Namen
MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
}

# Regex fuer Deribit BTC Optionen: z.B. BTC-27JUN26-90000-C oder BTC-3JUL26-85000-P
INSTRUMENT_PATTERN = re.compile(
    r"^BTC-(?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2})-(?P<strike>\d+(?:\.\d+)?)-(?P<type>[CP])$"
)

# Throttled Logger Cache
_THROTTLE_LOGS: Dict[str, float] = {}


def _throttled_warn(key: str, msg: str, interval: float = 60.0) -> None:
    """Loggt Warnungen maximal einmal alle `interval` Sekunden pro Schluessel."""
    now = time.time()
    last = _THROTTLE_LOGS.get(key, 0.0)
    if now - last >= interval:
        _THROTTLE_LOGS[key] = now
        logger.warning(msg)


# ── Frozen Dataclasses (Output Shape nach Spec §5) ────────────────────────────

@dataclass(frozen=True)
class StrikeGamma:
    """Gamma- und GEX-Metriken fuer einen einzelnen Strike."""
    strike: float
    option_type: str          # "C" | "P"
    open_interest: float      # in BTC (inverse Deribit Kontrakte)
    mark_iv: float            # Dezimalwert, z.B. 0.558 (55.8%)
    gamma: float              # Standard Black-Scholes Gamma
    gex_usd: float            # Vorzeichenbehaftetes GEX in USD pro 1% Spot-Bewegung


@dataclass(frozen=True)
class ExpiryGamma:
    """Aggregierte Optionsmetriken fuer eine Verfallsgruppe."""
    expiry: datetime          # UTC, 08:00:00
    label: str                # "0dte" | "weekly" | "monthly"
    t_years: float            # Restlaufzeit in Jahren (mind. 15min Floor)
    strikes: tuple[StrikeGamma, ...]
    net_gex: float            # Netto GEX (Summe aller Calls + Puts)
    zero_gamma: Optional[float]  # Strike mit linearem Null-Durchgang des kumulativen GEX
    put_wall: Optional[float]    # Strike mit maximalem Put-OI unterhalb Spot
    call_wall: Optional[float]   # Strike mit maximalem Call-OI oberhalb Spot
    atm_oi: float             # Summe Call+Put OI innerhalb +/-2.5% um Spot (in BTC)
    expected_move: Optional[float]  # S * iv_atm * sqrt(T_years); None wenn keine IV messbar
    iv_atm: Optional[float] = None  # ATM Mark-IV als Dezimalwert; None wenn nicht messbar


@dataclass(frozen=True)
class ExpiryReversalFlag:
    """
    Weiss, Gaudiosi, Zhou & Webb (2026) 08:00 UTC Reversal-Kontext.
    Aktiv nur wenn um 07:00 UTC:
    1. 0DTE ATM-OI im obersten Dezil (>= 90. Perzentil der letzten 60+ Tage)
    2. 0DTE Net-GEX negativ ist (Dealers Short Gamma / Amplifying)
    """
    active: Optional[bool]     # True / False / None (bei < 60 Tagen Historie)
    atm_oi_0dte: float         # 0DTE ATM OI in BTC
    net_gex_0dte: float        # 0DTE Net GEX in USD
    top_decile_threshold: Optional[float]
    history_count: int
    caveats: tuple[str, ...] = (
        "Modellannahme: Market Maker halten die Gegenseite der Kundenpositionen.",
        "Studiensample: 2021-2023 (pre-ETF, Finance Research Letters 107).",
        "Bereinigtes R^2 = 0.05. Reiner Strukturkontext, niemals ein isoliertes Signal."
    )


@dataclass(frozen=True)
class OptionsSnapshot:
    """Strukturierter Gesamt-Snapshot der Deribit Optionskette."""
    ts: datetime              # UTC, Berechnungszeitpunkt
    chain_ts: datetime        # UTC, Zeitstempel des letzten Ketten-Fetches
    spot: float               # Verwendeter Spotpreis
    stale: bool               # True wenn Fetch fehlgeschlagen oder veraltet
    expiries: tuple[ExpiryGamma, ...]
    skipped_instruments: int  # Anzahl uebersprungener/unparsbarer Instrumente
    reversal_flag: Optional[ExpiryReversalFlag] = None
    chain_age_seconds: float = 0.0  # Alter der zugrunde liegenden Kette in Sekunden


# ── Parsing- und Rechenfunktionen ─────────────────────────────────────────────

def parse_instrument_name(name: str) -> Optional[Tuple[datetime, float, str]]:
    """
    Parst Deribit Optionsnamen wie 'BTC-27JUN26-90000-C'.
    Rueckgabe: (expiry_datetime_utc, strike_float, 'C'|'P') oder None bei Ungueltigkeit.
    Deribit Optionen verfallen stets um 08:00:00 UTC.
    """
    if not isinstance(name, str):
        return None
    
    match = INSTRUMENT_PATTERN.match(name.strip().upper())
    if not match:
        return None
    
    day_str = match.group("day")
    month_str = match.group("month")
    year_str = match.group("year")
    strike_str = match.group("strike")
    opt_type = match.group("type")
    
    if month_str not in MONTH_MAP:
        return None
    
    try:
        day = int(day_str)
        month = MONTH_MAP[month_str]
        # 2-stelliges Jahr (z.B. 26 -> 2026)
        year = 2000 + int(year_str)
        strike = float(strike_str)
        
        # Validierung des Datums
        expiry_dt = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)
        return expiry_dt, strike, opt_type
    except (ValueError, OverflowError):
        return None


def calculate_t_years(expiry_utc: datetime, now_utc: datetime) -> float:
    """
    Berechnet die Restlaufzeit T in Jahren mit 15-Minuten Floor (Spec §4.1).
    Verhindert Gamma-Divergenzen bei T -> 0.
    """
    diff_sec = (expiry_utc - now_utc).total_seconds()
    effective_sec = max(diff_sec, EXPIRY_FLOOR_SECONDS)
    return effective_sec / SECONDS_PER_YEAR


def calculate_bs_gamma(spot: float, strike: float, mark_iv: float, t_years: float) -> float:
    """
    Berechnet Standard Black-Scholes Gamma:
    d1 = (ln(S/K) + 0.5 * sigma^2 * T) / (sigma * sqrt(T))
    gamma = exp(-0.5 * d1^2) / (S * sigma * sqrt(2 * pi * T))
    """
    if spot <= 0.0 or strike <= 0.0 or mark_iv <= 0.0 or t_years <= 0.0:
        return 0.0
    
    sigma = mark_iv
    t_sqrt = math.sqrt(t_years)
    denom = sigma * t_sqrt
    if denom <= 0.0:
        return 0.0
    
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / denom
    exp_term = math.exp(-0.5 * d1 * d1)
    gamma = exp_term / (spot * sigma * t_sqrt * math.sqrt(2.0 * math.pi))
    return gamma


def calculate_gex_usd(gamma: float, open_interest_btc: float, spot: float, option_type: str) -> float:
    """
    Berechnet Gamma Exposure in USD pro 1% Spot-Move fuer inverse Deribit Kontrakte (Spec §3).
    
    Deribit BTC Optionen sind invers: 1 Kontrakt = 1 BTC.
    Umrechnung in USD:
      - 1x Spot fuer USD-Notional des Kontrakts (OI * Spot)
      - 1x Spot fuer die Standard-GEX-Formel (Dollar-Delta pro 1% Move = Gamma * S * 1% * Notional)
      -> gex_usd = gamma * OI * S * S * 0.01 * sign
    
    Dealer-Konvention (Spec §4.3):
      - Call = +1 (Market Maker long Gamma -> Daempfung / Mean-Reversion)
      - Put = -1 (Market Maker short Gamma -> Verstaerkung / Accelerant)
    """
    if gamma <= 0.0 or open_interest_btc <= 0.0 or spot <= 0.0:
        return 0.0
    
    sign = 1.0 if option_type.upper() == "C" else -1.0
    return gamma * open_interest_btc * spot * spot * 0.01 * sign


def calculate_zero_gamma(strikes: List[StrikeGamma]) -> Optional[float]:
    """
    Findet den Zero-Gamma-Level durch aufsteigendes Kumulieren von GEX_k
    und lineare Interpolation zwischen den beiden umklammernden Strikes (Spec §4.4).
    Gibt None zurueck, wenn kein Nulldurchgang im Bereich existiert.
    """
    if not strikes:
        return None
    
    # Sortiere Strikes aufsteigend
    sorted_strikes = sorted(strikes, key=lambda s: s.strike)
    
    # Aggregiere Netto-GEX pro eindeutigem Strike
    strike_gex_map: Dict[float, float] = {}
    for s in sorted_strikes:
        strike_gex_map[s.strike] = strike_gex_map.get(s.strike, 0.0) + s.gex_usd
    
    unique_strikes = sorted(strike_gex_map.keys())
    if len(unique_strikes) < 2:
        return None
    
    cum_gex = 0.0
    cum_values: List[Tuple[float, float]] = []
    for k in unique_strikes:
        cum_gex += strike_gex_map[k]
        cum_values.append((k, cum_gex))
    
    # Exakter Treffer oder Nulldurchgang suchen
    for i in range(len(cum_values) - 1):
        k1, c1 = cum_values[i]
        k2, c2 = cum_values[i + 1]
        
        if c1 == 0.0:
            return k1
        
        # Vorzeichenwechsel zwischen k1 und k2
        if (c1 < 0.0 and c2 > 0.0) or (c1 > 0.0 and c2 < 0.0):
            # Lineare Interpolation: k_zero = k1 + (0 - c1) * (k2 - k1) / (c2 - c1)
            k_zero = k1 - c1 * (k2 - k1) / (c2 - c1)
            return float(k_zero)
    
    if cum_values[-1][1] == 0.0:
        return cum_values[-1][0]
    
    return None


def calculate_aggregates(
    expiry_dt: datetime,
    label: str,
    raw_instruments: List[Dict[str, Any]],
    spot: float,
    now_utc: datetime
) -> Tuple[ExpiryGamma, int]:
    """
    Berechnet alle Aggregat-Metriken fuer eine einzelne Verfallsgruppe (Spec §4.4).
    """
    t_years = calculate_t_years(expiry_dt, now_utc)
    strikes_list: List[StrikeGamma] = []
    skipped = 0
    
    sum_atm_iv_weight = 0.0
    sum_atm_weight = 0.0
    atm_oi_btc = 0.0
    
    # Bestehende Walls
    put_oi_below: Dict[float, float] = {}
    call_oi_above: Dict[float, float] = {}
    
    for item in raw_instruments:
        strike = item["strike"]
        opt_type = item["type"]
        oi = item["oi"]
        mark_iv = item["mark_iv"]
        
        # Filterung ungueltiger Werte
        if oi <= 0.0 or mark_iv <= 0.0 or math.isnan(mark_iv) or math.isnan(oi):
            skipped += 1
            continue
        
        gamma = calculate_bs_gamma(spot, strike, mark_iv, t_years)
        gex_usd = calculate_gex_usd(gamma, oi, spot, opt_type)
        
        strike_gamma = StrikeGamma(
            strike=float(strike),
            option_type=opt_type,
            open_interest=float(oi),
            mark_iv=float(mark_iv),
            gamma=float(gamma),
            gex_usd=float(gex_usd)
        )
        strikes_list.append(strike_gamma)
        
        # ATM OI (innerhalb +/- 2.5% von Spot, Weiss et al. 2026)
        pct_dist = abs(strike - spot) / spot
        if pct_dist <= 0.025:
            atm_oi_btc += oi
        
        # Gewichtete ATM IV (innerhalb +/- 5% von Spot fuer Expected Move)
        if pct_dist <= 0.05:
            sum_atm_iv_weight += mark_iv * oi
            sum_atm_weight += oi
        
        # Wall Tracking
        if opt_type == "P" and strike < spot:
            put_oi_below[strike] = put_oi_below.get(strike, 0.0) + oi
        elif opt_type == "C" and strike > spot:
            call_oi_above[strike] = call_oi_above.get(strike, 0.0) + oi
    
    # ATM IV: gewichtetes Mittel im 5%-Band, sonst naechstgelegener Strike.
    # Gibt es ueberhaupt keinen verwertbaren Strike, bleibt die IV unbekannt —
    # es wird kein Ersatzwert gesetzt (Charter §2), und der Expected Move
    # damit None statt einer Zahl ohne Grundlage.
    iv_atm: Optional[float]
    if sum_atm_weight > 0.0:
        iv_atm = sum_atm_iv_weight / sum_atm_weight
    elif strikes_list:
        closest = min(strikes_list, key=lambda s: abs(s.strike - spot))
        iv_atm = closest.mark_iv
    else:
        iv_atm = None
    
    net_gex = sum(s.gex_usd for s in strikes_list)
    zero_gamma = calculate_zero_gamma(strikes_list)
    
    # Put Wall: Maximum Put OI unterhalb Spot
    put_wall = max(put_oi_below.items(), key=lambda x: x[1])[0] if put_oi_below else None
    
    # Call Wall: Maximum Call OI oberhalb Spot
    call_wall = max(call_oi_above.items(), key=lambda x: x[1])[0] if call_oi_above else None
    
    # Expected Move: S * iv_atm * sqrt(T_years) — nur bei bekannter IV
    expected_move = spot * iv_atm * math.sqrt(t_years) if iv_atm is not None else None
    
    expiry_gamma = ExpiryGamma(
        expiry=expiry_dt,
        label=label,
        t_years=float(t_years),
        strikes=tuple(strikes_list),
        net_gex=float(net_gex),
        zero_gamma=zero_gamma,
        put_wall=put_wall,
        call_wall=call_wall,
        atm_oi=float(atm_oi_btc),
        expected_move=float(expected_move) if expected_move is not None else None,
        iv_atm=float(iv_atm) if iv_atm is not None else None
    )
    
    return expiry_gamma, skipped


def resolve_expiry_groups(
    parsed_by_expiry: Dict[datetime, List[Dict[str, Any]]],
    now_utc: datetime
) -> Dict[str, datetime]:
    """
    Bestimmt die drei Schluessel-Verfallsgruppen (0DTE, Weekly, Monthly) (Spec §4.5).
    - 0DTE: Verfall an der naechsten 08:00 UTC Grenze
    - Weekly: Naechster Freitag nach 0DTE (oder naechster Verfall > 0DTE)
    - Monthly: Naechster Monatsletzter-Freitag (Last Friday)
    """
    all_expiries = sorted(parsed_by_expiry.keys())
    if not all_expiries:
        return {}
    
    # 1. Bestimme 0DTE-Grenze
    # Wenn now_utc vor 08:00 UTC liegt -> 08:00 UTC heute; sonst 08:00 UTC morgen
    today_0800 = datetime(now_utc.year, now_utc.month, now_utc.day, 8, 0, 0, tzinfo=timezone.utc)
    if now_utc < today_0800:
        target_0dte = today_0800
    else:
        target_0dte = today_0800 + timedelta(days=1)
    
    # Finde naechste Verfaelle >= now_utc
    future_expiries = [e for e in all_expiries if e >= now_utc - timedelta(minutes=15)]
    if not future_expiries:
        future_expiries = all_expiries
    
    # 0DTE Expiry: Genau target_0dte oder der am naechsten liegende zukuenftige Verfall
    zero_dte_exp = next((e for e in future_expiries if e == target_0dte), future_expiries[0])
    
    # Weekly Expiry: Naechster Freitag-Verfall nach 0DTE (oder zweiter Verfall)
    remaining_after_0dte = [e for e in future_expiries if e > zero_dte_exp]
    weekly_candidates = [e for e in remaining_after_0dte if e.weekday() == 4]  # 4 = Freitag
    if weekly_candidates:
        weekly_exp = weekly_candidates[0]
    elif remaining_after_0dte:
        weekly_exp = remaining_after_0dte[0]
    else:
        weekly_exp = zero_dte_exp
    
    # Monthly Expiry: Finde den letzten Freitag eines Monats
    def is_last_friday_of_month(dt: datetime) -> bool:
        if dt.weekday() != 4:
            return False
        # Wenn 7 Tage spaeter ein anderer Monat ist, ist dies der letzte Freitag
        next_week = dt + timedelta(days=7)
        return next_week.month != dt.month
    
    monthly_candidates = [e for e in future_expiries if is_last_friday_of_month(e)]
    if monthly_candidates:
        monthly_exp = monthly_candidates[0]
    else:
        # Fallback auf am weitesten entfernten Verfall der naechsten 45 Tage
        monthly_exp = future_expiries[-1]
    
    return {
        "0dte": zero_dte_exp,
        "weekly": weekly_exp,
        "monthly": monthly_exp
    }


# ── Historie & 08:00 UTC Reversal Flag (Weiss et al. 2026, Spec §6) ───────────

class HistoryManager:
    """Verwaltet und persistiert die 07:00 UTC ATM-OI Historie fuer das Top-Dezil."""

    def __init__(self, filepath: Path = ATM_OI_HISTORY_FILE) -> None:
        self.filepath = filepath
        self._history: Dict[str, float] = {}  # date_str -> atm_oi_btc
        self._load()

    def _load(self) -> None:
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._history = json.load(f)
            except Exception as exc:
                _throttled_warn("history_load_err", f"Konnte ATM-OI Historie nicht laden: {exc}")
                self._history = {}

    def _save(self) -> None:
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._history, f, indent=2)
        except Exception as exc:
            _throttled_warn("history_save_err", f"Konnte ATM-OI Historie nicht speichern: {exc}")

    def record_if_0700_utc(self, now_utc: datetime, atm_oi_0dte: float) -> None:
        """Speichert den 07:00 UTC Wert einmal taeglich ab."""
        if now_utc.hour == 7 and 0 <= now_utc.minute <= 5:
            date_key = now_utc.strftime("%Y-%m-%d")
            if date_key not in self._history:
                self._history[date_key] = atm_oi_0dte
                self._save()

    def record_direct(self, date_str: str, atm_oi: float) -> None:
        """Direktes Hinzufuegen von Test- oder Backfill-Daten."""
        self._history[date_str] = atm_oi
        self._save()

    def evaluate_flag(self, atm_oi_0dte: float, net_gex_0dte: float) -> ExpiryReversalFlag:
        """
        Prueft die beiden Bedingungen:
        1. ATM-OI im obersten Dezil (Top 10% >= 90. Perzentil)
        2. Net-GEX negativ
        Gibt None fuer active zurueck bei < 60 Tagen Historie.
        """
        count = len(self._history)
        if count < MIN_HISTORY_FOR_EXPIRY_DECILE:
            return ExpiryReversalFlag(
                active=None,
                atm_oi_0dte=atm_oi_0dte,
                net_gex_0dte=net_gex_0dte,
                top_decile_threshold=None,
                history_count=count
            )
        
        values = sorted(self._history.values())
        # 90. Perzentil Index
        idx = int(math.ceil(0.90 * len(values))) - 1
        threshold = values[max(0, min(idx, len(values) - 1))]
        
        is_top_decile = atm_oi_0dte >= threshold
        is_neg_gamma = net_gex_0dte < 0.0
        active = is_top_decile and is_neg_gamma
        
        return ExpiryReversalFlag(
            active=active,
            atm_oi_0dte=atm_oi_0dte,
            net_gex_0dte=net_gex_0dte,
            top_decile_threshold=float(threshold),
            history_count=count
        )


# ── Deribit REST Client & Fetcher ─────────────────────────────────────────────

def fetch_deribit_chain_sync() -> List[Dict[str, Any]]:
    """
    Synchroner HTTP-Fetch des Deribit Book Summary Endpoints.
    Verwendet standard urllib.request ohne externe Abhaengigkeiten.
    """
    req = urllib.request.Request(
        DERIBIT_BOOK_SUMMARY_URL,
        headers={"User-Agent": "OrderFlowPro/2.0 OptionsAgent"}
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        if resp.status != 200:
            raise ValueError(f"Deribit API HTTP Status {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result", [])
        if not isinstance(result, list):
            raise ValueError("Deribit Response 'result' ist keine Liste")
        return result


async def fetch_deribit_chain_async() -> List[Dict[str, Any]]:
    """Asynchroner Wrapper um den synchronen Fetch."""
    return await asyncio.to_thread(fetch_deribit_chain_sync)


# ── OptionsAgent Engine ───────────────────────────────────────────────────────

class OptionsAgent:
    """
    OptionsAgent verwaltet zwei Taktraten (Spec §2):
    1. Kettentakt: Polling von Deribit alle 30-60s (OI bewegt sich langsam).
    2. Spottakt: Sofortige Gamma- und GEX-Neuberechnung bei jedem Spot-Tick.
    """

    def __init__(self, broker: Broker, history_manager: Optional[HistoryManager] = None) -> None:
        self.broker = broker
        self.history_mgr = history_manager or HistoryManager()
        
        self._raw_chain: List[Dict[str, Any]] = []
        self._chain_ts: datetime = datetime.now(timezone.utc)
        # Kein Default-Spot: bis zum ersten Tick ist der Spot unbekannt, und ein
        # erfundener Spot wuerde eine vollstaendige, falsche Karte erzeugen.
        self._current_spot: float = 0.0
        self._last_snapshot: Optional[OptionsSnapshot] = None
        self._stale: bool = True
        self._fetch_lock = asyncio.Lock()

    def set_spot(self, spot: float) -> Optional[OptionsSnapshot]:
        """Aktualisiert den Spotpreis und rechnet das GEX-Profil sofort neu."""
        if spot <= 0.0 or math.isnan(spot):
            return None
        self._current_spot = spot
        snap = self.recompute(now_utc=datetime.now(timezone.utc))
        if snap:
            self._last_snapshot = snap
        return snap

    def recompute(self, now_utc: Optional[datetime] = None) -> Optional[OptionsSnapshot]:
        """
        Fuehrt die vollstaendige Gamma- und Aggregatsberechnung deterministisch durch.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        
        chain_age = max(0.0, (now_utc - self._chain_ts).total_seconds())
        
        if not self._raw_chain:
            # Letzter echter Snapshot mit sichtbarem Alter — aber nur solange er
            # nicht ueberaltert ist. Danach Leerzustand statt einer Karte, die
            # aussieht wie Marktzustand, aber Vergangenheit zeigt (Charter §2).
            if self._last_snapshot and chain_age <= MAX_CHAIN_AGE_SECONDS:
                return dataclasses.replace(
                    self._last_snapshot,
                    ts=now_utc,
                    stale=True,
                    chain_age_seconds=chain_age
                )
            if self._last_snapshot:
                _throttled_warn(
                    "chain_expired",
                    f"Optionskette {chain_age:.0f}s alt (Limit {MAX_CHAIN_AGE_SECONDS:.0f}s) — "
                    "kein Snapshot, Anzeige bleibt leer."
                )
            return None
        
        spot = self._current_spot
        if spot <= 0.0:
            # Noch kein Spot-Tick empfangen. Ohne Spot ist kein Gamma rechenbar.
            _throttled_warn("no_spot", "Noch kein Spot-Tick empfangen — Options-Snapshot ausgesetzt.")
            return None
        parsed_by_expiry: Dict[datetime, List[Dict[str, Any]]] = {}
        total_skipped = 0
        
        # 1. Kette parsen
        for item in self._raw_chain:
            name = item.get("instrument_name")
            parsed = parse_instrument_name(name)
            if not parsed:
                total_skipped += 1
                continue
            
            exp_dt, strike, opt_type = parsed
            oi = float(item.get("open_interest") or 0.0)
            mark_iv_pct = float(item.get("mark_iv") or 0.0)
            mark_iv = mark_iv_pct / 100.0  # Prozent -> Dezimal (z.B. 55.8 -> 0.558)
            
            if exp_dt not in parsed_by_expiry:
                parsed_by_expiry[exp_dt] = []
            
            parsed_by_expiry[exp_dt].append({
                "instrument_name": name,
                "strike": strike,
                "type": opt_type,
                "oi": oi,
                "mark_iv": mark_iv,
                "underlying_price": float(item.get("underlying_price") or spot)
            })
        
        if not parsed_by_expiry:
            return None
        
        # 2. Verfallsgruppen ermitteln (0DTE, Weekly, Monthly)
        groups = resolve_expiry_groups(parsed_by_expiry, now_utc)
        expiry_gammas: List[ExpiryGamma] = []
        
        for label, exp_dt in groups.items():
            raw_instruments = parsed_by_expiry.get(exp_dt, [])
            eg, skipped_in_group = calculate_aggregates(exp_dt, label, raw_instruments, spot, now_utc)
            expiry_gammas.append(eg)
            total_skipped += skipped_in_group
        
        # 3. 08:00 UTC Reversal Flag ermitteln
        flag = None
        zero_dte_group = next((eg for eg in expiry_gammas if eg.label == "0dte"), None)
        if zero_dte_group:
            self.history_mgr.record_if_0700_utc(now_utc, zero_dte_group.atm_oi)
            flag = self.history_mgr.evaluate_flag(zero_dte_group.atm_oi, zero_dte_group.net_gex)
        
        snapshot = OptionsSnapshot(
            ts=now_utc,
            chain_ts=self._chain_ts,
            spot=spot,
            stale=self._stale,
            expiries=tuple(expiry_gammas),
            skipped_instruments=total_skipped,
            reversal_flag=flag,
            chain_age_seconds=chain_age
        )
        return snapshot

    async def fetch_and_update(self) -> Optional[OptionsSnapshot]:
        """
        Ruft die Deribit REST API ab, aktualisiert den internen Ketten-Cache
        und publiziert den Snapshot auf dem Broker.
        """
        async with self._fetch_lock:
            now_utc = datetime.now(timezone.utc)
            try:
                raw_items = await fetch_deribit_chain_async()
                if not raw_items:
                    _throttled_warn("empty_chain", "Deribit Rueckgabe war leer, Snapshot wird als stale markiert.")
                    self._stale = True
                else:
                    self._raw_chain = raw_items
                    self._chain_ts = now_utc
                    self._stale = False
            except Exception as exc:
                _throttled_warn("fetch_fail", f"Fehler beim Abrufen der Deribit Optionskette: {exc}")
                self._stale = True
            
            snap = self.recompute(now_utc=now_utc)
            if snap:
                self._last_snapshot = snap
                await self.broker.publish(OPTIONS, snap)
                await self.broker.publish(OPTIONS_SNAPSHOT, snap)
            return snap


# ── Agent Main Loop (Spec §2 Contract) ────────────────────────────────────────

async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    """
    Agent-Einstiegspunkt nach Standard-Agentenvertrag:
    async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    
    Verbindet Spot-Updates ueber Broker-Channels (AGGREGATED, TRADES)
    und fuehrt alle 45s einen Kettentakt-Fetch aus.
    """
    agent = OptionsAgent(broker)
    
    # Callback fuer Spot-Ticks vom Aggregator / Exchange Feed
    async def on_spot_update(msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        spot = msg.get("mid_price") or msg.get("price") or msg.get("spot")
        if spot and isinstance(spot, (int, float)) and spot > 0:
            snap = agent.set_spot(float(spot))
            if snap:
                await broker.publish(OPTIONS, snap)
                await broker.publish(OPTIONS_SNAPSHOT, snap)
    
    broker.subscribe(AGGREGATED, on_spot_update)
    broker.subscribe(TRADES, on_spot_update)
    
    logger.info("OptionsAgent gestartet (Deribit BTC GEX Engine).")
    
    # Initialer Fetch
    try:
        await agent.fetch_and_update()
    except Exception as exc:
        _throttled_warn("init_fetch_err", f"Initialer Fetch fehlgeschlagen: {exc}")
    
    # Kettentakt-Loop (alle 45 Sekunden)
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=45.0)
            break
        except asyncio.TimeoutError:
            pass
        
        try:
            await agent.fetch_and_update()
        except Exception as exc:
            _throttled_warn("poll_loop_err", f"Fehler im OptionsAgent Polling-Loop: {exc}")
            await asyncio.sleep(5.0)
    
    logger.info("OptionsAgent beendet.")
