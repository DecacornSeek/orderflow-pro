export const BUCKET = 25;

export interface Kline {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export class History {
  private maxSeconds: number;
  private snapshots: any[] = [];
  private vap: Map<number, number> = new Map();
  private klines: Kline[] = [];

  constructor(maxSeconds = 3600) {
    this.maxSeconds = maxSeconds;
  }

  setKlines(klines: Kline[]): void {
    this.klines = klines;
    // Pre-populate VAP from recent klines if VAP is currently empty
    if (this.vap.size === 0 && klines.length > 0) {
      for (const k of klines) {
        const avgPrice = (k.open + k.high + k.low + k.close) / 4;
        const bucket = Math.floor(avgPrice / BUCKET) * BUCKET;
        this.vap.set(bucket, (this.vap.get(bucket) || 0) + k.volume / 4);
      }
    }
  }

  getKlines(): Kline[] {
    return this.klines;
  }

  addSnapshot(snapshot: any): void {
    this.snapshots.push(snapshot);
    if (this.snapshots.length > this.maxSeconds) {
      this.snapshots.shift();
    }
  }

  getSnapshots(lastN = 60): any[] {
    return this.snapshots.slice(-lastN);
  }

  addTrade(price: number, size: number): void {
    const bucket = Math.floor(price / BUCKET) * BUCKET;
    this.vap.set(bucket, (this.vap.get(bucket) || 0) + size);
  }

  getDepthFrames(lastN = 60): any[] {
    return this.snapshots.slice(-lastN);
  }

  getVap(): Record<number, number> {
    const res: Record<number, number> = {};
    for (const [k, v] of this.vap.entries()) {
      res[k] = Number(v.toFixed(4));
    }
    return res;
  }
}
