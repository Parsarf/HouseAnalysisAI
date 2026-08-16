import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, login, me, type MeResponse } from "../api";

export function LoginPage(props: { onAuthenticated: (user: MeResponse) => void }) {
  const [password, setPassword] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(password, readOnly);
      props.onAuthenticated(await me());
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 401 ? "Invalid password" : reason instanceof Error ? reason.message : "Unable to sign in");
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-mark" aria-hidden="true">A</div>
        <div className="eyebrow">Acquisition intelligence</div>
        <h1 id="login-title">Welcome to ACQ</h1>
        <p className="login-copy">Underwrite opportunities, resolve risk, and move the right properties forward.</p>
        <form onSubmit={submit}>
          <label className="field-label" htmlFor="password">Workspace password</label>
          <input
            ref={inputRef}
            id="password"
            className="text-input input-lg"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-invalid={Boolean(error)}
          />
          {error && <div className="inline-error" role="alert">{error}</div>}
          <label className="check-row">
            <input type="checkbox" checked={readOnly} onChange={(event) => setReadOnly(event.target.checked)} />
            <span><strong>Read-only session</strong><small>Review analysis without changing portfolio data.</small></span>
          </label>
          <button className="btn btn-primary btn-block" disabled={busy || !password} type="submit">
            {busy ? "Signing in…" : "Continue"}
          </button>
        </form>
        <p className="login-footnote">Financial outputs are deterministic and source-traced.</p>
      </section>
    </main>
  );
}
