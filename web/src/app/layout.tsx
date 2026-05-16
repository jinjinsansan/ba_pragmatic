import type { Metadata } from "next";
import { Inter, Orbitron, Share_Tech_Mono, Shippori_Mincho_B1 } from "next/font/google";
import "./globals.css";

const orbitron = Orbitron({
  subsets: ["latin"],
  weight: ["500", "600", "700", "800", "900"],
  variable: "--font-disp",
});

const shippori = Shippori_Mincho_B1({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-jp",
});

const inter = Inter({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  variable: "--font-body",
});

const mono = Share_Tech_Mono({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "bafather | GUI Operations Console",
  description: "Cyber GUI operations console for members and admin teams.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${orbitron.variable} ${shippori.variable} ${inter.variable} ${mono.variable} min-h-screen bg-bg-primary text-text antialiased font-body`}>
        {children}
      </body>
    </html>
  );
}
