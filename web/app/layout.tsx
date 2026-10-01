import type { Metadata } from "next";
import "./globals.css";
import { LowStakesBanner } from "@/components/ui";

export const metadata: Metadata = { title: "iPDF — local PDF toolkit", description: "Merge, split, compress, convert, annotate and sign PDFs on your own machine." };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <LowStakesBanner />
        {children}
      </body>
    </html>
  );
}
