import type { Config } from "tailwindcss";

export default {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        'bg-primary': '#05080f',
        'bg-secondary': '#0b0f1b',
        'bg-card': 'rgba(15, 20, 35, 0.85)',
        'bg-glass': 'rgba(20, 28, 50, 0.6)',
        text: '#e0e8f0',
        'text-muted': '#7888a0',
        'text-dim': '#4a5568',
        player: {
          DEFAULT: '#00ff88',
          dark: '#00cc6f',
        },
        banker: {
          DEFAULT: '#ff3366',
          dark: '#b91c1c',
        },
        tie: '#ffcc00',
        accent: '#00e5ff',
      },
      fontFamily: {
        hud: ['Orbitron', 'Inter', 'sans-serif'],
        mono: ['"Share Tech Mono"', 'monospace'],
        body: ['Inter', '"Segoe UI"', 'sans-serif'],
      },
    },
  },
  plugins: [],
} satisfies Config;
