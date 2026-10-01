"use client";
import Link from "next/link";
import { api, fmtDate, useLoad } from "@/lib/api";
import { Empty, ErrorBox, Spinner } from "@/components/ui";

type Req = { id: string; title: string; status: string; file_name: string; signer_count: number; signed_count: number; created_at: string };

export default function SignList() {
  const reqs = useLoad(() => api<Req[]>("/api/sign/requests"));
  return (
    <>
      <h1>Signature requests</h1>
      <div className="alert warning">Simple electronic signatures: link-based, no identity verification, not qualified or regulated.</div>
      {reqs.loading && !reqs.data && <p><Spinner label="Loading…" /></p>}
      <ErrorBox error={reqs.error} onRetry={reqs.reload} />
      {reqs.data?.length === 0 && <Empty title="No signature requests">Open a PDF in your library and choose <strong>Tools → Sign</strong>.</Empty>}
      {reqs.data?.map((r) => (
        <div className="card row spread" key={r.id}>
          <div><Link href={`/sign/${r.id}`}>{r.title}</Link> <span className={`badge ${r.status === "sealed" ? "ok" : ""}`}>{r.status}</span>
            <div className="muted">{r.file_name} · {r.signed_count}/{r.signer_count} signed · {fmtDate(r.created_at)}</div></div>
        </div>
      ))}
    </>
  );
}
