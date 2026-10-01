"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { ApiError, api, fmtDate, useLoad } from "@/lib/api";
import { Empty, ErrorBox, Spinner } from "@/components/ui";

type Ev = { id: number; ts: string; type: string; file_id: string | null; sha256: string | null; outcome: string; details: Record<string, unknown>; hash: string };

function Events() {
  const fileId = useSearchParams().get("file_id");
  const q = fileId ? `&file_id=${fileId}` : "";
  const ev = useLoad(() => api<Ev[]>(`/api/events?limit=200${q}`), [q]);
  const [verify, setVerify] = useState<{ ok: boolean; checked: number; broken_at: number | null } | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  async function check() {
    setErr(null);
    try { setVerify(await api("/api/events/verify")); } catch (e) { setErr(e as ApiError); }
  }
  return (
    <>
      <h1>Event log</h1>
      <p className="muted">Append-only and hash-chained: each entry includes the hash of the one before it, so edits or deletions are detectable. {fileId && <>Showing one file. <Link href="/events">Show all</Link></>}</p>
      <div className="row"><button onClick={check}>Verify integrity of the whole log</button><a className="btn" href="/api/export">Export everything incl. events.csv</a></div>
      {verify && <div className={`alert ${verify.ok ? "success" : "error"}`} role="status">{verify.ok ? `Chain intact — ${verify.checked} entries verified.` : `Chain BROKEN at entry #${verify.broken_at}. History was altered outside the app.`}</div>}
      <ErrorBox error={err} />
      {ev.loading && !ev.data && <p><Spinner label="Loading events…" /></p>}
      <ErrorBox error={ev.error} onRetry={ev.reload} />
      {ev.data?.length === 0 && <Empty title="No events yet">Uploads, operations, downloads and signing steps are recorded here.</Empty>}
      {!!ev.data?.length && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>#</th><th>Time</th><th>Event</th><th>Outcome</th><th>Document SHA-256</th><th>Details</th></tr></thead>
            <tbody>{ev.data.map((e) => (
              <tr key={e.id}>
                <td>{e.id}</td><td>{fmtDate(e.ts)}</td><td>{e.type}</td>
                <td><span className={`badge ${e.outcome === "ok" || e.outcome === "sent" ? "ok" : e.outcome === "error" || e.outcome === "failed" ? "bad" : ""}`}>{e.outcome}</span></td>
                <td className="mono">{e.sha256 ? e.sha256.slice(0, 16) + "…" : "—"}</td>
                <td><details><summary>view</summary><pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(e.details, null, 2)}{"\n"}hash {e.hash}</pre></details></td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}
export default function Page() { return <Suspense fallback={<Spinner label="Loading…" />}><Events /></Suspense>; }
