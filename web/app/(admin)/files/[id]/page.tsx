"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ReactNode, useState } from "react";
import { ApiError, FileRec, Health, Rect, Warning, api, expiryText, fmtBytes, fmtDate, pagePreview, useLoad } from "@/lib/api";
import { Empty, ErrorBox, Spinner, Success, Warnings } from "@/components/ui";
import { RegionPicker } from "@/components/RegionPicker";

type Detail = { file: FileRec; lineage: FileRec[] };
const TOOLS = ["Pages", "Split", "Compress", "Watermark", "Redact", "Annotate", "OCR", "Sign"] as const;
type Tool = (typeof TOOLS)[number];

/** Shows warnings first, then runs the operation and reports the new version(s). */
function Runner({ op, file, label, build, onDone, disabled }: {
  op: string; file: FileRec; label: string; build: () => Record<string, unknown> | string; onDone: () => void; disabled?: boolean;
}) {
  const [state, setState] = useState<"idle" | "checking" | "confirm" | "running">("idle");
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [made, setMade] = useState<FileRec[]>([]);

  async function start() {
    setError(null); setMade([]);
    const params = build();
    if (typeof params === "string") { setError(new Error(params)); return; }
    setState("checking");
    try {
      const { warnings } = await api<{ warnings: Warning[] }>(`/api/ops/${op}/preflight`, { json: { file_ids: [file.id], params } });
      setWarnings(warnings);
      if (warnings.some((w) => w.severity === "warning")) setState("confirm");
      else await run(params);
    } catch (e) { setError(e as ApiError); setState("idle"); }
  }
  async function run(params = build()) {
    if (typeof params === "string") return;
    setState("running");
    try {
      const r = await api<{ outputs: FileRec[] }>(`/api/ops/${op}`, { json: { file_ids: [file.id], params } });
      setMade(r.outputs); onDone();
    } catch (e) { setError(e as ApiError); }
    setState("idle"); setWarnings([]);
  }
  return (
    <div style={{ marginTop: 12 }}>
      {state === "confirm" && (
        <>
          <Warnings items={warnings} />
          <div className="row"><button className="primary" onClick={() => run()}>Create new version anyway</button><button onClick={() => setState("idle")}>Cancel</button></div>
        </>
      )}
      {state !== "confirm" && (
        <button className="primary" onClick={start} disabled={disabled || state !== "idle"}>
          {state === "checking" ? "Checking…" : state === "running" ? "Working…" : label}
        </button>
      )}
      {(state === "checking" || state === "running") && <> <Spinner label={state === "running" ? "Creating a new version — large files can take a while…" : "Checking for risks…"} /></>}
      <ErrorBox error={error} />
      {made.length > 0 && (
        <Success>
          Created {made.length} new file{made.length > 1 ? "s" : ""}:{" "}
          {made.map((m) => <span key={m.id}><Link href={`/files/${m.id}`}>{m.display_name}</Link>{" "}</span>)}
          <Warnings items={made[0].warnings.filter((w) => !warnings.some((x) => x.code === w.code))} />
        </Success>
      )}
    </div>
  );
}

function F({ label, children }: { label: string; children: ReactNode }) { return <label className="f">{label}{children}</label>; }

export default function FilePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const detail = useLoad(() => api<Detail>(`/api/files/${id}`), [id]);
  const health = useLoad(() => api<Health>("/api/health"));
  const [tool, setTool] = useState<Tool>("Pages");
  const [page, setPage] = useState(1);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [verify, setVerify] = useState<string | null>(null);

  // tool state
  const [order, setOrder] = useState<string | null>(null);
  const [rotPages, setRotPages] = useState("all");
  const [degrees, setDegrees] = useState(90);
  const [splitMode, setSplitMode] = useState("ranges");
  const [ranges, setRanges] = useState("");
  const [every, setEvery] = useState(1);
  const [level, setLevel] = useState("medium");
  const [wm, setWm] = useState({ text: "CONFIDENTIAL", opacity: 0.25, size: 64, angle: 45, color: "#888888", pages: "all" });
  const [terms, setTerms] = useState("");
  const [regions, setRegions] = useState<Rect[]>([]);
  const [annType, setAnnType] = useState("highlight");
  const [annText, setAnnText] = useState("");
  const [annColor, setAnnColor] = useState("#ffe033");
  const [anns, setAnns] = useState<(Rect & { type: string; text: string; color: string })[]>([]);
  const [lang, setLang] = useState("eng");
  const [force, setForce] = useState(false);
  const [signTitle, setSignTitle] = useState("");

  if (detail.loading && !detail.data) return <p><Spinner label="Loading file…" /></p>;
  if (detail.error || !detail.data) return <ErrorBox error={detail.error} onRetry={detail.reload} />;
  const { file, lineage } = detail.data;
  const pages = file.page_count ?? 0;
  const isPdf = file.mime === "application/pdf";
  const gone = !!file.deleted_at;
  const orderValue = order ?? Array.from({ length: pages }, (_, i) => i + 1).join(",");
  const reload = () => detail.reload();

  async function del() {
    if (!confirm(`Delete ${file.display_name}? Its content is removed from disk; the audit record stays.`)) return;
    try { await api(`/api/files/${file.id}`, { method: "DELETE" }); router.push("/"); } catch (e) { setActionError(e as ApiError); }
  }
  async function setExpiry(days: number | null) {
    try { await api(`/api/files/${file.id}/expiry`, { json: { days } }); await reload(); } catch (e) { setActionError(e as ApiError); }
  }
  async function check() {
    try { const v = await api<{ ok: boolean }>(`/api/files/${file.id}/verify`); setVerify(v.ok ? "Hash matches — the file on disk is exactly what was recorded." : "MISMATCH: the file on disk differs from its recorded hash!"); }
    catch (e) { setActionError(e as ApiError); }
  }
  async function startSign() {
    try {
      const r = await api<{ id: string }>("/api/sign/requests", { json: { file_id: file.id, title: signTitle || file.display_name } });
      router.push(`/sign/${r.id}`);
    } catch (e) { setActionError(e as ApiError); }
  }

  return (
    <>
      <p className="muted"><Link href="/">← Library</Link></p>
      <h1>{file.display_name}</h1>
      <div className="card">
        <div className="row spread">
          <div>
            <span className="badge">{file.kind === "original" ? "original · read-only" : `version ${file.version} · ${file.operation}`}</span>{" "}
            {gone ? <span className="badge bad">{file.deleted_reason}</span> : <span className="badge">{expiryText(file.expires_at)}</span>}
            <div className="muted">{pages ? `${pages} pages · ` : ""}{fmtBytes(file.size_bytes)} · created {fmtDate(file.created_at)}</div>
            <div className="mono">sha256 {file.sha256}</div>
          </div>
          {!gone && (
            <div className="row">
              <a className="btn primary" href={`/api/files/${file.id}/download`}>Download</a>
              <button onClick={check}>Verify</button>
              <button onClick={() => setExpiry(null)}>Keep forever</button>
              <button onClick={() => setExpiry(30)}>Expire in 30 days</button>
              <button className="danger" onClick={del}>Delete</button>
            </div>
          )}
        </div>
        {verify && <div className={`alert ${verify.startsWith("Hash") ? "success" : "error"}`}>{verify}</div>}
      </div>
      <ErrorBox error={actionError} />
      {gone && <div className="alert warning">This file&apos;s content was {file.deleted_reason}. Only its record and history remain — restore from a backup if you need it.</div>}
      <Warnings items={file.warnings} />
      {file.operation === "redact" && <div className="alert info">This is a redacted copy. The original still contains the redacted content — delete it if that is the point.</div>}

      {isPdf && !gone && (
        <>
          <h2>Pages</h2>
          <div className="grid">
            {Array.from({ length: pages }, (_, i) => i + 1).map((n) => (
              <div key={n} className={`thumb ${page === n ? "sel" : ""}`} onClick={() => setPage(n)} role="button" tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setPage(n)} aria-label={`Select page ${n}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={pagePreview(file.id, n, 160)} alt={`Page ${n}`} loading="lazy" />
                <div className="muted">{n}</div>
              </div>
            ))}
          </div>

          <h2>Tools <span className="muted">— each creates a new version; this file is never changed</span></h2>
          <div className="tabs" role="tablist">
            {TOOLS.map((t) => <button key={t} role="tab" aria-selected={tool === t} className={tool === t ? "on" : ""} onClick={() => setTool(t)}>{t}</button>)}
          </div>
          <div className="card">
            {tool === "Pages" && (
              <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
                <div>
                  <h3>Reorder / remove pages</h3>
                  <F label="New order (comma-separated; leave pages out to remove them)">
                    <input type="text" value={orderValue} onChange={(e) => setOrder(e.target.value)} style={{ width: 260 }} />
                  </F>
                  <Runner op="reorder" file={file} label="Apply new order" onDone={reload}
                    build={() => { const o = orderValue.split(",").map((s) => parseInt(s.trim(), 10)); return o.some(isNaN) ? "Page order must be numbers like 3,1,2." : { order: o }; }} />
                </div>
                <div>
                  <h3>Rotate</h3>
                  <div className="row">
                    <F label="Pages"><input type="text" value={rotPages} onChange={(e) => setRotPages(e.target.value)} style={{ width: 120 }} placeholder="all or 1-3,5" /></F>
                    <F label="Angle"><select value={degrees} onChange={(e) => setDegrees(+e.target.value)}><option value={90}>90° clockwise</option><option value={180}>180°</option><option value={270}>90° counter-clockwise</option></select></F>
                  </div>
                  <Runner op="rotate" file={file} label="Rotate" onDone={reload} build={() => ({ pages: rotPages, degrees })} />
                </div>
              </div>
            )}
            {tool === "Split" && (
              <>
                <div className="row">
                  <F label="Mode"><select value={splitMode} onChange={(e) => setSplitMode(e.target.value)}><option value="ranges">By ranges</option><option value="every">Every N pages</option><option value="each">One file per page</option></select></F>
                  {splitMode === "ranges" && <F label="Ranges, one output each"><input type="text" value={ranges} onChange={(e) => setRanges(e.target.value)} placeholder="1-3,4-6,7-" /></F>}
                  {splitMode === "every" && <F label="Pages per file"><input type="number" min={1} value={every} onChange={(e) => setEvery(+e.target.value)} style={{ width: 90 }} /></F>}
                </div>
                <Runner op="split" file={file} label="Split" onDone={reload} build={() => ({ mode: splitMode, ranges, every })} />
              </>
            )}
            {tool === "Compress" && (
              <>
                <F label="Level"><select value={level} onChange={(e) => setLevel(e.target.value)}>
                  <option value="low">Low — lossless structure optimisation only</option>
                  <option value="medium">Medium — images downsampled to ≤2000px, JPEG 75</option>
                  <option value="high">High — images downsampled to ≤1200px, JPEG 55</option></select></F>
                <Runner op="compress" file={file} label="Compress" onDone={reload} build={() => ({ level })} />
              </>
            )}
            {tool === "Watermark" && (
              <>
                <div className="row">
                  <F label="Text"><input type="text" value={wm.text} maxLength={80} onChange={(e) => setWm({ ...wm, text: e.target.value })} /></F>
                  <F label="Opacity"><input type="number" step={0.05} min={0.05} max={1} value={wm.opacity} onChange={(e) => setWm({ ...wm, opacity: +e.target.value })} style={{ width: 80 }} /></F>
                  <F label="Size"><input type="number" min={8} max={300} value={wm.size} onChange={(e) => setWm({ ...wm, size: +e.target.value })} style={{ width: 80 }} /></F>
                  <F label="Angle"><input type="number" min={-90} max={90} value={wm.angle} onChange={(e) => setWm({ ...wm, angle: +e.target.value })} style={{ width: 80 }} /></F>
                  <F label="Colour"><input type="color" value={wm.color} onChange={(e) => setWm({ ...wm, color: e.target.value })} /></F>
                  <F label="Pages"><input type="text" value={wm.pages} onChange={(e) => setWm({ ...wm, pages: e.target.value })} style={{ width: 110 }} /></F>
                </div>
                <Runner op="watermark" file={file} label="Add watermark" onDone={reload} build={() => ({ ...wm })} />
              </>
            )}
            {tool === "Redact" && (
              <>
                <div className="alert warning">Redaction rasterizes the pages it touches, so the covered text is truly gone from the new version. Your original still contains it.</div>
                <F label="Find and black out text (one per line, case-insensitive)">
                  <textarea rows={3} value={terms} onChange={(e) => setTerms(e.target.value)} placeholder="Jane Doe&#10;123-45-6789" />
                </F>
                <p className="muted">…and/or drag on the page to draw boxes ({regions.length} drawn). Scanned pages have no text to search — run OCR first or draw boxes.</p>
                <div className="row"><label>Page <select value={page} onChange={(e) => setPage(+e.target.value)}>{Array.from({ length: pages }, (_, i) => <option key={i} value={i + 1}>{i + 1}</option>)}</select></label></div>
                <RegionPicker src={pagePreview(file.id, page, 900)} page={page} rects={regions} onAdd={(r) => setRegions([...regions, r])} onRemove={(i) => setRegions(regions.filter((_, j) => j !== i))} />
                <Runner op="redact" file={file} label="Redact into new version" onDone={reload}
                  build={() => { const t = terms.split("\n").map((s) => s.trim()).filter(Boolean); return !t.length && !regions.length ? "Enter text to find or draw at least one box." : { terms: t, regions }; }} />
              </>
            )}
            {tool === "Annotate" && (
              <>
                <div className="row">
                  <F label="Type"><select value={annType} onChange={(e) => setAnnType(e.target.value)}><option value="highlight">Highlight</option><option value="text">Text box</option><option value="note">Sticky note</option><option value="rect">Rectangle</option></select></F>
                  {(annType === "text" || annType === "note") && <F label="Text"><input type="text" value={annText} onChange={(e) => setAnnText(e.target.value)} /></F>}
                  <F label="Colour"><input type="color" value={annColor} onChange={(e) => setAnnColor(e.target.value)} /></F>
                  <label>Page <select value={page} onChange={(e) => setPage(+e.target.value)}>{Array.from({ length: pages }, (_, i) => <option key={i} value={i + 1}>{i + 1}</option>)}</select></label>
                </div>
                <p className="muted">Drag on the page to place each annotation ({anns.length} placed).</p>
                <RegionPicker src={pagePreview(file.id, page, 900)} page={page} color={annColor}
                  rects={anns.map((a) => ({ ...a, label: a.type, color: a.color }))}
                  onAdd={(r) => { if ((annType === "text" || annType === "note") && !annText.trim()) { setActionError(new ApiError("need_text", "Type the annotation text first.")); return; } setActionError(null); setAnns([...anns, { ...r, type: annType, text: annText, color: annColor }]); }}
                  onRemove={(i) => setAnns(anns.filter((_, j) => j !== i))} />
                <Runner op="annotate" file={file} label="Save annotations as new version" onDone={reload}
                  build={() => anns.length ? { annotations: anns } : "Place at least one annotation."} />
              </>
            )}
            {tool === "OCR" && (
              <>
                {health.data && !health.data.tools.tesseract && <div className="alert error">Tesseract is not installed, so OCR is unavailable. Install <code>tesseract-ocr</code> and <code>ghostscript</code>, then reload.</div>}
                <div className="row">
                  <F label="Languages (e.g. eng or eng+deu)"><input type="text" value={lang} onChange={(e) => setLang(e.target.value)} style={{ width: 160 }} /></F>
                  <label><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> Re-OCR pages that already have text</label>
                </div>
                <Runner op="ocr" file={file} label="Run OCR" onDone={reload} disabled={health.data ? !health.data.tools.tesseract : false} build={() => ({ languages: lang, force })} />
              </>
            )}
            {tool === "Sign" && (
              <>
                <p>Send this file for signature: place signature, date and text fields, invite signers by link or email, record their consent, and seal the result with a SHA-256 hash.</p>
                <div className="alert warning">Simple electronic signatures only — no identity verification, not qualified or regulated.</div>
                <F label="Request title"><input type="text" value={signTitle} placeholder={file.display_name} onChange={(e) => setSignTitle(e.target.value)} /></F>
                <div style={{ marginTop: 10 }}><button className="primary" onClick={startSign}>Set up signature request</button></div>
              </>
            )}
          </div>
        </>
      )}

      <h2>Version history</h2>
      {lineage.length <= 1 ? <Empty title="No versions yet">Run a tool above — the result appears here as a new file.</Empty> : (
        <div className="card">
          <table>
            <thead><tr><th>Version</th><th>File</th><th>Operation</th><th>Created</th><th>SHA-256</th></tr></thead>
            <tbody>{lineage.map((f) => (
              <tr key={f.id}>
                <td>{f.version === 0 ? "original" : `v${f.version}`}</td>
                <td>{f.deleted_at ? f.display_name : <Link href={`/files/${f.id}`}>{f.display_name}</Link>}{f.id === file.id && " (this)"}</td>
                <td>{f.operation ?? "upload"}</td><td>{fmtDate(f.created_at)}</td><td className="mono">{f.sha256.slice(0, 16)}…</td>
              </tr>))}</tbody>
          </table>
          <p className="muted"><a href={`/api/export?root_id=${file.root_id}`}>Export this file&apos;s whole history (zip)</a></p>
        </div>
      )}
      <p className="muted"><Link href={`/events?file_id=${file.id}`}>Event log for this file →</Link></p>
    </>
  );
}
