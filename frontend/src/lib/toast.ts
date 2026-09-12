/**
 * Toasts live in a module-level store rather than a React context so that
 * non-React code — the run store, the SSE reconnect loop — can raise one
 * without a provider in scope. `<Toaster/>` renders whatever is here.
 */

import { useSyncExternalStore } from "react";

import type { Tone } from "./status";

export type Toast = {
  id: string;
  tone: Tone;
  title: string;
  message?: string;
  /** Milliseconds before auto-dismiss. 0 keeps it until dismissed. */
  duration: number;
};

export type ToastInput = {
  tone?: Tone;
  title: string;
  message?: string;
  duration?: number;
};

const DEFAULT_DURATION = 6000;

let toasts: Toast[] = [];
let counter = 0;
const listeners = new Set<() => void>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function getToasts(): Toast[] {
  return toasts;
}

export function subscribeToasts(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function pushToast(input: ToastInput): string {
  const id = `toast-${++counter}`;
  const toast: Toast = {
    id,
    tone: input.tone ?? "info",
    title: input.title,
    message: input.message,
    duration: input.duration ?? DEFAULT_DURATION,
  };
  toasts = [...toasts, toast];
  emit();

  if (toast.duration > 0) {
    timers.set(
      id,
      setTimeout(() => dismissToast(id), toast.duration),
    );
  }
  return id;
}

export function dismissToast(id: string): void {
  const timer = timers.get(id);
  if (timer !== undefined) {
    clearTimeout(timer);
    timers.delete(id);
  }
  if (!toasts.some((toast) => toast.id === id)) return;
  toasts = toasts.filter((toast) => toast.id !== id);
  emit();
}

/** Test helper — drops everything without animating anything out. */
export function clearToasts(): void {
  for (const timer of timers.values()) clearTimeout(timer);
  timers.clear();
  if (toasts.length === 0) return;
  toasts = [];
  emit();
}

export type ToastApi = {
  toasts: Toast[];
  push: (input: ToastInput) => string;
  dismiss: (id: string) => void;
};

export function useToasts(): ToastApi {
  const current = useSyncExternalStore(subscribeToasts, getToasts, getToasts);
  return { toasts: current, push: pushToast, dismiss: dismissToast };
}
