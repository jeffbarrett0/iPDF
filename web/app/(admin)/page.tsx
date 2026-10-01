"use client";
import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { ApiError, FileRec, Health, api, expiryText, fmtBytes, fmtDate, useLoad } from "@/lib/api";
import { Empty, ErrorBox, Spinner, Success } from "@/components/ui";

const ACCEPT = ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.txt";

export default function Library() {
  const files = useLoad(() => api<FileRec[]>("/api/files"));
  const health = useLoad(() => api<Health>("/api/health"));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const input = useRef<HTMLInputElement>(null);

  async function upload(list: FileList | File[]) {
    setError(null); setNotice(null);
    const done: string[] = [];
    for (const f of Array.from(list)) {
      setBusy(`Uploading ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await api<{ file: FileRec; outputs: FileRec[]; convert_error: { message: string; hint?: string } | null }>("/api/files", { method: "POST", body: fd });
        done.push(r.outputs.length ? `${f.name} → converted to PDF` : f.name);
        if (r.convert_error) setError(new ApiError("convert", `${f.name} was saved, but conversion failed: ${r.convert_error.message}`, r.convert_error.hint ?? null));
      } catch (e) {
        setError(e instanceof ApiError ? new ApiError(e.code, `${f.name}: ${e.message}`, e.hint, e.status) : new ApiError("unknown", String(e)));
      }
    }
    setBusy(null);
    if (done.length) setNotice(`Saved ${done.join(", ")}. Originals are stored read-only.`);
    await files.reload();
  }

  async function retryConvert(f: FileRec) {
    setError(null); setBusy(`Converting ${f.display_name}…`);
    try { await api("/api/ops/convert", { json: { file_ids: [f.id], params: {} } }); setNotice(`Converted ${f.display_name}.`); }
    catch (e) { setError(e as ApiError); }
    setBusy(null); await files.reload();
  }

  async function merge() {
    setError(null); setBusy("Merging…");
    try {
      const r = await api<{ outputs: FileRec[] }>("/api/ops/merge", { json: { file_ids: selected, params: {} } });
      setNotice(`Created ${r.outputs[0].display_name}.`); setSelected([]);
    } catch (e) { setError(e as ApiError); }
    setBusy(null); await files.reload();
  }

  const groups = useMemo(() => {
    const m = new Map<string, FileRec[]>();
    for (const f of files.data ?? []) m.set(f.root_id, [...(m.get(f.root_id) ?? []), f]);
    return [...m.values()].map((g) => g.sort((a, b) => a.version - b.version));
  }, [files.data]);

  return (
    <>
      <h1>Library</h1>
      {health.data && !health.data.database && (
        <div className="alert error" role="alert"><strong>The database is not reachable.</strong> Run <code>./run.sh</code> (it starts PostgreSQL for you) or check <code>DATABASE_URL</code> in <code>.env</code>.</div>
      )}
      {health.error && <ErrorBox error={health.error} onRetry={health.reload} />}
      {health.data && !health.data.tools.libreoffice && <div className="alert info">LibreOffice was not found, so Word/Excel/PowerPoint files can be stored but not converted yet.</div>}

      <div className={`drop ${over ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); void upload(e.dataTransfer.files); }}>
        <p style={{ margin: "0 0 8px" }}><strong>Drop files here</strong> — PDF, Word, Excel, PowerPoint, OpenDocument, RTF, TXT</p>
        <p className="muted" style={{ margin: "0 0 10px" }}>Files stay on this computer{health.data ? ` (up to ${health.data.max_upload_mb} MB each)` : ""}. Nothing is uploaded to a vendor.</p>
        <input ref={input} type="file" accept={ACCEPT} multiple hidden onChange={(e) => e.target.files && void upload(e.target.files)} />
        <button className="primary" onClick={() => input.current?.click()} disabled={!!busy}>Choose files</button>
      </div>

      {busy && <p><Spinner label={busy} /></p>}
      <ErrorBox error={error} />
      {notice && <Success>{notice}</Success>}

      <div className="row spread" style={{ marginTop: 20 }}>
        <h2 style={{ margin: 0 }}>Your files</h2>
        <button className="primary" disabled={selected.length < 2 || !!busy} onClick={merge}>
          Merge {selected.length >= 2 ? `${selected.length} selected` : "(select 2+)"}
        </button>
      </div>

      {files.loading && !files.data && <p><Spinner label="Loading your library…" /></p>}
      {files.error && <ErrorBox error={files.error} onRetry={files.reload} />}
      {files.data && groups.length === 0 && (
        <Empty title="Nothing here yet">Drop a PDF above to get started. Every operation creates a new versioned file and leaves your original untouched.</Empty>
      )}
      {groups.map((g) => (
        <div className="card" key={g[0].root_id}>
          {g.map((f) => {
            const isPdf = f.mime === "application/pdf";
            return (
              <div key={f.id} className="row spread" style={{ padding: "6px 0", borderTop: f.version ? "1px solid var(--line)" : "none", paddingLeft: f.version ? 18 : 0 }}>
                <div className="row">
                  {isPdf && !f.deleted_at && (
                    <input type="checkbox" aria-label={`Select ${f.display_name}`} checked={selected.includes(f.id)}
                      onChange={(e) => setSelected((s) => (e.target.checked ? [...s, f.id] : s.filter((x) => x !== f.id)))} />
                  )}
                  <div>
                    {f.deleted_at ? <span>{f.display_name}</span> : <Link href={`/files/${f.id}`}>{f.display_name}</Link>}{" "}
                    <span className="badge">{f.kind === "original" ? "original · read-only" : `v${f.version} · ${f.operation}`}</span>{" "}
                    {f.deleted_at && <span className="badge bad">{f.deleted_reason}</span>}
                    <div className="muted">
                      {f.page_count ? `${f.page_count} page${f.page_count === 1 ? "" : "s"} · ` : ""}{fmtBytes(f.size_bytes)} · {fmtDate(f.created_at)}
                      {!f.deleted_at && ` · ${expiryText(f.expires_at)}`}
                    </div>
                  </div>
                </div>
                {!isPdf && !f.deleted_at && g.length === 1 && <button onClick={() => retryConvert(f)} disabled={!!busy}>Convert to PDF</button>}
                {f.warnings.some((w) => w.severity === "warning") && <span className="badge" title="Open the file for details">⚠ review</span>}
              </div>
            );
          })}
        </div>
      ))}
    </>
  );
}
