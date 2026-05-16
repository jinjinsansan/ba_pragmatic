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
        'bg-secondary': '#0b101b',
        'bg-card': 'rgba(15, 20, 35, 0.9)',
        'bg-glass': 'rgba(10, 19, 34, 0.72)',
        text: '#e0e8f0',
        'text-muted': '#7888a0',
        'text-dim': '#4a5568',
        player: {
          DEFAULT: '#00ff88',
          dark: '#00c869',
        },
        banker: {
          DEFAULT: '#ff3366',
          dark: '#c81f4b',
        },
        tie: '#ffcc00',
        accent: '#00e5ff',
      },
      fontFamily: {
        hud: ['var(--font-disp)', 'var(--font-jp)', 'serif'],
        mono: ['var(--font-mono)', 'monospace'],
        body: ['var(--font-body)', 'sans-serif'],
      },
    },
  },
  plugins: [],
} satisfies Config;
