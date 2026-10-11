import typography from '@tailwindcss/typography'

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', '"PingFang SC"', '"Noto Sans SC"', 'system-ui', 'sans-serif'],
        display: ['"Cinzel"', 'serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        abyss: { 950: '#07060d', 900: '#0d0b18', 850: '#121024', 800: '#181530', 700: '#221e40', 600: '#2f2a55' },
      },
      keyframes: {
        drift: { '0%,100%': { transform: 'translate(0,0) scale(1)' }, '50%': { transform: 'translate(3%,-4%) scale(1.08)' } },
        pulseDot: { '0%,100%': { opacity: 1 }, '50%': { opacity: 0.35 } },
        rise: { from: { opacity: 0, transform: 'translateY(6px)' }, to: { opacity: 1, transform: 'none' } },
      },
      animation: {
        drift: 'drift 22s ease-in-out infinite',
        pulseDot: 'pulseDot 1.6s ease-in-out infinite',
        rise: 'rise .28s ease-out both',
      },
    },
  },
  plugins: [typography],
}
