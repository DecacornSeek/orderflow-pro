"""
scripts/gex_scaling_probe.py — Entscheidungshilfe zu docs/GEX_SCALING.md.

Rechnet dieselbe Deribit-Optionskette unter beiden GEX-Konventionen durch und
stellt Net-GEX, Zero-Gamma, Walls und Regime-Label gegenueber:

  A) USD-Sicht:  Gamma_BS  * OI * S^2 * 1%   (das, was der Code heute rechnet)
  B) BTC-Sicht:  Gamma_BTC * OI * S^3 * 1%   (Charter §8, Deribit-Greek-Konvention)

Gamma_BTC = Gamma/S - 2*Delta/S^2 + 2*V/S^3 — exakt, nicht nur der Fuehrungsterm.

Das Skript veraendert nichts am Produktionspfad. Es liest nur.

Aufruf:
    python3 scripts/gex_scaling_probe.py                     # Live-Fetch
    python3 scripts/gex_scaling_probe.py --save chain.json   # Kette mitschreiben
    python3 scripts/gex_scaling_probe.py --file chain.json    # offline nachrechnen
    python3 scripts/gex_scaling_probe.py --spot 98000         # Spot vorgeben
"""

import argparse
from datetime import datetime, timezone
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.options_agent import (  # noqa: E402
    calculate_bs_gamma,
    calculate_t_years,
    fetch_deribit_chain_sync,
    parse_instrument_name,
    resolve_expiry_groups,
)


def _norm_cdf(x: float) -> float:
    """Standardnormale Verteilungsfunktion."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price_and_delta(spot: float, strike: float, sigma: float, t_years: float,
                       option_type: str) -> Tuple[float, float]:
    """
    Black-Scholes USD-Preis und Delta (r = 0, wie im Options-Agent).
    Rueckgabe: (V_USD, Delta).
    """
    if spot <= 0.0 or strike <= 0.0 or sigma <= 0.0 or t_years <= 0.0:
        return 0.0, 0.0
    t_sqrt = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / (sigma * t_sqrt)
    d2 = d1 - sigma * t_sqrt
    if option_type.upper() == "C":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2), _norm_cdf(d1)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1), _norm_cdf(d1) - 1.0


def gamma_btc_denominated(spot: float, strike: float, sigma: float, t_years: float,
                          option_type: str) -> float:
    """
    Zweite Ableitung des BTC-denominierten Optionswerts nach Spot.

        V_BTC   = V_USD / S
        G_BTC   = Gamma/S - 2*Delta/S^2 + 2*V/S^3

    Wird bei tief im Geld liegenden Calls negativ — der BTC-Payoff 1 - K/S ist
    dort konkav in S. Das ist kein Fehler, siehe docs/GEX_SCALING.md §5.
    """
    if spot <= 0.0:
        return 0.0
    gamma = calculate_bs_gamma(spot, strike, sigma, t_years)
    value, delta = bs_price_and_delta(spot, strike, sigma, t_years, option_type)
    return gamma / spot - 2.0 * delta / (spot ** 2) + 2.0 * value / (spot ** 3)


def zero_gamma_from_pairs(pairs: List[Tuple[float, float]]) -> Optional[float]:
    """
    Kumuliert GEX ueber aufsteigende Strikes und interpoliert den Nulldurchgang
    linear. Gibt None zurueck, wenn kein Vorzeichenwechsel existiert — es wird
    kein Ersatzwert erfunden (Charter §2).
    """
    if len(pairs) < 2:
        return None
    merged: Dict[float, float] = {}
    for strike, gex in pairs:
        merged[strike] = merged.get(strike, 0.0) + gex

    cum = 0.0
    curve: List[Tuple[float, float]] = []
    for strike in sorted(merged):
        cum += merged[strike]
        curve.append((strike, cum))

    for i in range(len(curve) - 1):
        k1, c1 = curve[i]
        k2, c2 = curve[i + 1]
        if c1 == 0.0:
            return k1
        if (c1 < 0.0 < c2) or (c2 < 0.0 < c1):
            return k1 - c1 * (k2 - k1) / (c2 - c1)
    return None


def analyse(instruments: List[Dict[str, Any]], spot: float, expiry_dt: datetime,
            now_utc: datetime) -> Dict[str, Any]:
    """Rechnet eine Verfallsgruppe unter beiden Konventionen durch."""
    t_years = calculate_t_years(expiry_dt, now_utc)

    net_usd = 0.0
    net_btc = 0.0
    pairs_usd: List[Tuple[float, float]] = []
    pairs_btc: List[Tuple[float, float]] = []
    sign_flips = 0
    call_oi_above: Dict[float, float] = {}
    put_oi_below: Dict[float, float] = {}

    for item in instruments:
        strike = item["strike"]
        opt_type = item["type"]
        oi = item["oi"]
        sigma = item["mark_iv"]
        if oi <= 0.0 or sigma <= 0.0 or math.isnan(oi) or math.isnan(sigma):
            continue

        sign = 1.0 if opt_type == "C" else -1.0

        gamma_usd = calculate_bs_gamma(spot, strike, sigma, t_years)
        gex_usd = gamma_usd * oi * spot * spot * 0.01 * sign

        gamma_btc = gamma_btc_denominated(spot, strike, sigma, t_years, opt_type)
        gex_btc = gamma_btc * oi * (spot ** 3) * 0.01 * sign

        # Vorzeichenwechsel zwischen den Konventionen zaehlen (nur echte Beitraege)
        if gex_usd != 0.0 and gex_btc != 0.0 and (gex_usd > 0.0) != (gex_btc > 0.0):
            sign_flips += 1

        net_usd += gex_usd
        net_btc += gex_btc
        pairs_usd.append((strike, gex_usd))
        pairs_btc.append((strike, gex_btc))

        if opt_type == "C" and strike > spot:
            call_oi_above[strike] = call_oi_above.get(strike, 0.0) + oi
        elif opt_type == "P" and strike < spot:
            put_oi_below[strike] = put_oi_below.get(strike, 0.0) + oi

    return {
        "t_years": t_years,
        "contracts": len(pairs_usd),
        "net_usd": net_usd,
        "net_btc": net_btc,
        "zero_usd": zero_gamma_from_pairs(pairs_usd),
        "zero_btc": zero_gamma_from_pairs(pairs_btc),
        "call_wall": max(call_oi_above.items(), key=lambda x: x[1])[0] if call_oi_above else None,
        "put_wall": max(put_oi_below.items(), key=lambda x: x[1])[0] if put_oi_below else None,
        "sign_flips": sign_flips,
    }


def _fmt(value: Optional[float], suffix: str = "", nd: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{nd}f}{suffix}"


def _regime(net_gex: float) -> str:
    return "SHORT GAMMA (verstaerkend)" if net_gex < 0 else "LONG GAMMA (daempfend)"


def main() -> int:
    parser = argparse.ArgumentParser(description="GEX-Skalierung S^2 vs S^3 gegenueberstellen")
    parser.add_argument("--file", help="Ketten-JSON statt Live-Fetch lesen")
    parser.add_argument("--save", help="Live-Kette nach dem Fetch hierhin schreiben")
    parser.add_argument("--spot", type=float, help="Spot vorgeben (sonst aus underlying_price)")
    args = parser.parse_args()

    if args.file:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        chain = raw.get("result", raw) if isinstance(raw, dict) else raw
        print(f"Kette aus Datei: {args.file}")
    else:
        try:
            chain = fetch_deribit_chain_sync()
        except Exception as exc:
            print(f"Deribit-Fetch fehlgeschlagen: {exc}", file=sys.stderr)
            print("Bei blockiertem Egress: Kette anderswo ziehen und --file nutzen.", file=sys.stderr)
            return 1
        if args.save:
            Path(args.save).write_text(json.dumps(chain), encoding="utf-8")
            print(f"Kette gespeichert: {args.save}")

    now_utc = datetime.now(timezone.utc)
    parsed_by_expiry: Dict[datetime, List[Dict[str, Any]]] = {}
    underlying_prices: List[float] = []

    for item in chain:
        parsed = parse_instrument_name(item.get("instrument_name"))
        if not parsed:
            continue
        exp_dt, strike, opt_type = parsed
        underlying = float(item.get("underlying_price") or 0.0)
        if underlying > 0.0:
            underlying_prices.append(underlying)
        parsed_by_expiry.setdefault(exp_dt, []).append({
            "strike": strike,
            "type": opt_type,
            "oi": float(item.get("open_interest") or 0.0),
            "mark_iv": float(item.get("mark_iv") or 0.0) / 100.0,
        })

    if not parsed_by_expiry:
        print("Keine parsbaren BTC-Optionen in der Kette.", file=sys.stderr)
        return 1

    spot = args.spot or (sum(underlying_prices) / len(underlying_prices) if underlying_prices else 0.0)
    if spot <= 0.0:
        print("Kein Spotpreis ermittelbar — --spot setzen.", file=sys.stderr)
        return 1

    groups = resolve_expiry_groups(parsed_by_expiry, now_utc)

    print()
    print(f"Spot: {spot:,.0f} USD   |   {now_utc:%Y-%m-%d %H:%M UTC}   |   "
          f"{len(chain)} Instrumente, {len(parsed_by_expiry)} Verfallstermine")
    print("=" * 78)

    for label, exp_dt in groups.items():
        res = analyse(parsed_by_expiry[exp_dt], spot, exp_dt, now_utc)
        days = res["t_years"] * 365.0

        print()
        print(f"── {label.upper()}  ({exp_dt:%d%b%y 08:00 UTC}, {days:.2f} Tage, "
              f"{res['contracts']} Kontrakte mit OI)")
        print(f"{'':22} {'A) USD-Sicht S^2':>22} {'B) BTC-Sicht S^3':>22}")
        print(f"{'Net GEX (USD/1%)':22} {res['net_usd']:>22,.0f} {res['net_btc']:>22,.0f}")
        print(f"{'Regime':22} {_regime(res['net_usd']):>22} {_regime(res['net_btc']):>22}")
        print(f"{'Zero-Gamma':22} {_fmt(res['zero_usd']):>22} {_fmt(res['zero_btc']):>22}")

        if res["zero_usd"] is not None and res["zero_btc"] is not None:
            delta = res["zero_btc"] - res["zero_usd"]
            print(f"{'  Differenz':22} {'':>22} {delta:>+22,.0f} USD "
                  f"({delta / spot * 100:+.2f}% vom Spot)")

        print(f"{'Call-Wall (OI)':22} {_fmt(res['call_wall']):>22} {'identisch':>22}")
        print(f"{'Put-Wall (OI)':22} {_fmt(res['put_wall']):>22} {'identisch':>22}")
        print(f"{'Strikes mit Vorz.-Wechsel':22} {res['sign_flips']:>22} von {res['contracts']}")

        if res["net_usd"] * res["net_btc"] < 0.0:
            print("  ACHTUNG: Die beiden Konventionen liefern GEGENSAETZLICHE Regime-Labels.")
        if res["zero_usd"] is None or res["zero_btc"] is None:
            print("  Hinweis: In mindestens einer Konvention existiert kein Nulldurchgang.")

    print()
    print("=" * 78)
    print("Lesart: Liegen beide Zero-Gamma nah beieinander, ist die Marke belastbar.")
    print("Laufen sie auseinander, gehoert die Spanne ins UI — nicht ein Punkt.")
    print("Herleitung: docs/GEX_SCALING.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
