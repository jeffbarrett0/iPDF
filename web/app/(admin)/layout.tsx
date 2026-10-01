import Link from "next/link";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className="top">
        <nav aria-label="Main">
          <Link className="brand" href="/">iPDF</Link>
          <Link className="nav" href="/">Library</Link>
          <Link className="nav" href="/sign">Signatures</Link>
          <Link className="nav" href="/events">Event log</Link>
          <Link className="nav" href="/storage">Storage &amp; backup</Link>
        </nav>
      </header>
      <main>{children}</main>
    </>
  );
}
