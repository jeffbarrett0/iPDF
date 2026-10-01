"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, Rect, api, fmtDate, pagePreview, useLoad } from "@/lib/api";
import { ErrorBox, Spinner, Success } from "@/components/ui";
import { RegionPicker } from "@/components/RegionPicker";

type Signer = { id: string; name: string; email: string; status: string; consent_at: string | null; signed_at: string | null };
type Field = { id: string; signer_id: string; page: number; x: number; y: number; w: number; h: number; kind: string; label: string };
type Req = { id: string; file_id: string; title: string; status: string; file_name: string; page_count: number; signers: Signer[]; fields: Field[]; sealed_file_id: string | null; sealed_sha256: string | null; source_sha256: string };
type Invite = { signer_id: string; name: string; email: string; delivery: string; detail: string | null; link: string };
const COLORS = ["#d64545", "#2457d6", "#2f9e55", "#b8860b", "#8e44ad", "#0e9aa7"];

export default function SignRequest() {
  const { id } = useParams<{ id: string }>();
  const req = useLoad(() => api<Req>(`/api/sign/requests/${id}`), [id]);
  const [signers, setSigners] = useState<{ name: string; email: string }[]>([{ name: "", email: "" }]);
  const [fields, setFields] = useState<(Rect & { signer: number; kind: string })[]>([]);
  const [active, setActive] = useState(0);
  const [kind, setKind] = useState("signature");
  const [page, setPage] = useState(1);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [saved, setSaved] = useState(false);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // hydrate draft editor from the server once
  useEffect(() => {
    const r = req.data;
    if (!r || loadedFor === r.id) return;
    if (r.signers.length) {
      setSigners(r.signers.map((s) => ({ name: s.name, email: s.email })));
      setFields(r.fields.map((f) => ({ page: f.page, x: f.x, y: f.y, w: f.w, h: f.h, kind: f.kind, signer: r.signers.findIndex((s) => s.id === f.signer_id) })));
    }
    setLoadedFor(r.id);
  }, [req.data, loadedFor]);

  async function act<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    setErr(null); setBusy(label);
    try { return await fn(); } catch (e) { setErr(e as ApiError); } finally { setBusy(null); }
  }
  const save = () => act("Saving layout…", async () => { await api(`/api/sign/requests/${id}`, { method: "PUT", json: { signers, fields } }); setSaved(true); await req.reload(); setLoadedFor(null); });
  const send = () => act("Sending invitations…", async () => {
    if (!saved) await api(`/api/sign/requests/${id}`, { method: "PUT", json: { signers, fields } });
    const r = await api<{ invitations: Invite[] }>(`/api/sign/requests/${id}/send`, { method: "POST" });
    setInvites(r.invitations); await req.reload();
  });
  const newLink = (sid: string) => act("Creating new link…", async () => {
    const i = await api<Invite>(`/api/sign/requests/${id}/signers/${sid}/link`, { method: "POST" });
    setInvites((cur) => [...cur.filter((c) => c.signer_id !== sid), i]); await req.reload();
  });
  const voidIt = () => confirm("Void this request? Existing links stop working.") && act("Voiding…", async () => { await api(`/api/sign/requests/${id}/void`, { method: "POST" }); await req.reload(); });
  const seal = () => act("Sealing…", async () => { await api(`/api/sign/requests/${id}/seal`, { method: "POST" }); await req.reload(); });

  if (req.loading && !req.data) return <p><Spinner label="Loading…" /></p>;
  if (!req.data) return <ErrorBox error={req.error} onRetry={req.reload} />;
  const r = req.data;
  const draft = r.status === "draft";
  const copy = (t: string) => navigator.clipboard?.writeText(t).catch(() => undefined);

  return (
    <>
      <p className="muted"><Link href="/sign">← Signature requests</Link></p>
      <h1>{r.title} <span className="badge">{r.status}</span></h1>
      <p className="muted">Document: <Link href={`/files/${r.file_id}`}>{r.file_name}</Link> · SHA-256 at request time <span className="mono">{r.source_sha256}</span></p>
      <div className="alert warning">Simple electronic signature. Signers are identified only by holding their private link; no ID is checked. Do not use for regulated or high-value agreements.</div>
      <ErrorBox error={err} />
      {busy && <p><Spinner label={busy} /></p>}

      {draft && (
        <>
          <div className="card">
            <h3>1 · Signers</h3>
            {signers.map((s, i) => (
              <div className="row" key={i} style={{ marginBottom: 6 }}>
                <span style={{ width: 12, height: 12, background: COLORS[i % COLORS.length], borderRadius: 3, display: "inline-block" }} />
                <input type="text" placeholder="Name" aria-label={`Signer ${i + 1} name`} value={s.name} onChange={(e) => setSigners(signers.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                <input type="email" placeholder="email@example.com" aria-label={`Signer ${i + 1} email`} value={s.email} onChange={(e) => setSigners(signers.map((x, j) => (j === i ? { ...x, email: e.target.value } : x)))} />
                {signers.length > 1 && <button onClick={() => { setSigners(signers.filter((_, j) => j !== i)); setFields(fields.filter((f) => f.signer !== i).map((f) => ({ ...f, signer: f.signer > i ? f.signer - 1 : f.signer }))); setActive(0); }}>Remove</button>}
              </div>
            ))}
            <button onClick={() => setSigners([...signers, { name: "", email: "" }])}>+ Add signer</button>
          </div>
          <div className="card">
            <h3>2 · Place fields</h3>
            <div className="row">
              <label>Place for <select value={active} onChange={(e) => setActive(+e.target.value)}>{signers.map((s, i) => <option key={i} value={i}>{s.name || `Signer ${i + 1}`}</option>)}</select></label>
              <label>Field <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="signature">Signature</option><option value="date">Date (auto)</option><option value="text">Text</option></select></label>
              <label>Page <select value={page} onChange={(e) => setPage(+e.target.value)}>{Array.from({ length: r.page_count }, (_, i) => <option key={i} value={i + 1}>{i + 1}</option>)}</select></label>
            </div>
            <p className="muted">Drag on the page to draw each field ({fields.length} placed).</p>
            <RegionPicker src={pagePreview(r.file_id, page, 900)} page={page}
              rects={fields.map((f) => ({ ...f, label: `${f.kind} · ${signers[f.signer]?.name || f.signer + 1}`, color: COLORS[f.signer % COLORS.length] }))}
              color={COLORS[active % COLORS.length]} onAdd={(rc) => { setSaved(false); setFields([...fields, { ...rc, signer: active, kind }]); }}
              onRemove={(i) => { setSaved(false); setFields(fields.filter((_, j) => j !== i)); }} />
          </div>
          <div className="row">
            <button onClick={save} disabled={!!busy}>Save layout</button>
            <button className="primary" onClick={send} disabled={!!busy}>3 · Create links &amp; invite signers</button>
          </div>
        </>
      )}

      {!draft && (
        <div className="card">
          <h3>Signers</h3>
          <table><thead><tr><th>Name</th><th>Status</th><th>Consented</th><th>Signed</th><th></th></tr></thead><tbody>
            {r.signers.map((s) => (
              <tr key={s.id}><td>{s.name}<div className="muted">{s.email}</div></td><td><span className={`badge ${s.status === "signed" ? "ok" : s.status === "declined" ? "bad" : ""}`}>{s.status}</span></td>
                <td>{fmtDate(s.consent_at)}</td><td>{fmtDate(s.signed_at)}</td>
                <td>{r.status === "sent" && !["signed", "declined"].includes(s.status) && <button onClick={() => newLink(s.id)} disabled={!!busy}>New link</button>}</td></tr>))}
          </tbody></table>
        </div>
      )}

      {invites.length > 0 && (
        <div className="card">
          <h3>Links to share</h3>
          <p className="muted">Shown only now — only a hash of each link is stored. Use “New link” later if you lose one. Links work only while this computer is reachable by the signer (see README: LAN / tunnel).</p>
          {invites.map((i) => (
            <div key={i.signer_id} className={`alert ${i.delivery === "sent" ? "success" : i.delivery === "failed" ? "error" : "info"}`}>
              <strong>{i.name}</strong> — {i.delivery === "sent" ? `emailed to ${i.email}` : i.delivery === "failed" ? `email failed (${i.detail}); share the link yourself` : "email not configured; share the link yourself"}
              <div className="row"><span className="mono">{i.link}</span><button onClick={() => copy(i.link)}>Copy</button></div>
            </div>
          ))}
        </div>
      )}

      {r.status === "completed" && <div className="alert warning">Everyone signed, but sealing did not finish. <button onClick={seal} disabled={!!busy}>Retry sealing</button></div>}
      {r.status === "sealed" && r.sealed_file_id && (
        <Success>
          Sealed. Final PDF SHA-256: <span className="mono">{r.sealed_sha256}</span> (also written to the <Link href={`/events?file_id=${r.sealed_file_id}`}>event log</Link>).{" "}
          <a className="btn primary" href={`/api/files/${r.sealed_file_id}/download`}>Download signed PDF</a>{" "}
          <Link href={`/files/${r.sealed_file_id}`}>Open</Link>
        </Success>
      )}
      {["sent", "completed"].includes(r.status) && <p><button className="danger" onClick={voidIt}>Void request</button></p>}
    </>
  );
}
