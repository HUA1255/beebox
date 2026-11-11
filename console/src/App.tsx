import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";

import { clearToken, getToken, storeToken, UNAUTHORIZED_EVENT } from "./api";
import { CredentialsPage } from "./CredentialsPage";
import { HealthPage } from "./HealthPage";
import { JobDetailPage } from "./JobDetailPage";
import { JobsPage } from "./JobsPage";
import { Layout } from "./Layout";
import { LoginPage } from "./LoginPage";
import { SubmitPage } from "./SubmitPage";
import { ModelsPage } from "./ModelsPage";

export function App() {
  const [token, setToken] = useState<string | null>(() => getToken());
  const navigate = useNavigate();

  useEffect(() => {
    // Any /v1 call that comes back 401 clears the stored token in api.ts and
    // fires this event, so the browser returns to the login page with a
    // message instead of rendering an empty, unexplained table.
    const onUnauthorized = () => {
      setToken(null);
      navigate("/login?reason=expired", { replace: true });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [navigate]);

  function signIn(value: string) {
    storeToken(value);
    setToken(value);
  }

  function signOut() {
    clearToken();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <Routes>
      <Route
        path="/login"
        element={
          token ? (
            <Navigate to="/jobs" replace />
          ) : (
            <LoginPage onAuthenticated={signIn} />
          )
        }
      />
      <Route
        element={
          token ? <Layout onSignOut={signOut} /> : <Navigate to="/login" replace />
        }
      >
        <Route index element={<Navigate to="/jobs" replace />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="/submit" element={<SubmitPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/credentials" element={<CredentialsPage />} />
        <Route path="/health" element={<HealthPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/jobs" replace />} />
    </Routes>
  );
}
