"use client";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";
import { ApiError, api, useLoad } from "@/lib/api";
import { ErrorBox, Spinner, Success } from "@/components/ui";

type PField = { id: string; page: number; x: number; y: number; w: number; h: number; kind: string; label: string; required: boolean; mine: boolean; filled: boolean };
type Info = {
  title: string; message: string; request_status: string; document: string; page_count: number; signer: { name: string; status: string };
  consent_text: string; fields: PField[]; sealed: boolean;
};
type Val = { mode?: "typed" | "drawn"; text?: string; data_url?: string };

function Pad({ onChange }: { onChange: (url: string | null) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const drawing = useRef(false);
  const pt = (e: React.PointerEvent) => {
    const c = ref.current!, b = c.getBoundingClientRect();
    return { x: ((e.clientX - b.left) / b.width) * c.width, y: ((e.clientY - b.top) / b.height) * c.height };
  };
  return (
    <div>
      <canvas ref={ref} className="sigpad" width={840} height={300} aria-label="Draw your signature"
        onPointerDown={(e) => { drawing.current = true; e.currentTarget.setPointerCapture(e.pointerId); const c = ref.current!.getContext("2d")!; const p = pt(e); c.beginPath(); c.moveTo(p.x, p.y); c.lineWidth = 6; c.lineCap = "round"; c.lineJoin = "round"; c.strokeStyle = "#111"; }}
        onPointerMove={(e) => { if (!drawing.current) return; const c = ref.current!.getContext("2d")!; const p = pt(e); c.lineTo(p.x, p.y); c.stroke(); }}
        onPointerUp={() => { drawing.current = false; onChange(ref.current!.toDataURL("image/png")); }} />
      <button type="button" onClick={() => { ref.current!.getContext("2d")!.clearRect(0, 0, 840, 300); onChange(null); }}>Clear</button>
    </div>
  );
}

export default function Signer() {
  const { token } = useParams<{ token: string }>();
  const info = useLoad(() => api<Info>(`/api/public/sign/${token}`), [token]);
  const [agree, setAgree] = useState(false);
  const [consented, setConsented] = useState(false);
  const [vals, setVals] = useState<Record<string, Val>>({});
  const [mode, setMode] = useState<Record<string, "typed" | "drawn">>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [done, setDone] = useState<{ sealed: boolean } | null>(null);
  const [declined, setDeclined] = useState(false);

  async function act(label: string, fn: () => Promise<void>) {
    setErr(null); setBusy(label);
    try { await fn(); } catch (e) { setErr(e as ApiError); } finally { setBusy(null); }
  }
  const consent = () => act("Recording consent…", async () => { await api(`/api/public/sign/${token}/consent`, { json: { agree } }); setConsented(true); });
  const submit = () => act("Signing…", async () => {
    const values: Record<string, Val> = {};
    for (const f of info.data!.fields.filter((x) => x.mine)) if (vals[f.id]) values[f.id] = vals[f.id];
    const r = await api<{ sealed: boolean }>(`/api/public/sign/${token}/sign`, { json: { values } });
    setDone(r); await info.reload();
  });
  const decline = () => confirm("Decline to sign this document?") && act("Declining…", async () => {
    await api(`/api/public/sign/${token}/decline`, { json: { reason: "" } }); setDeclined(true);
  });

  if (info.loading && !info.data) return <main><p><Spinner label="Opening your signing link…" /></p></main>;
  if (!info.data) return <main><h1>Signing link</h1><ErrorBox error={info.error} onRetry={info.reload} /></main>;
  const d = info.data;
  const mine = d.fields.filter((f) => f.mine);
  const status = d.signer.status;
  const finished = done || status === "signed" || declined || status === "declined";
  const ready = consented || status === "consented";

  return (
    <main>
      <h1>{d.title}</h1>
      <p className="muted">Hello {d.signer.name}. Document: {d.document}</p>
      {d.message && <div className="card">{d.message}</div>}
      <div className="alert warning">This is a simple electronic signature. Your identity is not verified. Only sign if you trust the sender.</div>
      <ErrorBox error={err} />
      {busy && <p><Spinner label={busy} /></p>}

      {declined || status === "declined" ? <div className="alert info">You declined to sign. The sender can see this.</div> : null}
      {finished && !declined && status !== "declined" && (
        <Success>
          Thank you — your signature is recorded.{" "}
          {d.sealed ? <a className="btn primary" href={`/api/public/sign/${token}/document`}>Download the sealed PDF</a> : "Once everyone has signed, the sender will seal the final PDF."}
        </Success>
      )}

      {!finished && (
        <>
          {!ready && (
            <div className="card">
              <h3>Before you sign</h3>
              <p>{d.consent_text}</p>
              <label><input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} /> I agree to sign electronically</label>
              <div className="row" style={{ marginTop: 10 }}>
                <button className="primary" disabled={!agree || !!busy} onClick={consent}>Continue</button>
                <button className="danger" onClick={decline} disabled={!!busy}>Decline</button>
              </div>
            </div>
          )}

          <h2>Review the document</h2>
          {Array.from({ length: d.page_count }, (_, i) => i + 1).map((n) => (
            <div key={n} className="picker" style={{ display: "block", marginBottom: 12, maxWidth: 900 }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={`/api/public/sign/${token}/pages/${n}/preview?width=900`} alt={`Page ${n}`} loading="lazy" />
              {d.fields.filter((f) => f.page === n).map((f) => (
                <div key={f.id} className="box" style={{ left: `${f.x * 100}%`, top: `${f.y * 100}%`, width: `${f.w * 100}%`, height: `${f.h * 100}%`, borderColor: f.mine ? "#2457d6" : "#999", background: f.mine ? "#2457d633" : "#9993" }}>
                  <span style={{ background: f.mine ? "#2457d6" : "#777", padding: "0 3px" }}>{f.mine ? `Your ${f.kind}` : `Other signer's ${f.kind}`}</span>
                </div>
              ))}
            </div>
          ))}

          {ready && (
            <div className="card">
              <h3>Your fields</h3>
              {mine.map((f) => (
                <div key={f.id} style={{ marginBottom: 14 }}>
                  <strong>{f.kind === "signature" ? `Signature (page ${f.page})` : f.kind === "date" ? `Date (page ${f.page}) — filled automatically` : f.label || `Text (page ${f.page})`}</strong>
                  {f.kind === "text" && <div><input type="text" value={vals[f.id]?.text ?? ""} maxLength={200} onChange={(e) => setVals({ ...vals, [f.id]: { mode: "typed", text: e.target.value } })} /></div>}
                  {f.kind === "signature" && (
                    <div>
                      <div className="tabs">
                        <button className={(mode[f.id] ?? "typed") === "typed" ? "on" : ""} onClick={() => { setMode({ ...mode, [f.id]: "typed" }); setVals({ ...vals, [f.id]: { mode: "typed", text: "" } }); }}>Type</button>
                        <button className={mode[f.id] === "drawn" ? "on" : ""} onClick={() => { setMode({ ...mode, [f.id]: "drawn" }); setVals({ ...vals, [f.id]: { mode: "drawn" } }); }}>Draw</button>
                      </div>
                      {mode[f.id] === "drawn"
                        ? <Pad onChange={(u) => setVals({ ...vals, [f.id]: u ? { mode: "drawn", data_url: u } : { mode: "drawn" } })} />
                        : <input type="text" aria-label="Type your signature" maxLength={80} value={vals[f.id]?.text ?? ""} placeholder={d.signer.name} style={{ fontFamily: "'Times New Roman', serif", fontStyle: "italic", fontSize: 22 }}
                            onChange={(e) => setVals({ ...vals, [f.id]: { mode: "typed", text: e.target.value } })} />}
                    </div>
                  )}
                </div>
              ))}
              <div className="row">
                <button className="primary" onClick={submit} disabled={!!busy}>Sign document</button>
                <button className="danger" onClick={decline} disabled={!!busy}>Decline</button>
              </div>
            </div>
          )}
        </>
      )}
    </main>
  );
}
