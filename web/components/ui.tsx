"use client";
import { ReactNode } from "react";
import { ApiError, Warning } from "@/lib/api";

export function LowStakesBanner() {
  return (
    <div className="lowstakes" role="note" aria-label="Low-stakes use warning">
      <div>
        <strong>⚠ Low-stakes use only.</strong> iPDF is a personal, local tool. Its signatures are simple electronic records —
        not qualified or legally verified signatures — and nobody&apos;s identity is checked. Do not rely on it for contracts
        with legal weight, regulated records, or anything where a dispute is costly. Keep your own backups.
      </div>
    </div>
  );
}

export function Spinner({ label = "Working…" }: { label?: string }) {
  return <span role="status"><span className="spinner" aria-hidden /> {label}</span>;
}

export function ErrorBox({ error, onRetry }: { error: ApiError | Error | null; onRetry?: () => void }) {
  if (!error) return null;
  const hint = error instanceof ApiError ? error.hint : null;
  return (
    <div className="alert error" role="alert">
      <strong>{error.message}</strong>
      {hint && <div className="hint">{hint}</div>}
      {onRetry && <div style={{ marginTop: 6 }}><button onClick={onRetry}>Try again</button></div>}
    </div>
  );
}

export function Success({ children }: { children: ReactNode }) {
  return <div className="alert success" role="status">{children}</div>;
}

export function Warnings({ items }: { items: Warning[] }) {
  if (!items.length) return null;
  return (
    <div>
      {items.map((w) => (
        <div key={w.code + w.message} className={`alert ${w.severity === "warning" ? "warning" : "info"}`}>
          {w.severity === "warning" ? "⚠ " : "ℹ "}{w.message}
        </div>
      ))}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><strong>{title}</strong><div>{children}</div></div>;
}
