import {
  Candle,
  aggregateCandles,
  activeSession,
  bracketFromEntry,
  detectCrtSetup,
  HtfBiasTracker,
  nyClock,
  ReBreakMachine,
  runCrtBacktest,
} from "../core/crtStrategy.js";

let PASS = 0;
let FAIL = 0;

function check(name: string, cond: boolean, detail = ""): void {
  if (cond) {
    PASS++;
    console.log(`  [PASS] ${name}`);
  } else {
    FAIL++;
    console.log(`  [FAIL] ${name} ${detail ? `(${detail})` : ""}`);
  }
}

function close(a: number, b: number, tol = 1e-6): boolean {
  return Math.abs(a - b) <= tol;
}

const M1 = 60_000;
const M15 = 15 * M1;
const H1 = 60 * M1;
const k = (time: number, open: number, high: number, low: number, close: number): Candle => ({ time, open, high, low, close });
const CFG = { rr: 1.5, tickSize: 0.1, entryBufferTicks: 1 };

console.log("\n=== 1. Bracket: SL aus festem 1:1.5 CRV ===");
const bl = bracketFromEntry("long", 100, 115, 1.5)!;
check("long: reward 15 -> risk 10 -> stop 90", close(bl.risk, 10) && close(bl.stop, 90) && bl.target === 115);
const bs = bracketFromEntry("short", 100, 85, 1.5)!;
check("short: reward 15 -> stop 110", close(bs.stop, 110));
check("kein positiver Reward -> null", bracketFromEntry("long", 115, 115, 1.5) === null);

console.log("\n=== 2. CRT 3-Kerzen Setup ===");
const c1 = k(0, 100, 110, 95, 105);
const longSetup = detectCrtSetup(c1, k(M15, 105, 108, 93, 99), M15, CFG);
check("long: Sweep unter C1-Low + Close zurueck in Range", longSetup?.direction === "long");
check("long: Trigger = C2-High + 1 Tick, Target = C1-High", close(longSetup!.trigger, 108.1) && longSetup!.target === 110);
check("long: C3 startet am Close von C2", longSetup!.c3Open === 2 * M15);
check("long ungueltig wenn C2 das C1-High verletzt", detectCrtSetup(c1, k(M15, 105, 111, 93, 99), M15, CFG) === null);
check("long ungueltig wenn Body unter C1-Low schliesst", detectCrtSetup(c1, k(M15, 105, 108, 93, 94), M15, CFG) === null);
const shortSetup = detectCrtSetup(c1, k(M15, 105, 112, 97, 101), M15, CFG);
check("short: Sweep ueber C1-High + Close zurueck", shortSetup?.direction === "short" && close(shortSetup!.trigger, 96.9) && shortSetup!.target === 95);
check("kein Sweep -> kein Setup", detectCrtSetup(c1, k(M15, 105, 109, 96, 100), M15, CFG) === null);
check("C2-High am Ziel -> kein Reward -> kein Setup", detectCrtSetup(c1, k(M15, 105, 110, 93, 99), M15, CFG) === null);

console.log("\n=== 3. Handelszeiten America/New_York (DST) ===");
check("Sommer: 13:30 UTC = 09:30 NY", nyClock(Date.UTC(2026, 6, 15, 13, 30)).minuteOfDay === 570);
check("Winter: 14:30 UTC = 09:30 NY", nyClock(Date.UTC(2026, 0, 15, 14, 30)).minuteOfDay === 570);
check("09:29 NY ausserhalb", activeSession(Date.UTC(2026, 6, 15, 13, 29)) === null);
check("11:59 NY in ny_am", activeSession(Date.UTC(2026, 6, 15, 15, 59))?.name === "ny_am");
check("12:00 NY ausserhalb (Ende exklusiv)", activeSession(Date.UTC(2026, 6, 15, 16, 0)) === null);
check("20:00 NY = Asia Session, NY-Vortag", activeSession(Date.UTC(2026, 6, 16, 0, 0))?.name === "asia" && nyClock(Date.UTC(2026, 6, 16, 0, 0)).dateKey === "2026-07-15");

console.log("\n=== 4. 1H Bias + Invalidation ===");
const swingLowSeries = [k(0, 96, 97, 95, 96), k(H1, 96, 96, 90, 92), k(2 * H1, 92, 95, 94, 94.5)];
const t1 = new HtfBiasTracker({ pivotLen: 1, invalidationCloses: 2 });
swingLowSeries.forEach((c) => t1.ingest(c));
check("Swing-Low 90 nach pivotLen bestaetigt", t1.snapshot().swingLow === 90 && t1.snapshot().bias === 0);
t1.ingest(k(3 * H1, 94, 95, 89, 92));
check("Sweep + Rejection des Swing-Lows -> nur Longs", t1.snapshot().bias === 1 && t1.allows("long") && !t1.allows("short"));
t1.ingest(k(4 * H1, 89.5, 91.5, 88, 89));
check("1 Body-Close unter Level -> Bias bleibt", t1.snapshot().bias === 1);
t1.ingest(k(5 * H1, 89, 92, 88.5, 91));
t1.ingest(k(6 * H1, 89.8, 91, 87, 88));
check("Zaehler resettet bei Close zurueck ueber Level", t1.snapshot().bias === 1);
t1.ingest(k(7 * H1, 88, 89.5, 86, 87));
check("2 volle Body-Closes unter Level -> neutral", t1.snapshot().bias === 0 && !t1.allows("long"));

const t2 = new HtfBiasTracker({ pivotLen: 1 });
[k(0, 104, 105, 103, 104), k(H1, 104, 110, 104, 108), k(2 * H1, 108, 106, 104, 105)].forEach((c) => t2.ingest(c));
t2.ingest(k(3 * H1, 105, 111, 104.5, 107));
check("Sweep + Rejection des Swing-Highs -> nur Shorts", t2.snapshot().bias === -1 && t2.allows("short"));
check("Long-Body ueber Level invalidiert Short-Bias nicht sofort", (() => {
  t2.ingest(k(4 * H1, 109, 112, 108, 111));
  return t2.snapshot().bias === -1;
})());

const t3 = new HtfBiasTracker({ pivotLen: 1 });
[k(0, 100, 105, 95, 100), k(H1, 100, 110, 90, 100), k(2 * H1, 100, 105, 95, 100)].forEach((c) => t3.ingest(c));
t3.ingest(k(3 * H1, 100, 111, 89, 100));
check("Outside Bar sweept beide Seiten -> neutral", t3.snapshot().bias === 0);

console.log("\n=== 5. Variante B: 1m Re-Break Zustandsautomat ===");
const rb = new ReBreakMachine(longSetup!, CFG);
check("unter C2-High: wartet", rb.ingest(k(0, 107, 107.9, 106.9, 107.5)) === null && rb.state === "wait_break");
rb.ingest(k(M1, 107.5, 108.3, 107.4, 108.2));
check("erster Ausbruch ueber C2-High", rb.state === "breaking" && rb.trigger() === null);
rb.ingest(k(2 * M1, 108.2, 108.6, 108.1, 108.5));
rb.ingest(k(3 * M1, 108.5, 108.4, 108.0, 108.1));
check("nach 1 Ruecksetzer-Kerze scharf auf Zwischenhoch + Tick", close(rb.trigger()!, 108.7));
const fill = rb.ingest(k(4 * M1, 108.1, 108.9, 108.0, 108.8));
check("Re-Break fuellt am Zwischenhoch", fill !== null && close(fill, 108.7) && rb.state === "filled");

const rb2 = new ReBreakMachine(longSetup!, CFG);
rb2.ingest(k(0, 107.5, 108.5, 107.4, 108.4));
for (let i = 1; i <= 4; i++) rb2.ingest(k(i * M1, 108.2, 108.3, 107.9, 108.0));
check("> 3 Ruecksetzer-Kerzen ohne Re-Break -> invalid", rb2.state === "invalid");

const rbs = new ReBreakMachine(shortSetup!, CFG);
rbs.ingest(k(0, 97.5, 97.8, 96.5, 96.7)); // Ausbruch unter C2-Low 97
rbs.ingest(k(M1, 96.7, 97.2, 96.6, 97.0)); // Ruecksetzer
const fillS = rbs.ingest(k(2 * M1, 97.0, 97.1, 96.3, 96.4));
check("short Re-Break gespiegelt: Fill an Zwischentief - Tick", fillS !== null && close(fillS, 96.4));

console.log("\n=== 6. Backtest Variante A ===");
// Block: Filler, C1, C2 (Long-Sweep), C3 (Breakout), Ergebnis-Kerze
const T0 = Date.UTC(2026, 6, 15, 13, 0); // 09:00 NY -> C3 um 09:45 NY
function block(t0: number, outcome: "win" | "loss" | "nofill"): Candle[] {
  const c3 = outcome === "nofill" ? k(t0 + 3 * M15, 99, 108.0, 98, 107) : k(t0 + 3 * M15, 107, 108.5, 106.9, 108.4);
  const out = outcome === "loss" ? k(t0 + 4 * M15, 108.4, 108.6, 106, 106.5) : k(t0 + 4 * M15, 108.4, 110.5, 108, 110.2);
  return [k(t0, 100, 101, 99, 100), k(t0 + M15, 100, 110, 95, 105), k(t0 + 2 * M15, 105, 108, 93, 99), c3, out];
}
const noBias = { useBias: false, timeframeMs: M15, ...CFG };

const win = runCrtBacktest(block(T0, "win"), noBias);
const tw = win.trades[0];
check("genau ein Trade", win.trades.length === 1 && win.stats.setups === 1);
check("Entry am C2-High + Tick waehrend C3", tw && close(tw.entry, 108.1) && tw.entryTime === T0 + 3 * M15);
check("TP am C1-High -> +1.5R", tw.outcome === "target" && close(tw.rMultiple!, 1.5));
check("Stats: Breakeven-Winrate 40%", close(win.stats.breakevenWinRate, 0.4) && win.stats.winRate === 1);

const loss = runCrtBacktest(block(T0, "loss"), noBias);
check("Stop -> -1R", loss.trades[0].outcome === "stop" && close(loss.trades[0].rMultiple!, -1));

const fee = runCrtBacktest(block(T0, "win"), { ...noBias, feeRate: 0.0004 });
check("Gebuehren senken R", fee.trades[0].rMultiple! < 1.5);

const nofill = runCrtBacktest(block(T0, "nofill"), noBias);
check("C3 ohne Ausbruch -> Setup verfaellt (kein Fill in C4)", nofill.trades.length === 0 && nofill.stats.setups === 1);

const outside = runCrtBacktest(block(Date.UTC(2026, 6, 15, 16, 0), "win"), noBias);
check("C3 ausserhalb der Session -> kein Trade", outside.trades.length === 0);

const twoBlocks = [...block(T0, "win"), ...block(T0 + 5 * M15, "win")];
check("2 Setups am Tag, Limit 2 -> 2 Trades", runCrtBacktest(twoBlocks, noBias).trades.length === 2);
check("Tageslimit 1 -> 1 Trade", runCrtBacktest(twoBlocks, { ...noBias, maxTradesPerDay: 1 }).trades.length === 1);

const bothBar = [...block(T0, "win").slice(0, 4), k(T0 + 4 * M15, 108.4, 110.5, 106, 107)];
check("Stop+Target in einer Kerze: stop_first konservativ", runCrtBacktest(bothBar, noBias).trades[0].outcome === "stop");
check("Stop+Target in einer Kerze: target_first optional", runCrtBacktest(bothBar, { ...noBias, sameBarPolicy: "target_first" }).trades[0].outcome === "target");

const htfLong = [k(T0 - 4 * H1, 96, 97, 95, 96), k(T0 - 3 * H1, 96, 96, 90, 92), k(T0 - 2 * H1, 92, 95, 94, 94.5), k(T0 - H1, 94, 95, 89, 92)];
const htfShort = [k(T0 - 4 * H1, 104, 105, 103, 104), k(T0 - 3 * H1, 104, 110, 104, 108), k(T0 - 2 * H1, 108, 106, 104, 105), k(T0 - H1, 105, 111, 104.5, 107)];
const biasCfg = { ...noBias, useBias: true, bias: { pivotLen: 1, invalidationCloses: 2 } };
check("1H Kaeufer-Territorium erlaubt Long", runCrtBacktest(block(T0, "win"), biasCfg, { htf: htfLong }).trades.length === 1);
check("1H Verkaeufer-Territorium blockt Long", runCrtBacktest(block(T0, "win"), biasCfg, { htf: htfShort }).trades.length === 0);

console.log("\n=== 7. Backtest Variante B (1m Re-Break) ===");
const ltf = block(T0, "win");
const c3t = T0 + 3 * M15;
const minutes = [
  k(c3t, 107, 107.5, 106.9, 107.4),
  k(c3t + M1, 107.4, 108.3, 107.3, 108.2), // erster Ausbruch
  k(c3t + 2 * M1, 108.2, 108.6, 108.1, 108.5), // Zwischenhoch 108.6
  k(c3t + 3 * M1, 108.5, 108.4, 108.0, 108.1), // Ruecksetzer
  k(c3t + 4 * M1, 108.4, 108.8, 108.3, 108.7), // Re-Break -> Fill 108.7
];
const rbRes = runCrtBacktest(ltf, { ...noBias, entryMode: "rebreak" }, { oneMinute: minutes });
const rbt = rbRes.trades[0];
check("Re-Break Entry am Zwischenhoch + Tick", rbt && close(rbt.entry, 108.7) && rbt.entryTime === c3t + 4 * M1);
check("SL aus CRV vom tatsaechlichen Entry", close(rbt.stop, 108.7 - (110 - 108.7) / 1.5));
check("Ziel C1-High erreicht", rbt.outcome === "target" && close(rbt.rMultiple!, 1.5));
const noRebreak = runCrtBacktest(ltf, { ...noBias, entryMode: "rebreak" }, { oneMinute: minutes.slice(0, 4) });
check("kein Re-Break in C3 -> kein Trade", noRebreak.trades.length === 0);
check("rebreak ohne 1m-Daten wirft", (() => {
  try {
    runCrtBacktest(ltf, { ...noBias, entryMode: "rebreak" });
    return false;
  } catch {
    return true;
  }
})());

console.log("\n=== 8. Helpers ===");
const agg = aggregateCandles([k(0, 1, 2, 0.5, 1.5), k(M15, 1.5, 3, 1, 2), k(2 * M15, 2, 2.5, 0.2, 1), k(3 * M15, 1, 1.2, 0.9, 1.1)], H1);
check("4x 15m -> 1x 1h OHLC", agg.length === 1 && agg[0].open === 1 && agg[0].high === 3 && agg[0].low === 0.2 && agg[0].close === 1.1);

console.log(`\n================================`);
console.log(`  ${PASS} tests passed, ${FAIL} tests failed`);
console.log(`================================\n`);

if (FAIL > 0) process.exit(1);
