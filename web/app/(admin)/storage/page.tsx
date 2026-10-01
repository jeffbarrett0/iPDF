"use client";
import { useState } from "react";
import { ApiError, Health, api, fmtBytes, fmtDate, useLoad } from "@/lib/api";
import { Empty, ErrorBox, Spinner, Success } from "@/components/ui";

type Backup = { name: string; size: number; created: string };

export default function Storage() {
  const health = useLoad(() => api<Health>("/api/health"));
  const backups = useLoad(() => api<Backup[]>("/api/backups"));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  async function make() {
    setBusy(true); setErr(null); setOk(null);
    try { const b = await api<{ name: string; path: string }>("/api/backups", { method: "POST" }); setOk(`Backup written to ${b.path}`); await backups.reload(); }
    catch (e) { setErr(e as ApiError); }
    setBusy(false);
  }
  const h = health.data;
  return (
    <>
      <h1>Storage &amp; backup</h1>
      <ErrorBox error={health.error} onRetry={health.reload} />
      {health.loading && !h && <p><Spinner label="Loading…" /></p>}
      {h && (
        <div className="card">
          <h3>Where your data lives</h3>
          <table><tbody>
            <tr><th>Files &amp; database</th><td className="mono">{h.data_dir}</td></tr>
            <tr><th>Backups</th><td className="mono">{h.backup_dir}</td></tr>
            <tr><th>Default expiry</th><td>{h.file_ttl_days > 0 ? `${h.file_ttl_days} days after creation (change per file, or FILE_TTL_DAYS in .env)` : "never"}</td></tr>
            <tr><th>Database</th><td>{h.database ? "connected" : <span className="badge bad">unreachable</span>}</td></tr>
            <tr><th>LibreOffice</th><td>{h.tools.libreoffice ? "available" : "not found — Office conversion disabled"}</td></tr>
            <tr><th>OCR (Tesseract)</th><td>{h.tools.tesseract ? "available" : "not found — OCR disabled"}</td></tr>
            <tr><th>Email invitations</th><td>{h.smtp ? "SMTP configured" : "not configured — you share signing links yourself"}</td></tr>
          </tbody></table>
        </div>
      )}
      <div className="card">
        <h3>Export</h3>
        <p className="muted">A plain zip of every stored file plus <code>manifest.json</code> (hashes, parameters, lineage) and the event log. Opens without iPDF.</p>
        <a className="btn primary" href="/api/export">Export everything (zip)</a>
      </div>
      <div className="card">
        <div className="row spread"><h3 style={{ margin: 0 }}>Backups</h3><button className="primary" onClick={make} disabled={busy}>{busy ? "Backing up…" : "Create backup now"}</button></div>
        <p className="muted">Restorable archives (database rows + files). Restore with <code>.venv/bin/python -m app.backup restore &lt;archive&gt;</code> into an empty install — see the README.</p>
        {busy && <Spinner label="Writing backup…" />}
        <ErrorBox error={err} />
        {ok && <Success>{ok}</Success>}
        {backups.data?.length === 0 && <Empty title="No backups yet">Create one before upgrading or moving machines.</Empty>}
        {!!backups.data?.length && (
          <table><thead><tr><th>Archive</th><th>Size</th><th>Created</th></tr></thead><tbody>
            {backups.data.map((b) => <tr key={b.name}><td><a href={`/api/backups/${b.name}`}>{b.name}</a></td><td>{fmtBytes(b.size)}</td><td>{fmtDate(b.created)}</td></tr>)}
          </tbody></table>
        )}
      </div>
    </>
  );
}
