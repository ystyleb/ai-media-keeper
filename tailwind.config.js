/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/**/*.js",
  ],
  safelist: [
    // 动态构造的 class 名（partials 用 `class="sb-dot-{{ p.dot }}"`），
    // JIT 字面扫描看不到 "sb-dot-ok" 等真实类名，必须 safelist 保留。
    "sb-dot-ok",
    "sb-dot-warn",
    "sb-dot-err",
    "sb-dot-gray",
  ],
  theme: {
    extend: {
      colors: {
        // 方向 A 媒体中心风 palette（spec: docs/superpowers/specs/2026-06-10-visual-redesign-design.md）
        canvas: "#0d0b16",
        surface: { from: "#1a142f", to: "#130f22" },
        sidebar: {
          bg: "#15102a",
          from: "#15102a",
          to: "#0d0b16",
          fg: "#9b91b8",
          "fg-active": "#d8cdf8",
          accent: "#a78bfa",
        },
        statusbar: {
          bg: "#0a0813",
          fg: "#8d80b5",
          ok: "#34d399",
          warn: "#fbbf24",
          err: "#f87171",
          gray: "#6f6590",
        },
        ink: {
          DEFAULT: "#e6e1f2",
          strong: "#f5f2fd",
          soft: "#9b91b8",
          mute: "#6f6590",
        },
        line: { DEFAULT: "#2a2150", faint: "#221a3e" },
        accent: { DEFAULT: "#a78bfa", deep: "#7c3aed", alt: "#d946ef", "alt-soft": "#f0abfc" },
      },
      borderRadius: { card: "16px" },
    },
  },
  plugins: [],
};
