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
        'bg-primary': '#0a0807',
        'bg-secondary': '#14110d',
        'bg-card': 'rgba(28, 24, 18, 0.9)',
        'bg-glass': 'rgba(36, 30, 22, 0.58)',
        text: '#f3e9d2',
        'text-muted': '#a89d83',
        'text-dim': '#6b624f',
        player: {
          DEFAULT: '#7a9778',
          dark: '#5f785e',
        },
        banker: {
          DEFAULT: '#b14a43',
          dark: '#8f3a34',
        },
        tie: '#d9a64a',
        accent: '#c9a875',
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
