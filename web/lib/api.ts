"use client";
import { useCallback, useEffect, useState } from "react";

export type Warning = { code: string; severity: "info" | "warning"; message: string };
export type FileRec = {
  id: string; kind: "original" | "output"; root_id: string; version: number; display_name: string; mime: string;
  sha256: string; size_bytes: number; page_count: number | null; operation: string | null;
  params: Record<string, unknown>; source_ids: string[]; analysis: Record<string, unknown>; warnings: Warning[];
  created_at: string; expires_at: string | null; deleted_at: string | null; deleted_reason: string | null;
};
export type Rect = { page: number; x: number; y: number; w: number; h: number };
export type Health = {
  ok: boolean; database: boolean; tools: { libreoffice: boolean; tesseract: boolean; ghostscript: boolean };
  smtp: boolean; data_dir: string; backup_dir: string; file_ttl_days: number; max_upload_mb: number;
};

export class ApiError extends Error {
  constructor(public code: string, message: string, public hint: string | null = null, public status = 0) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const opts: RequestInit = { ...init };
  if (init?.json !== undefined) {
    opts.method = opts.method ?? "POST";
    opts.headers = { "Content-Type": "application/json", ...(init.headers ?? {}) };
    opts.body = JSON.stringify(init.json);
  }
  let res: Response;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError("network", "Cannot reach the local iPDF API.", "Is ./run.sh still running? Start it again and retry.");
  }
  if (!res.ok) {
    let body: { error?: { code: string; message: string; hint?: string | null } } = {};
    try { body = await res.json(); } catch { /* non-JSON error from the proxy */ }
    if (body.error) throw new ApiError(body.error.code, body.error.message, body.error.hint ?? null, res.status);
    throw new ApiError(
      res.status >= 500 ? "api_down" : "http_error",
      res.status >= 500 ? "The local API did not respond properly." : `Request failed (${res.status}).`,
      res.status >= 500 ? "Check that the API process and PostgreSQL are running (see the terminal running ./run.sh)." : null,
      res.status,
    );
  }
  if (res.headers.get("content-type")?.includes("application/json")) return res.json();
  return undefined as T;
}

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const reload = useCallback(async () => {
    setLoading(true);
    try { setData(await fn()); setError(null); }
    catch (e) { setError(e instanceof ApiError ? e : new ApiError("unknown", String(e))); }
    finally { setLoading(false); }
  }, deps);
  useEffect(() => { void reload(); }, [reload]);
  return { data, error, loading, reload };
}

export const fmtBytes = (n: number) =>
  n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
export const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
export function expiryText(s: string | null) {
  if (!s) return "never expires";
  const days = Math.ceil((new Date(s).getTime() - Date.now()) / 86400000);
  return days <= 0 ? "expires today" : `expires in ${days} day${days === 1 ? "" : "s"}`;
}
export const pagePreview = (id: string, page: number, width = 320) => `/api/files/${id}/pages/${page}/preview?width=${width}`;
