import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // The dev server runs in a container with ./frontend bind-mounted from the
    // host. Filesystem events do not cross that mount on Windows or macOS, so
    // Vite never noticed edits and kept serving the modules it had already
    // transformed — a browser reload did not help, only restarting the
    // container did. Polling costs a little CPU and makes hot reload work.
    watch: { usePolling: true, interval: 300 },
    host: true,
  },
});
