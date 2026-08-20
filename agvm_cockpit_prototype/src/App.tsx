import { useEffect, useMemo, useState, type CSSProperties } from "react";

type HealthState = {
  ok?: boolean;
  service?: string;
  version?: string;
  active_brain_id?: string | null;
  brain_registry_ready?: boolean;
};

const apiBaseUrl = import.meta.env.VITE_API_URL || "http://localhost:8010";

export function CockpitApp() {
  const [health, setHealth] = useState<HealthState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${apiBaseUrl}/health`, { headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error(`API returned ${response.status}`);
        return response.json() as Promise<HealthState>;
      })
      .then((payload) => {
        if (!cancelled) {
          setHealth(payload);
          setError(null);
        }
      })
      .catch((nextError: unknown) => {
        if (!cancelled) setError(nextError instanceof Error ? nextError.message : "API unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const status = useMemo(() => {
    if (health?.ok) return "API connected";
    if (error) return "API unavailable";
    return "Checking API";
  }, [error, health?.ok]);

  return (
    <main style={pageStyle}>
      <section style={panelStyle}>
        <p style={eyebrowStyle}>AGVM Core</p>
        <h1 style={titleStyle}>Local memory runtime</h1>
        <p style={bodyStyle}>
          Start with the local API, connect an MCP client, then use the public docs to verify the AGVM tool
          surface. The full core cockpit is gated until the public retrieve router extraction is complete.
        </p>
        <div style={statusGridStyle}>
          <Status label="API" value={status} tone={health?.ok ? "good" : error ? "bad" : "pending"} />
          <Status label="Brain" value={String(health?.active_brain_id || "not selected")} tone={health?.brain_registry_ready ? "good" : "pending"} />
          <Status label="Docs" value="MCP setup ready" tone="good" />
        </div>
        <nav style={linkRowStyle}>
          <a style={linkStyle} href="/docs/local-install.md">Local install</a>
          <a style={linkStyle} href="/docs/mcp-codex.md">Codex MCP</a>
          <a style={linkStyle} href="/docs/modules.md">Modules</a>
          <a style={linkStyle} href={`${apiBaseUrl}/docs`}>API docs</a>
        </nav>
      </section>
    </main>
  );
}

function Status({ label, tone, value }: { label: string; tone: "good" | "bad" | "pending"; value: string }) {
  const color = tone === "good" ? "#13d7a2" : tone === "bad" ? "#ff6b6b" : "#b7c3d8";
  return (
    <div style={statusTileStyle}>
      <span style={{ ...dotStyle, background: color }} />
      <div>
        <strong style={statusValueStyle}>{value}</strong>
        <span style={statusLabelStyle}>{label}</span>
      </div>
    </div>
  );
}

const pageStyle = {
  alignItems: "center",
  background: "#071014",
  color: "#eef7f6",
  display: "flex",
  minHeight: "100vh",
  padding: "32px",
} satisfies CSSProperties;

const panelStyle = {
  border: "1px solid rgba(19, 215, 162, 0.28)",
  borderRadius: 8,
  maxWidth: 880,
  padding: 32,
} satisfies CSSProperties;

const eyebrowStyle = {
  color: "#13d7a2",
  fontSize: 12,
  fontWeight: 700,
  letterSpacing: 0,
  margin: "0 0 10px",
  textTransform: "uppercase",
} satisfies CSSProperties;

const titleStyle = {
  fontSize: 34,
  letterSpacing: 0,
  lineHeight: 1.1,
  margin: "0 0 12px",
} satisfies CSSProperties;

const bodyStyle = {
  color: "#b7c3d8",
  fontSize: 16,
  lineHeight: 1.6,
  margin: "0 0 24px",
} satisfies CSSProperties;

const statusGridStyle = {
  display: "grid",
  gap: 12,
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  marginBottom: 24,
} satisfies CSSProperties;

const statusTileStyle = {
  alignItems: "center",
  border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: 8,
  display: "flex",
  gap: 10,
  padding: "12px 14px",
} satisfies CSSProperties;

const dotStyle = {
  borderRadius: "50%",
  display: "inline-block",
  height: 9,
  width: 9,
} satisfies CSSProperties;

const statusValueStyle = {
  display: "block",
  fontSize: 14,
} satisfies CSSProperties;

const statusLabelStyle = {
  color: "#8f9db3",
  display: "block",
  fontSize: 11,
  marginTop: 2,
  textTransform: "uppercase",
} satisfies CSSProperties;

const linkRowStyle = {
  display: "flex",
  flexWrap: "wrap",
  gap: 10,
} satisfies CSSProperties;

const linkStyle = {
  border: "1px solid rgba(19, 215, 162, 0.35)",
  borderRadius: 8,
  color: "#13d7a2",
  fontSize: 14,
  padding: "10px 12px",
  textDecoration: "none",
} satisfies CSSProperties;
