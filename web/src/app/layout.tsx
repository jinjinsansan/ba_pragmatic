import type { Metadata } from "next";
import { Cormorant_Garamond, Inter, JetBrains_Mono, Shippori_Mincho_B1 } from "next/font/google";
import "./globals.css";

const cormorant = Cormorant_Garamond({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-disp",
});

const shippori = Shippori_Mincho_B1({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-jp",
});

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-body",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "bafather | Premium Baccarat Operations",
  description: "Premium baccarat operations console for members and admin teams.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${cormorant.variable} ${shippori.variable} ${inter.variable} ${mono.variable} min-h-screen bg-bg-primary text-text antialiased font-body`}>
        {children}
      </body>
    </html>
  );
}
