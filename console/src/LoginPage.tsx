import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, validateToken } from "./api";
import { describeError } from "./errors";

interface LoginPageProps {
  onAuthenticated: (token: string) => void;
}

export function LoginPage({ onAuthenticated }: LoginPageProps) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const expired = searchParams.get("reason") === "expired";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) {
      setError("请输入 BeeBox token。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await validateToken(candidate);
      onAuthenticated(candidate);
      navigate("/jobs", { replace: true });
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) {
        setError("token 不正确，请检查 BEEBOX_API_TOKEN 后重试。");
      } else {
        setError(describeError(failure));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell" data-testid="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1>BeeBox 控制台</h1>
        <p className="muted">输入服务端配置的 BEEBOX_API_TOKEN。</p>
        <label className="field">
          <span>API token</span>
          <input
            autoFocus
            autoComplete="off"
            name="token"
            data-testid="login-token"
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="Bearer token"
          />
        </label>
        {expired && !error ? (
          <p className="alert" data-testid="login-expired">
            登录已失效，请重新输入 token。
          </p>
        ) : null}
        {error ? (
          <p className="alert" role="alert" data-testid="login-error">
            {error}
          </p>
        ) : null}
        <button type="submit" className="button primary" data-testid="login-submit" disabled={busy}>
          {busy ? "验证中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
