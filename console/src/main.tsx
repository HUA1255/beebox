import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {/* BeeBox serves this build at /console, so client routes are relative to it. */}
    <BrowserRouter basename="/console">
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
