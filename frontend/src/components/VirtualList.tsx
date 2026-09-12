import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

/**
 * Fixed-height windowing, in about eighty lines.
 *
 * The old run list rendered twenty-four cards into a 3,346px column and got
 * slower with every run the owner ever launched. This renders only what is on
 * screen. It is deliberately not a dependency: rows here are one known height,
 * which is the case a virtualiser can solve exactly.
 *
 * When the viewport cannot be measured — jsdom, or the first paint before
 * layout — it falls back to a generous window rather than rendering nothing.
 * A list that is invisible in tests is a list nobody tests.
 */
const FALLBACK_VIEWPORT = 1400;

export function VirtualList<T>({
  items,
  rowHeight,
  renderRow,
  rowKey,
  overscan = 4,
  label,
  className,
}: {
  items: readonly T[];
  rowHeight: number;
  renderRow: (item: T, index: number) => ReactNode;
  rowKey: (item: T, index: number) => string;
  overscan?: number;
  label: string;
  className?: string;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const measure = (): void => setHeight(element.clientHeight);
    measure();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const viewport = height > 0 ? height : FALLBACK_VIEWPORT;
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visible = Math.ceil(viewport / rowHeight) + overscan * 2;
  const last = Math.min(items.length, first + visible);
  const slice = items.slice(first, last);

  return (
    <div
      className={className ? `virtual-list ${className}` : "virtual-list"}
      ref={viewportRef}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      role="list"
      aria-label={label}
    >
      <div
        className="virtual-list__sizer"
        style={{ height: items.length * rowHeight }}
        aria-hidden={items.length === 0 ? "true" : undefined}
      >
        {slice.map((item, index) => (
          <div
            className="virtual-list__row"
            role="listitem"
            key={rowKey(item, first + index)}
            style={{ top: (first + index) * rowHeight, height: rowHeight }}
          >
            {renderRow(item, first + index)}
          </div>
        ))}
      </div>
    </div>
  );
}
