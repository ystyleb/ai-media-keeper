/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        // NASVault palette — keep small, expand later
        sidebar: {
          bg: "#1a1d23",
          fg: "#cdd2d8",
          "fg-active": "#ffffff",
          accent: "#6699cc",
        },
        statusbar: {
          bg: "#2d3138",
          fg: "#a0a4ab",
          ok: "#7eb377",
          warn: "#d9b04a",
          err: "#cc6666",
          gray: "#6c727b",
        },
      },
    },
  },
  plugins: [],
};
