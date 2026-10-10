/**
 * Nitro Trades CRT (Candle Range Theory) — Mike Babayan Original-Regeln.
 *
 * Reine Funktionen + kleine Zustandsautomaten, damit dieselbe Logik im
 * Backtest und spaeter live (Aggregator-Kerzen) laufen kann.
 *
 * Regelwerk (siehe auch strategies/nitro_crt.pine):
 *   C1 = Range (Docht zu Docht), C2 = Sweep einer Seite + Close zurueck in
 *   der Range ohne die Gegenseite zu verletzen, C3 = Delivery: Einstieg nur
 *   waehrend der Laufzeit von C3, sonst verfaellt das Setup.
 *   TP = gegenueberliegendes C1-Extrem, SL rein rechnerisch aus festem CRV
 *   (Risk = Reward / 1.5), 1H-Bias, NY-Sessions, max. 1-2 Trades pro Tag.
 */

export type Direction = "long" | "short";

/** OHLC-Kerze; time = Eroeffnungszeit in unix ms. */
export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface CrtConfig {
  rr: number; // festes Reward:Risk (Babayan: 1.5)
  tickSize: number;
  entryBufferTicks: number; // Abstand der Stop-Order zum C2-Extrem
}

export const DEFAULT_CRT_CONFIG: CrtConfig = {
  rr: 1.5,
  tickSize: 0.1,
  entryBufferTicks: 1,
};

export interface CrtSetup {
  direction: Direction;
  c1High: number;
  c1Low: number;
  c2High: number;
  c2Low: number;
  /** Stop-Order-Level fuer Variante A (C2-Extrem +/- Buffer). */
  trigger: number;
  /** Unverrueckbares Ziel: gegenueberliegendes C1-Extrem. */
  target: number;
  /** Zeitpunkt, ab dem C3 laeuft (= Close von C2). */
  c3Open: number;
}

export interface Bracket {
  entry: number;
  stop: number;
  target: number;
  risk: number; // Preisdistanz Entry -> Stop
  reward: number; // Preisdistanz Entry -> Target
}

/**
 * SL wird nicht an Struktur gelegt, sondern aus dem festen CRV abgeleitet:
 * Risk = |TP - Entry| / rr. Gibt null zurueck, wenn das Ziel nicht vor dem
 * Entry liegt (kein positiver Reward).
 */
export function bracketFromEntry(direction: Direction, entry: number, target: number, rr: number): Bracket | null {
  if (!(rr > 0)) throw new Error(`rr must be > 0, got ${rr}`);
  const reward = direction === "long" ? target - entry : entry - target;
  if (!(reward > 0)) return null;
  const risk = reward / rr;
  const stop = direction === "long" ? entry - risk : entry + risk;
  return { entry, stop, target, risk, reward };
}

/**
 * Prueft C1/C2 auf ein gueltiges CRT-Setup. Wird am Close von C2 aufgerufen;
 * das Setup ist dann genau fuer die naechste Kerze (C3) scharf.
 *
 * Long:  C2-Low < C1-Low (Sweep), C2-Close > C1-Low (Body zurueck in Range),
 *        C2-High <= C1-High (Gegenseite nicht verletzt).
 * Short: spiegelbildlich.
 */
export function detectCrtSetup(
  c1: Candle,
  c2: Candle,
  timeframeMs: number,
  cfg: CrtConfig = DEFAULT_CRT_CONFIG
): CrtSetup | null {
  const buffer = cfg.tickSize * cfg.entryBufferTicks;
  const base = { c1High: c1.high, c1Low: c1.low, c2High: c2.high, c2Low: c2.low, c3Open: c2.time + timeframeMs };

  let setup: CrtSetup | null = null;
  if (c2.low < c1.low && c2.close > c1.low && c2.high <= c1.high) {
    setup = { ...base, direction: "long", trigger: c2.high + buffer, target: c1.high };
  } else if (c2.high > c1.high && c2.close < c1.high && c2.low >= c1.low) {
    setup = { ...base, direction: "short", trigger: c2.low - buffer, target: c1.low };
  }
  // Setup ohne positiven Reward (C2-Extrem liegt schon am Ziel) ist wertlos
  if (setup && !bracketFromEntry(setup.direction, setup.trigger, setup.target, cfg.rr)) return null;
  return setup;
}

// =============================================================================
// Handelszeiten (America/New_York, DST-korrekt via Intl)
// =============================================================================

export interface SessionWindow {
  name: string;
  start: number; // Minuten seit Mitternacht NY, inklusiv
  end: number; // exklusiv
}

export const NITRO_SESSIONS: SessionWindow[] = [
  { name: "ny_am", start: 9 * 60 + 30, end: 12 * 60 },
  { name: "asia", start: 20 * 60, end: 22 * 60 },
];

const NY_FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** NY-Kalendertag (YYYY-MM-DD) + Minute des Tages fuer einen unix-ms Zeitstempel. */
export function nyClock(ts: number): { dateKey: string; minuteOfDay: number } {
  const parts: Record<string, string> = {};
  for (const p of NY_FORMAT.formatToParts(new Date(ts))) parts[p.type] = p.value;
  return {
    dateKey: `${parts.year}-${parts.month}-${parts.day}`,
    minuteOfDay: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

export function activeSession(ts: number, windows: SessionWindow[] = NITRO_SESSIONS): SessionWindow | null {
  const m = nyClock(ts).minuteOfDay;
  return windows.find((w) => m >= w.start && m < w.end) ?? null;
}

// =============================================================================
// 1H Bias: Sweep + Rejection eines Swing-Extrems, Invalidation per Body-Close
// =============================================================================

export type Bias = -1 | 0 | 1; // 1 = Kaeufer-Territorium (nur Longs), -1 = nur Shorts

export interface HtfBiasConfig {
  pivotLen: number; // Kerzen links/rechts fuer ein bestaetigtes Swing-Extrem
  invalidationCloses: number; // volle Body-Closes jenseits des Levels (Babayan: 1-2)
}

export const DEFAULT_HTF_BIAS_CONFIG: HtfBiasConfig = { pivotLen: 3, invalidationCloses: 2 };

export class HtfBiasTracker {
  private cfg: HtfBiasConfig;
  private window: Candle[] = [];
  private swingHigh: number | null = null;
  private swingLow: number | null = null;
  private bias: Bias = 0;
  private level: number | null = null;
  private beyondCount = 0;

  constructor(cfg: Partial<HtfBiasConfig> = {}) {
    this.cfg = { ...DEFAULT_HTF_BIAS_CONFIG, ...cfg };
  }

  /** Nur ABGESCHLOSSENE HTF-Kerzen einspeisen (kein Lookahead). */
  ingest(c: Candle): void {
    // 1. Sweep gegen die bisher bekannten Swing-Level pruefen
    const sweptLow = this.swingLow !== null && c.low < this.swingLow && c.close > this.swingLow;
    const sweptHigh = this.swingHigh !== null && c.high > this.swingHigh && c.close < this.swingHigh;

    if (sweptLow && !sweptHigh) {
      this.setBias(1, this.swingLow);
    } else if (sweptHigh && !sweptLow) {
      this.setBias(-1, this.swingHigh);
    } else if (sweptLow && sweptHigh) {
      // Outside Bar sweept beide Seiten -> kein eindeutiges Territorium
      this.setBias(0, null);
    } else if (this.bias !== 0 && this.level !== null) {
      // 2. Invalidation: Body schliesst VOLL jenseits des Bias-Levels
      const bodyBeyond =
        this.bias === 1 ? Math.max(c.open, c.close) < this.level : Math.min(c.open, c.close) > this.level;
      this.beyondCount = bodyBeyond ? this.beyondCount + 1 : 0;
      if (this.beyondCount >= this.cfg.invalidationCloses) this.setBias(0, null);
    }

    // 3. Swing-Extreme aktualisieren (bestaetigt erst nach pivotLen Kerzen rechts)
    const L = this.cfg.pivotLen;
    this.window.push(c);
    if (this.window.length > 2 * L + 1) this.window.shift();
    if (this.window.length === 2 * L + 1) {
      const center = this.window[L];
      const left = this.window.slice(0, L);
      const right = this.window.slice(L + 1);
      if (left.every((k) => k.high < center.high) && right.every((k) => k.high <= center.high)) {
        this.swingHigh = center.high;
      }
      if (left.every((k) => k.low > center.low) && right.every((k) => k.low >= center.low)) {
        this.swingLow = center.low;
      }
    }
  }

  private setBias(bias: Bias, level: number | null): void {
    this.bias = bias;
    this.level = level;
    this.beyondCount = 0;
  }

  allows(direction: Direction): boolean {
    return direction === "long" ? this.bias === 1 : this.bias === -1;
  }

  snapshot(): { bias: Bias; level: number | null; swingHigh: number | null; swingLow: number | null } {
    return { bias: this.bias, level: this.level, swingHigh: this.swingHigh, swingLow: this.swingLow };
  }
}

// =============================================================================
// Variante B: 1-Minuten Re-Break Zustandsautomat (waehrend C3)
// =============================================================================

export interface ReBreakConfig {
  minPullbackBars: number; // Babayan: Ruecksetzer ueber 1-3 Kerzen
  maxPullbackBars: number;
}

export const DEFAULT_REBREAK_CONFIG: ReBreakConfig = { minPullbackBars: 1, maxPullbackBars: 3 };

export type ReBreakState = "wait_break" | "breaking" | "filled" | "invalid";

/** Spiegelt Short-Kerzen in den Long-Raum (Preis -> -Preis), damit die Logik nur einmal existiert. */
function orient(c: Candle, sign: number): Candle {
  return sign > 0
    ? c
    : { time: c.time, open: -c.open, high: -c.low, low: -c.high, close: -c.close };
}

export class ReBreakMachine {
  state: ReBreakState = "wait_break";
  private sign: number;
  private ref: number; // C2-Extrem im Long-Raum
  private buffer: number;
  private peak = -Infinity; // Zwischenhoch nach dem ersten Ausbruch
  private pullbackBars = 0;
  private cfg: ReBreakConfig;

  constructor(setup: CrtSetup, crt: CrtConfig = DEFAULT_CRT_CONFIG, cfg: Partial<ReBreakConfig> = {}) {
    this.sign = setup.direction === "long" ? 1 : -1;
    this.ref = this.sign > 0 ? setup.c2High : -setup.c2Low;
    this.buffer = crt.tickSize * crt.entryBufferTicks;
    this.cfg = { ...DEFAULT_REBREAK_CONFIG, ...cfg };
  }

  /** Aktuelles Stop-Order-Level fuer den Re-Break (null solange nicht scharf). */
  trigger(): number | null {
    if (this.state !== "breaking" || this.pullbackBars < this.cfg.minPullbackBars) return null;
    return this.sign * (this.peak + this.buffer);
  }

  /** Eine abgeschlossene 1m-Kerze verarbeiten; gibt den Fill-Preis zurueck, falls ausgeloest. */
  ingest(raw: Candle): number | null {
    if (this.state === "filled" || this.state === "invalid") return null;
    const m = orient(raw, this.sign);

    if (this.state === "wait_break") {
      // Status 1: erster Ausbruch ueber das C2-Niveau
      if (m.high > this.ref) {
        this.state = "breaking";
        this.peak = m.high;
        this.pullbackBars = 0;
      }
      return null;
    }

    // Status 3: Re-Break ueber das Zwischenhoch nach gueltigem Ruecksetzer
    const armed = this.pullbackBars >= this.cfg.minPullbackBars;
    const trig = this.peak + this.buffer;
    if (armed && m.high >= trig) {
      this.state = "filled";
      return this.sign * Math.max(trig, m.open);
    }
    if (m.high > this.peak) {
      // Ausbruch laeuft weiter (noch kein Ruecksetzer) -> Zwischenhoch nachziehen
      this.peak = m.high;
      this.pullbackBars = 0;
    } else {
      // Status 2: Ruecksetzer-Kerze
      this.pullbackBars++;
      if (this.pullbackBars > this.cfg.maxPullbackBars) this.state = "invalid";
    }
    return null;
  }
}

// =============================================================================
// Backtest (Variante A + B)
// =============================================================================

export type SameBarPolicy = "stop_first" | "target_first";

export interface CrtBacktestConfig extends CrtConfig {
  timeframeMs: number; // Setup-Zeiteinheit (15m / 30m / 1h)
  htfMs: number;
  useBias: boolean;
  bias: HtfBiasConfig;
  sessions: SessionWindow[];
  maxTradesPerDay: number;
  entryMode: "breakout" | "rebreak";
  rebreak: ReBreakConfig;
  /** OHLC verraet nicht, ob Stop oder Target zuerst kam — konservativ: Stop. */
  sameBarPolicy: SameBarPolicy;
  feeRate: number; // Gebuehr pro Seite als Anteil vom Preis (z.B. 0.0004)
}

export const DEFAULT_CRT_BACKTEST_CONFIG: CrtBacktestConfig = {
  ...DEFAULT_CRT_CONFIG,
  timeframeMs: 15 * 60_000,
  htfMs: 60 * 60_000,
  useBias: true,
  bias: DEFAULT_HTF_BIAS_CONFIG,
  sessions: NITRO_SESSIONS,
  maxTradesPerDay: 2,
  entryMode: "breakout",
  rebreak: DEFAULT_REBREAK_CONFIG,
  sameBarPolicy: "stop_first",
  feeRate: 0,
};

export interface CrtTrade {
  direction: Direction;
  setupTime: number; // Eroeffnung von C2
  entryTime: number;
  entry: number;
  stop: number;
  target: number;
  exitTime: number | null;
  exitPrice: number | null;
  outcome: "target" | "stop" | "open";
  rMultiple: number | null; // netto nach Gebuehren
}

export interface CrtBacktestStats {
  setups: number;
  trades: number;
  closed: number;
  wins: number;
  losses: number;
  winRate: number;
  totalR: number;
  expectancyR: number;
  breakevenWinRate: number; // brutto: 1 / (1 + rr)
  maxConsecutiveLosses: number;
}

export interface CrtBacktestResult {
  trades: CrtTrade[];
  stats: CrtBacktestStats;
}

/** Fasst Kerzen zu einer hoeheren Zeiteinheit zusammen (Bucket = floor(time / ms)). */
export function aggregateCandles(candles: Candle[], ms: number): Candle[] {
  const out: Candle[] = [];
  for (const c of candles) {
    const t = Math.floor(c.time / ms) * ms;
    const last = out[out.length - 1];
    if (last && last.time === t) {
      last.high = Math.max(last.high, c.high);
      last.low = Math.min(last.low, c.low);
      last.close = c.close;
    } else {
      out.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
    }
  }
  return out;
}

/** Prueft eine offene Position gegen eine Kerze; true = geschlossen. */
function manage(trade: CrtTrade, c: Candle, cfg: CrtBacktestConfig): boolean {
  const sign = trade.direction === "long" ? 1 : -1;
  const m = orient(c, sign);
  const hitTarget = m.high >= sign * trade.target;
  const hitStop = m.low <= sign * trade.stop;
  if (!hitTarget && !hitStop) return false;

  const targetWins = hitTarget && (!hitStop || cfg.sameBarPolicy === "target_first");
  trade.outcome = targetWins ? "target" : "stop";
  trade.exitPrice = targetWins ? trade.target : trade.stop;
  trade.exitTime = c.time;
  const risk = Math.abs(trade.entry - trade.stop);
  const fees = cfg.feeRate * (trade.entry + trade.exitPrice);
  trade.rMultiple = (sign * (trade.exitPrice - trade.entry) - fees) / risk;
  return true;
}

function openTrade(setup: CrtSetup, entry: number, time: number, rr: number): CrtTrade | null {
  const b = bracketFromEntry(setup.direction, entry, setup.target, rr);
  if (!b) return null;
  return {
    direction: setup.direction,
    setupTime: setup.c3Open,
    entryTime: time,
    entry,
    stop: b.stop,
    target: b.target,
    exitTime: null,
    exitPrice: null,
    outcome: "open",
    rMultiple: null,
  };
}

/**
 * Bar-fuer-Bar Simulation. Jede Kerze i ist potenziell C3 fuer (C1=i-2, C2=i-1).
 * - Bias nur aus HTF-Kerzen, die vor Eroeffnung von C3 geschlossen sind.
 * - Session-Filter auf die Eroeffnung von C3.
 * - Pyramiding 0: waehrend einer offenen Position werden keine Setups gehandelt.
 * - entryMode "rebreak" braucht 1m-Kerzen (options.oneMinute).
 */
export function runCrtBacktest(
  candles: Candle[],
  config: Partial<CrtBacktestConfig> = {},
  options: { htf?: Candle[]; oneMinute?: Candle[] } = {}
): CrtBacktestResult {
  const cfg: CrtBacktestConfig = { ...DEFAULT_CRT_BACKTEST_CONFIG, ...config };
  if (cfg.entryMode === "rebreak" && !options.oneMinute) {
    throw new Error("entryMode 'rebreak' requires options.oneMinute candles");
  }
  const htf = options.htf ?? aggregateCandles(candles, cfg.htfMs);
  const oneMinute = options.oneMinute ?? [];
  const tracker = new HtfBiasTracker(cfg.bias);

  const trades: CrtTrade[] = [];
  const tradesPerDay = new Map<string, number>();
  let htfIdx = 0;
  let minuteIdx = 0;
  let setups = 0;
  let position: CrtTrade | null = null;

  for (let i = 2; i < candles.length; i++) {
    const c3 = candles[i];
    while (htfIdx < htf.length && htf[htfIdx].time + cfg.htfMs <= c3.time) tracker.ingest(htf[htfIdx++]);

    if (position) {
      if (manage(position, c3, cfg)) position = null;
      continue;
    }

    const setup = detectCrtSetup(candles[i - 2], candles[i - 1], cfg.timeframeMs, cfg);
    if (!setup) continue;
    setups++;
    if (cfg.useBias && !tracker.allows(setup.direction)) continue;
    if (!activeSession(c3.time, cfg.sessions)) continue;
    const day = nyClock(c3.time).dateKey;
    if ((tradesPerDay.get(day) ?? 0) >= cfg.maxTradesPerDay) continue;

    let trade: CrtTrade | null = null;
    if (cfg.entryMode === "breakout") {
      // Variante A: Stop-Order am C2-Extrem, nur waehrend C3 gueltig
      const sign = setup.direction === "long" ? 1 : -1;
      const m = orient(c3, sign);
      if (m.high >= sign * setup.trigger) {
        trade = openTrade(setup, sign * Math.max(sign * setup.trigger, m.open), c3.time, cfg.rr);
        if (trade) manage(trade, c3, cfg);
      }
    } else {
      // Variante B: Re-Break auf 1m innerhalb des C3-Fensters
      const end = c3.time + cfg.timeframeMs;
      while (minuteIdx < oneMinute.length && oneMinute[minuteIdx].time < c3.time) minuteIdx++;
      const machine = new ReBreakMachine(setup, cfg, cfg.rebreak);
      for (let k = minuteIdx; k < oneMinute.length && oneMinute[k].time < end; k++) {
        if (!trade) {
          const fill = machine.ingest(oneMinute[k]);
          if (fill === null) continue;
          trade = openTrade(setup, fill, oneMinute[k].time, cfg.rr);
          if (!trade) break;
        }
        if (manage(trade, oneMinute[k], cfg)) break;
      }
    }

    if (!trade) continue;
    trades.push(trade);
    tradesPerDay.set(day, (tradesPerDay.get(day) ?? 0) + 1);
    if (trade.outcome === "open") position = trade;
  }

  return { trades, stats: summarize(trades, setups, cfg.rr) };
}

function summarize(trades: CrtTrade[], setups: number, rr: number): CrtBacktestStats {
  const closed = trades.filter((t) => t.outcome !== "open");
  const wins = closed.filter((t) => t.outcome === "target").length;
  const totalR = closed.reduce((s, t) => s + (t.rMultiple ?? 0), 0);
  let streak = 0;
  let maxStreak = 0;
  for (const t of closed) {
    streak = t.outcome === "stop" ? streak + 1 : 0;
    maxStreak = Math.max(maxStreak, streak);
  }
  return {
    setups,
    trades: trades.length,
    closed: closed.length,
    wins,
    losses: closed.length - wins,
    winRate: closed.length ? wins / closed.length : 0,
    totalR,
    expectancyR: closed.length ? totalR / closed.length : 0,
    breakevenWinRate: 1 / (1 + rr),
    maxConsecutiveLosses: maxStreak,
  };
}
