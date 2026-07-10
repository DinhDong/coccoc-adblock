import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import ModerationBoard from "./pages/dashboard.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ModerationBoard />
  </StrictMode>
);
