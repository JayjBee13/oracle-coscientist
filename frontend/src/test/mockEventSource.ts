/**
 * jsdom has no EventSource, so the reconnect and resume behaviour that the
 * whole live view depends on would otherwise be untestable. This mock records
 * every connection (including its URL, which is where `after_seq` shows up),
 * and lets a test push events, drop the connection and assert what the client
 * did about it.
 */

/* Not `implements EventSource`: the DOM interface narrows `addEventListener`
   in a way EventTarget cannot satisfy. `installMockEventSource` casts at the
   one place it matters. */
export class MockEventSource extends EventTarget {
  static readonly CONNECTING = 0 as const;
  static readonly OPEN = 1 as const;
  static readonly CLOSED = 2 as const;

  /** Every EventSource the code under test has constructed, in order. */
  static instances: MockEventSource[] = [];

  /** When true, a new connection opens by itself on the next microtask. */
  static autoOpen = true;

  readonly CONNECTING = 0 as const;
  readonly OPEN = 1 as const;
  readonly CLOSED = 2 as const;

  readonly url: string;
  readonly withCredentials = false;
  readyState: number = MockEventSource.CONNECTING;

  onopen: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    MockEventSource.instances.push(this);
    if (MockEventSource.autoOpen) {
      queueMicrotask(() => {
        if (this.readyState === MockEventSource.CONNECTING) this.open();
      });
    }
  }

  /* --- EventSource surface ------------------------------------------------ */

  close(): void {
    this.readyState = MockEventSource.CLOSED;
  }

  /* --- Test controls ------------------------------------------------------ */

  /** Completes the connection handshake. */
  open(): void {
    if (this.readyState === MockEventSource.CLOSED) return;
    this.readyState = MockEventSource.OPEN;
    const event = new Event("open");
    this.onopen?.(event);
    this.dispatchEvent(event);
  }

  /** Delivers a named SSE event with a raw data payload. */
  emit(type: string, data: string, lastEventId = ""): void {
    const event = new MessageEvent(type, { data, lastEventId });
    if (type === "message") this.onmessage?.(event);
    this.dispatchEvent(event);
  }

  /** Delivers a `run_event`, the only event name the backend streams. */
  emitRunEvent(event: {
    seq: number;
    run_id?: string;
    round?: number | null;
    type: string;
    payload?: Record<string, unknown>;
    ts?: string;
  }): void {
    const full = {
      run_id: "run-1",
      round: null,
      payload: {},
      ts: "2026-08-01T00:00:00Z",
      ...event,
    };
    this.emit("run_event", JSON.stringify(full), String(full.seq));
  }

  /** Drops the connection the way a network blip does. */
  fail(): void {
    this.readyState = MockEventSource.CONNECTING;
    const event = new Event("error");
    this.onerror?.(event);
    this.dispatchEvent(event);
  }

  static reset(): void {
    for (const instance of MockEventSource.instances) instance.close();
    MockEventSource.instances = [];
    MockEventSource.autoOpen = true;
  }

  static get last(): MockEventSource {
    const instance = MockEventSource.instances[MockEventSource.instances.length - 1];
    if (!instance) throw new Error("No EventSource has been opened");
    return instance;
  }

  /** Connections that have not been closed. */
  static get openCount(): number {
    return MockEventSource.instances.filter(
      (instance) => instance.readyState !== MockEventSource.CLOSED,
    ).length;
  }
}

export function installMockEventSource(): void {
  globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
}
