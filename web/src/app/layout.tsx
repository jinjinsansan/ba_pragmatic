import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LAPLACE - AI Baccarat Prediction Engine",
  description: "AI-powered baccarat prediction with automated bet execution",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg-primary text-text antialiased font-body">
        {children}
      </body>
    </html>
  );
}
