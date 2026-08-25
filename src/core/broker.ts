export const L2 = "L2";
export const TRADES = "TRADES";
export const AGGREGATED = "AGGREGATED";
export const SIGNALS = "SIGNALS";
export const PATTERNS = "PATTERNS";
export const OPTIONS = "OPTIONS";

type Listener<T = any> = (data: T) => void | Promise<void>;

export class Broker {
  private channels = new Map<string, Set<Listener>>();

  subscribe<T = any>(channel: string, listener: Listener<T>): () => void {
    if (!this.channels.has(channel)) {
      this.channels.set(channel, new Set());
    }
    this.channels.get(channel)!.add(listener);
    return () => {
      this.channels.get(channel)?.delete(listener);
    };
  }

  async publish<T = any>(channel: string, data: T): Promise<void> {
    const listeners = this.channels.get(channel);
    if (!listeners || listeners.size === 0) return;
    const promises: Promise<any>[] = [];
    for (const listener of Array.from(listeners)) {
      try {
        const res = listener(data);
        if (res instanceof Promise) {
          promises.push(res.catch(err => console.error(`Error in broker listener on ${channel}:`, err)));
        }
      } catch (err) {
        console.error(`Sync error in broker listener on ${channel}:`, err);
      }
    }
    if (promises.length > 0) {
      await Promise.all(promises);
    }
  }
}
