"use client";
import { useRef, useState } from "react";
import { Rect } from "@/lib/api";

type Props = {
  src: string;
  page: number;
  rects: (Rect & { label?: string; color?: string })[];
  onAdd: (r: Rect) => void;
  onRemove: (index: number) => void;
  color?: string;
  disabled?: boolean;
};

/** Click-and-drag on a page preview; emits rectangles as fractions of the page (top-left origin). */
export function RegionPicker({ src, page, rects, onAdd, onRemove, color = "#d64545", disabled }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  const pos = (e: React.PointerEvent) => {
    const b = ref.current!.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), y: Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)) };
  };
  const down = (e: React.PointerEvent) => {
    if (disabled || (e.target as HTMLElement).tagName === "BUTTON") return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const p = pos(e);
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
  };
  const move = (e: React.PointerEvent) => { if (drag) { const p = pos(e); setDrag({ ...drag, x1: p.x, y1: p.y }); } };
  const up = () => {
    if (!drag) return;
    const x = Math.min(drag.x0, drag.x1), y = Math.min(drag.y0, drag.y1);
    const w = Math.abs(drag.x1 - drag.x0), h = Math.abs(drag.y1 - drag.y0);
    setDrag(null);
    if (w > 0.01 && h > 0.005) onAdd({ page, x, y, w, h });
  };
  const style = (r: { x: number; y: number; w: number; h: number }, c: string, fill = "33") => ({
    left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%`, borderColor: c, background: c + fill,
  });
  return (
    <div className="picker" ref={ref} onPointerDown={down} onPointerMove={move} onPointerUp={up} style={{ cursor: disabled ? "default" : "crosshair" }}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt={`Page ${page}`} draggable={false} />
      {rects.map((r, i) => r.page === page && (
        <div key={i} className="box" style={style(r, r.color ?? color)}>
          <span style={{ background: r.color ?? color, padding: "0 3px" }}>{r.label}</span>
          {!disabled && <button aria-label="Remove region" onClick={() => onRemove(i)}>✕</button>}
        </div>
      ))}
      {drag && <div className="box" style={style({ x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1), w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0) }, color)} />}
    </div>
  );
}
