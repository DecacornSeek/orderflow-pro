import { Broker } from "./broker.js";

export const OPTIONS = "OPTIONS";

export interface OptionStrikeCluster {
  strike: number;
  call_oi: number;
  put_oi: number;
  total_oi: number;
  call_gex: number;
  put_gex: number;
  net_gex: number; // in USD
  iv: number;
  is_atm?: boolean;
}

export interface ExpiryGroup {
  expiry_code: string; // e.g. "28AUG26" or "0DTE"
  expiry_date: string;
  days_to_expiry: number;
  total_oi_btc: number;
  net_gex_usd: number;
  atm_iv: number;
  expected_move_usd: number;
  expected_move_pct: number;
  put_wall: number;
  call_wall: number;
  zero_gamma: number | null;
  max_pain: number;
  strikes: OptionStrikeCluster[];
}

export interface OptionsSnapshot {
  timestamp: number;
  spot_price: number;
  source: "deribit_live" | "deribit_cached" | "synthetic_model";
  total_oi_btc: number;
  atm_oi_btc?: number;
  atm_oi_usd?: number;
  net_gex_usd: number;
  gex_regime: "AMPLIFYING" | "DAMPENING"; // net_gex < 0 -> AMPLIFYING (accelerant); net_gex > 0 -> DAMPENING (pinning)
  zero_gamma: number | null;
  put_wall: number;
  call_wall: number;
  max_pain: number;
  atm_iv: number;
  realised_vol_24h: number;
  iv_rv_ratio: number;
  expected_move_0dte: number;
  expected_move_weekly: number;
  expiry_groups: Record<string, ExpiryGroup>;
  top_clusters: OptionStrikeCluster[];
  expiry_reversal_flag: {
    active: boolean;
    utc_hour: number;
    utc_minute: number;
    is_top_decile_oi: boolean;
    is_negative_gamma: boolean;
    atm_oi_usd?: number;
    headline: string;
    detail: string;
    caveats: string[];
  };
}

export class OptionsAgent {
  private broker: Broker;
  private isRunning = false;
  private pollInterval: NodeJS.Timeout | null = null;
  private lastSnapshot: OptionsSnapshot | null = null;
  private lastFetchTime = 0;
  private spotPrice = 64500.0;
  private cachedRawData: any[] | null = null;

  constructor(broker: Broker) {
    this.broker = broker;
  }

  setSpotPrice(spot: number): void {
    this.spotPrice = spot;
  }

  async start(): Promise<void> {
    this.isRunning = true;
    await this.pollDeribit();
    // Poll Deribit every 35 seconds (slow moving chain)
    this.pollInterval = setInterval(() => {
      this.pollDeribit();
    }, 35000);
  }

  stop(): void {
    this.isRunning = false;
    if (this.pollInterval) clearInterval(this.pollInterval);
  }

  getSnapshot(): OptionsSnapshot {
    if (this.lastSnapshot) {
      // Re-update with latest spot price
      return this.computeModel(this.spotPrice, this.cachedRawData);
    }
    return this.generateBaseline(this.spotPrice);
  }

  private async pollDeribit(): Promise<void> {
    try {
      const url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option";
      const res = await fetch(url, { signal: AbortSignal.timeout(6000) });
      if (res.ok) {
        const json = await res.json();
        if (json && Array.isArray(json.result) && json.result.length > 0) {
          this.cachedRawData = json.result;
          this.lastFetchTime = Date.now();
          const snap = this.computeModel(this.spotPrice, json.result, "deribit_live");
          this.lastSnapshot = snap;
          this.broker.publish(OPTIONS, snap);
          return;
        }
      }
    } catch (err) {
      // Network timeout or Deribit API rate-limit
    }

    // If fetch failed, use cached data or generate mathematically sound baseline
    const source = this.cachedRawData ? "deribit_cached" : "synthetic_model";
    const snap = this.computeModel(this.spotPrice, this.cachedRawData, source);
    this.lastSnapshot = snap;
    this.broker.publish(OPTIONS, snap);
  }

  private computeModel(
    spot: number,
    rawInstruments: any[] | null,
    source: "deribit_live" | "deribit_cached" | "synthetic_model" = "synthetic_model"
  ): OptionsSnapshot {
    if (!rawInstruments || rawInstruments.length === 0) {
      return this.generateBaseline(spot);
    }

    const clustersMap = new Map<number, OptionStrikeCluster>();
    const expiriesMap = new Map<string, any[]>();
    let totalOiBtc = 0;
    let sumAtmIv = 0;
    let atmIvCount = 0;

    const now = Date.now();
    const nowUtc = new Date(now);
    const utcHours = nowUtc.getUTCHours();
    const utcMinutes = nowUtc.getUTCMinutes();

    for (const item of rawInstruments) {
      // Format: BTC-28AUG26-64000-C or BTC-28AUG26-64000-P
      const parts = item.instrument_name.split("-");
      if (parts.length < 4) continue;

      const expCode = parts[1];
      const strike = parseFloat(parts[2]);
      const type = parts[3].toUpperCase(); // "C" or "P"
      const oi = parseFloat(item.open_interest || 0);
      const markIv = parseFloat(item.mark_iv || 0.52);

      if (isNaN(strike) || oi <= 0) continue;

      totalOiBtc += oi;

      if (!expiriesMap.has(expCode)) {
        expiriesMap.set(expCode, []);
      }
      expiriesMap.get(expCode)!.push({
        strike,
        type,
        oi,
        markIv,
        underlyingPrice: item.underlying_price || spot,
      });

      if (!clustersMap.has(strike)) {
        clustersMap.set(strike, {
          strike,
          call_oi: 0,
          put_oi: 0,
          total_oi: 0,
          call_gex: 0,
          put_gex: 0,
          net_gex: 0,
          iv: markIv,
        });
      }

      const cluster = clustersMap.get(strike)!;
      if (type === "C") {
        cluster.call_oi += oi;
      } else {
        cluster.put_oi += oi;
      }
      cluster.total_oi += oi;

      if (Math.abs(strike - spot) / spot < 0.03 && markIv > 0.1) {
        sumAtmIv += markIv;
        atmIvCount++;
      }
    }

    const atmIv = atmIvCount > 0 ? sumAtmIv / atmIvCount / 100 : 0.52; // decimal e.g. 0.52

    // Black-Scholes gamma calculation per strike
    // T_years default ~ 3 days average
    const T = Math.max(0.5 / 365, 3 / 365);
    const sigma = Math.max(0.15, atmIv);

    let chainNetGex = 0;
    const allStrikes = Array.from(clustersMap.values()).sort((a, b) => a.strike - b.strike);

    for (const c of allStrikes) {
      const K = c.strike;
      const d1 = (Math.log(spot / K) + 0.5 * sigma * sigma * T) / (sigma * Math.sqrt(T));
      const gammaBs = Math.exp(-0.5 * d1 * d1) / (spot * sigma * Math.sqrt(2 * Math.PI * T));

      // Dealer convention: Call = +1 (dampens), Put = -1 (amplifies)
      const callGex = gammaBs * c.call_oi * 1.0 * (spot * spot) * 0.01 * 1.0;
      const putGex = gammaBs * c.put_oi * 1.0 * (spot * spot) * 0.01 * -1.0;
      c.call_gex = Number(callGex.toFixed(0));
      c.put_gex = Number(putGex.toFixed(0));
      c.net_gex = Number((callGex + putGex).toFixed(0));
      chainNetGex += c.net_gex;
    }

    // Zero Gamma (where cumulative GEX crosses 0)
    let cumGex = 0;
    let zeroGamma: number | null = null;
    for (let i = 0; i < allStrikes.length; i++) {
      const prevCum = cumGex;
      cumGex += allStrikes[i].net_gex;
      if (i > 0 && ((prevCum < 0 && cumGex >= 0) || (prevCum > 0 && cumGex <= 0))) {
        zeroGamma = allStrikes[i].strike;
        break;
      }
    }
    if (!zeroGamma) {
      zeroGamma = spot > 0 ? Number((spot * (chainNetGex < 0 ? 1.025 : 0.98)).toFixed(0)) : null;
    }

    // Walls
    let maxPutOi = 0;
    let putWall = spot * 0.95;
    let maxCallOi = 0;
    let callWall = spot * 1.05;

    for (const c of allStrikes) {
      if (c.strike < spot && c.put_oi > maxPutOi) {
        maxPutOi = c.put_oi;
        putWall = c.strike;
      }
      if (c.strike > spot && c.call_oi > maxCallOi) {
        maxCallOi = c.call_oi;
        callWall = c.strike;
      }
    }

    // Max Pain strike calculation
    let minPainTotal = Infinity;
    let maxPain = spot;
    const testStrikes = allStrikes.filter((c) => Math.abs(c.strike - spot) / spot < 0.25);
    for (const test of testStrikes) {
      let pain = 0;
      for (const other of testStrikes) {
        if (other.strike < test.strike) {
          // Calls ITM
          pain += (test.strike - other.strike) * other.call_oi;
        } else if (other.strike > test.strike) {
          // Puts ITM
          pain += (other.strike - test.strike) * other.put_oi;
        }
      }
      if (pain < minPainTotal) {
        minPainTotal = pain;
        maxPain = test.strike;
      }
    }

    // Realised Vol (24h default ~48-55%)
    const rv24h = 0.495;
    const ivRvRatio = Number((atmIv / rv24h).toFixed(2));

    // Expected moves
    const em0dte = spot * atmIv * Math.sqrt(1 / 365);
    const emWeekly = spot * atmIv * Math.sqrt(7 / 365);

    // Filter relevant strikes around spot (±15% for vertical axis)
    const topClusters = allStrikes
      .filter((c) => Math.abs(c.strike - spot) / spot <= 0.16)
      .slice(0, 45);

    // ATM Open Interest (within ±2.5% of underlying price as defined in Weiss et al. 2026, Section 2)
    let atmOiContracts = 0;
    for (const c of allStrikes) {
      if (Math.abs(c.strike - spot) / spot <= 0.025) {
        atmOiContracts += c.total_oi;
      }
    }
    const atmOiNotionalUsd = atmOiContracts * spot;
    // 90th percentile cutoff in Weiss et al. 2026 Table 2 / Footnote 7 corresponds to $109.2M in ATM open interest
    const isTopDecileOi = atmOiNotionalUsd >= 109_200_000 || atmOiContracts >= 1600;

    // Expiry Reversal Flag Check (Weiss et al. 2026, Finance Research Letters)
    // Pre-expiry window: 07:00–08:00 UTC; Settlement window: 07:30–08:00 UTC; Post-expiry reversal: 08:00–10:00 UTC
    const is07to08 = utcHours === 7 || (utcHours === 8 && utcMinutes <= 5);
    const isNegativeGamma = chainNetGex < 0;

    return {
      timestamp: Date.now(),
      spot_price: spot,
      source,
      total_oi_btc: Number(totalOiBtc.toFixed(1)),
      atm_oi_btc: Number(atmOiContracts.toFixed(1)),
      atm_oi_usd: Number(atmOiNotionalUsd.toFixed(0)),
      net_gex_usd: Number(chainNetGex.toFixed(0)),
      gex_regime: chainNetGex < 0 ? "AMPLIFYING" : "DAMPENING",
      zero_gamma: zeroGamma,
      put_wall: Number(putWall.toFixed(0)),
      call_wall: Number(callWall.toFixed(0)),
      max_pain: Number(maxPain.toFixed(0)),
      atm_iv: Number((atmIv * 100).toFixed(1)),
      realised_vol_24h: Number((rv24h * 100).toFixed(1)),
      iv_rv_ratio: ivRvRatio,
      expected_move_0dte: Number(em0dte.toFixed(0)),
      expected_move_weekly: Number(emWeekly.toFixed(0)),
      expiry_groups: {},
      top_clusters: topClusters,
      expiry_reversal_flag: {
        active: is07to08 && isNegativeGamma && isTopDecileOi,
        utc_hour: utcHours,
        utc_minute: utcMinutes,
        is_top_decile_oi: isTopDecileOi,
        is_negative_gamma: isNegativeGamma,
        atm_oi_usd: Number(atmOiNotionalUsd.toFixed(0)),
        headline: "08:00 UTC Deribit-Verfall Reversal (Weiss et al., 2026)",
        detail:
          "Empirisch belegt: 07:00–08:00 UTC Pre-Expiry Drift gefolgt von Return-Reversal bis 10:00 UTC, ausschließlich unter der Doppelbedingung: Top-Dezil ATM-OI (≥ $109.2M innerhalb ±2.5% Spot) UND negatives Netto-Gamma (Market-Maker Short Gamma).",
        caveats: [
          "Doppelbedingung zwingend: Ohne Top-Dezil ATM-OI + negatives Gamma ist der Reversal-Koeffizient statistisch insignifikant.",
          "Reversal misst die Umkehr des 07:00–08:00 UTC Moves bis 10:00 UTC (keine Max-Pain-Konvergenz).",
          "Studiensample: 2021–2023 (pre-ETF), bereinigtes R² = 0.0502. Reiner Strukturkontext.",
        ],
      },
    };
  }

  private generateBaseline(spot: number): OptionsSnapshot {
    const sigma = 0.523; // 52.3% ATM IV
    const strikes: OptionStrikeCluster[] = [];
    const step = 500;
    const center = Math.round(spot / step) * step;

    let chainNetGex = 0;
    let totalOi = 0;

    for (let s = center - 16 * step; s <= center + 16 * step; s += step) {
      const dist = (s - spot) / spot;
      const callWeight = Math.max(0.1, 1 + dist * 3.5);
      const putWeight = Math.max(0.1, 1 - dist * 3.5);
      const oiBase = 1200 * Math.exp(-0.5 * Math.pow(dist / 0.06, 2));

      const callOi = Number((oiBase * (dist > 0 ? 1.6 : 0.5) * callWeight + 80).toFixed(1));
      const putOi = Number((oiBase * (dist < 0 ? 1.7 : 0.4) * putWeight + 80).toFixed(1));
      totalOi += callOi + putOi;

      const T = 3 / 365;
      const d1 = (Math.log(spot / s) + 0.5 * sigma * sigma * T) / (sigma * Math.sqrt(T));
      const gammaBs = Math.exp(-0.5 * d1 * d1) / (spot * sigma * Math.sqrt(2 * Math.PI * T));

      const callGex = gammaBs * callOi * 1.0 * (spot * spot) * 0.01 * 1.0;
      const putGex = gammaBs * putOi * 1.0 * (spot * spot) * 0.01 * -1.0;
      const net = callGex + putGex;
      chainNetGex += net;

      strikes.push({
        strike: s,
        call_oi: callOi,
        put_oi: putOi,
        total_oi: Number((callOi + putOi).toFixed(1)),
        call_gex: Number(callGex.toFixed(0)),
        put_gex: Number(putGex.toFixed(0)),
        net_gex: Number(net.toFixed(0)),
        iv: Number((sigma * 100).toFixed(1)),
        is_atm: Math.abs(s - spot) < step * 0.6,
      });
    }

    const zeroGamma = center + (chainNetGex < 0 ? 1000 : -1000);
    const putWall = center - 2000;
    const callWall = center + 2500;
    const maxPain = center - 500;

    const rv24h = 0.482;
    const em0dte = spot * sigma * Math.sqrt(1 / 365);
    const emWeekly = spot * sigma * Math.sqrt(7 / 365);

    const nowUtc = new Date();
    const utcHours = nowUtc.getUTCHours();
    const utcMinutes = nowUtc.getUTCMinutes();
    const is07to08 = utcHours === 7 || (utcHours === 8 && utcMinutes <= 15);

    return {
      timestamp: Date.now(),
      spot_price: spot,
      source: "synthetic_model",
      total_oi_btc: Number(totalOi.toFixed(1)),
      net_gex_usd: Number(chainNetGex.toFixed(0)),
      gex_regime: chainNetGex < 0 ? "AMPLIFYING" : "DAMPENING",
      zero_gamma: zeroGamma,
      put_wall: putWall,
      call_wall: callWall,
      max_pain: maxPain,
      atm_iv: Number((sigma * 100).toFixed(1)),
      realised_vol_24h: Number((rv24h * 100).toFixed(1)),
      iv_rv_ratio: Number((sigma / rv24h).toFixed(2)),
      expected_move_0dte: Number(em0dte.toFixed(0)),
      expected_move_weekly: Number(emWeekly.toFixed(0)),
      expiry_groups: {},
      top_clusters: strikes,
      expiry_reversal_flag: {
        active: is07to08 && chainNetGex < 0,
        utc_hour: utcHours,
        utc_minute: utcMinutes,
        is_top_decile_oi: true,
        is_negative_gamma: chainNetGex < 0,
        headline: "08:00 UTC Deribit-Verfall Reversal-Konstellation (Weiss et al. 2026)",
        detail:
          "Pre-Expiry Drift 07:00–08:00 UTC mit anschließender Reversal-Tendenz bis 10:00 UTC dokumentiert bei hohem ATM OI und negativem Net Gamma.",
        caveats: [
          "Modellierte Standardkonvention: Dealer halten Gegenseite der Kundenpositionen.",
          "Studiensample endete Dezember 2023 (pre-ETF).",
          "Bereinigtes R² = 0.05 — Strukturkontext, niemals ein isoliertes Handelssignal.",
        ],
      },
    };
  }
}
