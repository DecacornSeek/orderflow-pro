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

export interface MaterialChangeItem {
  id: string;
  timestamp_utc: string;
  type: "INFORMATIVE_OI_BUILD" | "MECHANICAL_SPOT_DRIFT" | "WALL_MIGRATION" | "IV_SHIFT" | "REGIME_TRANSITION";
  headline: string;
  detail: string;
}

export interface SessionAnchor {
  anchor_timestamp: number;
  anchor_time_utc: string;
  spot_price: number;
  zero_gamma: number | null;
  put_wall: number;
  call_wall: number;
  atm_iv: number;
  net_gex_usd: number;
}

export interface OptionsSnapshot {
  timestamp: number;
  spot_price: number;
  source: "deribit_live" | "deribit_cached" | "synthetic_model";
  total_oi_btc: number;
  atm_oi_btc?: number;
  atm_oi_usd?: number;
  net_gex_usd: number;
  gex_regime: "AMPLIFYING" | "DAMPENING"; // Hysteresis-filtered regime
  raw_gex_regime: "AMPLIFYING" | "DAMPENING";
  hysteresis_band_usd: number;
  zero_gamma: number | null;
  put_wall: number;
  call_wall: number;
  max_pain: number;
  atm_iv: number;
  realised_vol_24h: number;
  iv_rv_ratio: number;
  
  // Remaining-Time Decayed Expected Move (√(t_rest/365))
  t_rest_0dte_hours: number;
  expected_move_0dte: number; // Backward-compatible alias for 0DTE decayed
  expected_move_0dte_2sigma: number;
  expected_move_0dte_decayed: number;
  expected_move_0dte_decayed_2sigma: number;
  expected_move_0dte_session: number; // Full 24h reference
  expected_move_weekly: number;
  expected_move_weekly_2sigma: number;
  skew_25d: number; // 25-Delta Skew in Vol Points (IV_Call - IV_Put)

  // Session Open Anchor & Deltas
  session_anchor: SessionAnchor;
  deltas_from_anchor: {
    delta_spot: number;
    delta_zero_gamma: number | null;
    delta_put_wall: number;
    delta_call_wall: number;
    delta_atm_iv: number;
    delta_net_gex_usd: number;
    drift_nature: "INFORMATIVE_OI_BUILD" | "MECHANICAL_SPOT_DERIVATIVE" | "UNCHANGED";
  };

  material_changes: MaterialChangeItem[];

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
  
  // Session Anchor State
  private sessionAnchor: SessionAnchor | null = null;
  private currentFilteredRegime: "AMPLIFYING" | "DAMPENING" = "DAMPENING";
  private materialChangesLog: MaterialChangeItem[] = [];

  constructor(broker: Broker) {
    this.broker = broker;
  }

  setSpotPrice(spot: number): void {
    this.spotPrice = spot;
  }

  resetSessionAnchor(): void {
    this.sessionAnchor = null;
    if (this.lastSnapshot) {
      this.initAnchor(this.lastSnapshot);
    }
  }

  private initAnchor(snap: { spot_price: number; zero_gamma: number | null; put_wall: number; call_wall: number; atm_iv: number; net_gex_usd: number }): SessionAnchor {
    const now = new Date();
    const anchor: SessionAnchor = {
      anchor_timestamp: Date.now(),
      anchor_time_utc: `${String(now.getUTCHours()).padStart(2, '0')}:${String(now.getUTCMinutes()).padStart(2, '0')} UTC`,
      spot_price: snap.spot_price,
      zero_gamma: snap.zero_gamma,
      put_wall: snap.put_wall,
      call_wall: snap.call_wall,
      atm_iv: snap.atm_iv,
      net_gex_usd: snap.net_gex_usd,
    };
    this.sessionAnchor = anchor;
    this.addMaterialChange(
      "REGIME_TRANSITION",
      "Session-Open Anchor Captured",
      `Referenzpunkte fixiert bei Spot $${snap.spot_price.toLocaleString('en-US')}, Zero-Γ $${snap.zero_gamma?.toLocaleString('en-US') ?? '—'}, IV ${snap.atm_iv}%.`
    );
    return anchor;
  }

  private addMaterialChange(type: MaterialChangeItem["type"], headline: string, detail: string): void {
    const now = new Date();
    const utcStr = `${String(now.getUTCHours()).padStart(2, '0')}:${String(now.getUTCMinutes()).padStart(2, '0')}:${String(now.getUTCSeconds()).padStart(2, '0')} UTC`;
    this.materialChangesLog.unshift({
      id: `mc_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
      timestamp_utc: utcStr,
      type,
      headline,
      detail,
    });
    if (this.materialChangesLog.length > 20) {
      this.materialChangesLog.pop();
    }
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

    // 08:00 UTC Expiry Remaining-Time Decay (√(t_rest/365))
    // Compute exact hours until next 08:00 UTC settlement (with 15 min / 0.25h floor)
    const target08Utc = new Date(nowUtc);
    target08Utc.setUTCHours(8, 0, 0, 0);
    if (nowUtc.getTime() >= target08Utc.getTime()) {
      target08Utc.setUTCDate(target08Utc.getUTCDate() + 1);
    }
    const msTo08 = target08Utc.getTime() - nowUtc.getTime();
    const hoursRest08 = Math.max(0.25, msTo08 / (1000 * 60 * 60));
    const tRest08Years = hoursRest08 / (24 * 365);

    // Decayed 0DTE Expected Move
    const em0dteDecayed = spot * atmIv * Math.sqrt(tRest08Years);
    const em0dteDecayed2s = 2.0 * em0dteDecayed;
    const em0dteSessionFull = spot * atmIv * Math.sqrt(1 / 365);

    // Weekly Expected Move
    const emWeekly = spot * atmIv * Math.sqrt(7 / 365);
    const emWeekly2s = 2.0 * emWeekly;

    // 25-Delta Skew estimation:
    // Approx 25d strikes at spot * exp(±0.6745 * IV * sqrt(T))
    const tSkew = 7 / 365;
    const kCall25 = spot * Math.exp(0.6745 * atmIv * Math.sqrt(tSkew));
    const kPut25 = spot * Math.exp(-0.6745 * atmIv * Math.sqrt(tSkew));
    let callIv25 = atmIv;
    let putIv25 = atmIv;
    let minCallDist = Infinity;
    let minPutDist = Infinity;
    for (const c of allStrikes) {
      if (c.call_oi > 0) {
        const d = Math.abs(c.strike - kCall25);
        if (d < minCallDist) {
          minCallDist = d;
          callIv25 = (c.iv || (atmIv * 100)) / 100;
        }
      }
      if (c.put_oi > 0) {
        const d = Math.abs(c.strike - kPut25);
        if (d < minPutDist) {
          minPutDist = d;
          putIv25 = (c.iv || (atmIv * 100)) / 100;
        }
      }
    }
    const skew25d = Number((callIv25 - putIv25).toFixed(4));

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

    // Hysteresis Band: ±0.25σ of remaining session move or $150 minimum
    const hysteresisBandUsd = Math.max(150, Math.round(0.25 * em0dteDecayed));
    const rawGexRegime: "AMPLIFYING" | "DAMPENING" = chainNetGex < 0 ? "AMPLIFYING" : "DAMPENING";
    
    // Apply Hysteresis Filter to avoid flickering around Zero-Γ
    if (zeroGamma !== null) {
      if (this.currentFilteredRegime === "DAMPENING" && spot < (zeroGamma - hysteresisBandUsd)) {
        this.currentFilteredRegime = "AMPLIFYING";
        this.addMaterialChange(
          "REGIME_TRANSITION",
          "Regime Shift: Long Γ → Short Γ (AMPLIFYING)",
          `Spot ($${spot.toLocaleString('en-US')}) durchbrach Zero-Γ ($${zeroGamma.toLocaleString('en-US')}) unterhalb der Hysterese-Zone (−$${hysteresisBandUsd}). Hedging wechselt zu Accelerant.`
        );
      } else if (this.currentFilteredRegime === "AMPLIFYING" && spot > (zeroGamma + hysteresisBandUsd)) {
        this.currentFilteredRegime = "DAMPENING";
        this.addMaterialChange(
          "REGIME_TRANSITION",
          "Regime Shift: Short Γ → Long Γ (DAMPENING)",
          `Spot ($${spot.toLocaleString('en-US')}) überwand Zero-Γ ($${zeroGamma.toLocaleString('en-US')}) oberhalb der Hysterese-Zone (+$${hysteresisBandUsd}). Hedging wirkt dämpfend / pinning.`
        );
      }
    } else {
      this.currentFilteredRegime = rawGexRegime;
    }

    // Session Anchor & Delta comparison
    if (!this.sessionAnchor) {
      this.initAnchor({
        spot_price: spot,
        zero_gamma: zeroGamma,
        put_wall: Number(putWall.toFixed(0)),
        call_wall: Number(callWall.toFixed(0)),
        atm_iv: Number((atmIv * 100).toFixed(1)),
        net_gex_usd: Number(chainNetGex.toFixed(0)),
      });
    }

    const anchor = this.sessionAnchor!;
    const deltaSpot = Math.round(spot - anchor.spot_price);
    const deltaZeroGamma = zeroGamma !== null && anchor.zero_gamma !== null ? Math.round(zeroGamma - anchor.zero_gamma) : null;
    const deltaPutWall = Math.round(putWall - anchor.put_wall);
    const deltaCallWall = Math.round(callWall - anchor.call_wall);
    const deltaAtmIv = Number(((atmIv * 100) - anchor.atm_iv).toFixed(1));
    const deltaNetGex = Math.round(chainNetGex - anchor.net_gex_usd);

    // Classify drift nature (Mechanical vs. Informative OI accumulation)
    let driftNature: "INFORMATIVE_OI_BUILD" | "MECHANICAL_SPOT_DERIVATIVE" | "UNCHANGED" = "UNCHANGED";
    if (deltaZeroGamma !== null && Math.abs(deltaZeroGamma) >= 250) {
      if (Math.abs(deltaSpot) <= 150) {
        driftNature = "INFORMATIVE_OI_BUILD";
      } else {
        driftNature = "MECHANICAL_SPOT_DERIVATIVE";
      }
    } else if (Math.abs(deltaPutWall) >= 500 || Math.abs(deltaCallWall) >= 500) {
      driftNature = "INFORMATIVE_OI_BUILD";
    }

    return {
      timestamp: Date.now(),
      spot_price: spot,
      source,
      total_oi_btc: Number(totalOiBtc.toFixed(1)),
      atm_oi_btc: Number(atmOiContracts.toFixed(1)),
      atm_oi_usd: Number(atmOiNotionalUsd.toFixed(0)),
      net_gex_usd: Number(chainNetGex.toFixed(0)),
      gex_regime: this.currentFilteredRegime,
      raw_gex_regime: rawGexRegime,
      hysteresis_band_usd: hysteresisBandUsd,
      zero_gamma: zeroGamma,
      put_wall: Number(putWall.toFixed(0)),
      call_wall: Number(callWall.toFixed(0)),
      max_pain: Number(maxPain.toFixed(0)),
      atm_iv: Number((atmIv * 100).toFixed(1)),
      realised_vol_24h: Number((rv24h * 100).toFixed(1)),
      iv_rv_ratio: ivRvRatio,
      t_rest_0dte_hours: Number(hoursRest08.toFixed(2)),
      expected_move_0dte: Number(em0dteDecayed.toFixed(0)),
      expected_move_0dte_2sigma: Number(em0dteDecayed2s.toFixed(0)),
      expected_move_0dte_decayed: Number(em0dteDecayed.toFixed(0)),
      expected_move_0dte_decayed_2sigma: Number(em0dteDecayed2s.toFixed(0)),
      expected_move_0dte_session: Number(em0dteSessionFull.toFixed(0)),
      expected_move_weekly: Number(emWeekly.toFixed(0)),
      expected_move_weekly_2sigma: Number(emWeekly2s.toFixed(0)),
      skew_25d: skew25d,
      session_anchor: anchor,
      deltas_from_anchor: {
        delta_spot: deltaSpot,
        delta_zero_gamma: deltaZeroGamma,
        delta_put_wall: deltaPutWall,
        delta_call_wall: deltaCallWall,
        delta_atm_iv: deltaAtmIv,
        delta_net_gex_usd: deltaNetGex,
        drift_nature: driftNature,
      },
      material_changes: [...this.materialChangesLog],
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
    const nowUtc = new Date();
    const utcHours = nowUtc.getUTCHours();
    const utcMinutes = nowUtc.getUTCMinutes();
    const is07to08 = utcHours === 7 || (utcHours === 8 && utcMinutes <= 15);

    const target08Utc = new Date(nowUtc);
    target08Utc.setUTCHours(8, 0, 0, 0);
    if (nowUtc.getTime() >= target08Utc.getTime()) {
      target08Utc.setUTCDate(target08Utc.getUTCDate() + 1);
    }
    const msTo08 = target08Utc.getTime() - nowUtc.getTime();
    const hoursRest08 = Math.max(0.25, msTo08 / (1000 * 60 * 60));
    const tRest08Years = hoursRest08 / (24 * 365);

    const em0dteDecayed = spot * sigma * Math.sqrt(tRest08Years);
    const em0dteDecayed2s = 2.0 * em0dteDecayed;
    const em0dteSessionFull = spot * sigma * Math.sqrt(1 / 365);
    const emWeekly = spot * sigma * Math.sqrt(7 / 365);
    const emWeekly2s = 2.0 * emWeekly;

    const hysteresisBandUsd = Math.max(150, Math.round(0.25 * em0dteDecayed));
    const rawGexRegime: "AMPLIFYING" | "DAMPENING" = chainNetGex < 0 ? "AMPLIFYING" : "DAMPENING";
    
    if (zeroGamma !== null) {
      if (this.currentFilteredRegime === "DAMPENING" && spot < (zeroGamma - hysteresisBandUsd)) {
        this.currentFilteredRegime = "AMPLIFYING";
      } else if (this.currentFilteredRegime === "AMPLIFYING" && spot > (zeroGamma + hysteresisBandUsd)) {
        this.currentFilteredRegime = "DAMPENING";
      }
    } else {
      this.currentFilteredRegime = rawGexRegime;
    }

    if (!this.sessionAnchor) {
      this.initAnchor({
        spot_price: spot,
        zero_gamma: zeroGamma,
        put_wall: putWall,
        call_wall: callWall,
        atm_iv: Number((sigma * 100).toFixed(1)),
        net_gex_usd: Number(chainNetGex.toFixed(0)),
      });
    }

    const anchor = this.sessionAnchor!;
    const deltaSpot = Math.round(spot - anchor.spot_price);
    const deltaZeroGamma = zeroGamma !== null && anchor.zero_gamma !== null ? Math.round(zeroGamma - anchor.zero_gamma) : null;
    const deltaPutWall = Math.round(putWall - anchor.put_wall);
    const deltaCallWall = Math.round(callWall - anchor.call_wall);
    const deltaAtmIv = Number(((sigma * 100) - anchor.atm_iv).toFixed(1));
    const deltaNetGex = Math.round(chainNetGex - anchor.net_gex_usd);

    return {
      timestamp: Date.now(),
      spot_price: spot,
      source: "synthetic_model",
      total_oi_btc: Number(totalOi.toFixed(1)),
      net_gex_usd: Number(chainNetGex.toFixed(0)),
      gex_regime: this.currentFilteredRegime,
      raw_gex_regime: rawGexRegime,
      hysteresis_band_usd: hysteresisBandUsd,
      zero_gamma: zeroGamma,
      put_wall: putWall,
      call_wall: callWall,
      max_pain: maxPain,
      atm_iv: Number((sigma * 100).toFixed(1)),
      realised_vol_24h: Number((rv24h * 100).toFixed(1)),
      iv_rv_ratio: Number((sigma / rv24h).toFixed(2)),
      t_rest_0dte_hours: Number(hoursRest08.toFixed(2)),
      expected_move_0dte: Number(em0dteDecayed.toFixed(0)),
      expected_move_0dte_2sigma: Number(em0dteDecayed2s.toFixed(0)),
      expected_move_0dte_decayed: Number(em0dteDecayed.toFixed(0)),
      expected_move_0dte_decayed_2sigma: Number(em0dteDecayed2s.toFixed(0)),
      expected_move_0dte_session: Number(em0dteSessionFull.toFixed(0)),
      expected_move_weekly: Number(emWeekly.toFixed(0)),
      expected_move_weekly_2sigma: Number(emWeekly2s.toFixed(0)),
      skew_25d: 0.012,
      session_anchor: anchor,
      deltas_from_anchor: {
        delta_spot: deltaSpot,
        delta_zero_gamma: deltaZeroGamma,
        delta_put_wall: deltaPutWall,
        delta_call_wall: deltaCallWall,
        delta_atm_iv: deltaAtmIv,
        delta_net_gex_usd: deltaNetGex,
        drift_nature: "UNCHANGED",
      },
      material_changes: [...this.materialChangesLog],
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
