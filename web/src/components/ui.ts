/**
 * Shared inline-style primitives. Kept as plain objects so components stay
 * dependency-free; web/src/style.css is owned elsewhere.
 */
import type { CSSProperties } from "react";

export const palette = {
  text: "#1c2330",
  muted: "#6b7280",
  border: "#d8dde4",
  accent: "#175c45",
  good: "#1a7f4b",
  warn: "#a15c07",
  bad: "#b3261e",
  surface: "#ffffff",
  subtle: "#f4f6f9",
} as const;

export const card: CSSProperties = {
  background: palette.surface,
  border: `1px solid ${palette.border}`,
  borderRadius: 14,
  padding: "20px 22px",
  marginBottom: 16,
  boxShadow: "0 1px 2px rgba(20,35,28,.04), 0 12px 30px rgba(20,35,28,.05)",
};

export const cardTitle: CSSProperties = {
  margin: "0 0 12px",
  fontSize: 15,
  fontWeight: 600,
  color: palette.text,
};

export const table: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 14,
};

export const th: CSSProperties = {
  textAlign: "left",
  padding: "8px 10px",
  borderBottom: `2px solid ${palette.border}`,
  color: palette.muted,
  fontWeight: 600,
  whiteSpace: "nowrap",
};

export const td: CSSProperties = {
  padding: "8px 10px",
  borderBottom: `1px solid ${palette.border}`,
  verticalAlign: "top",
};

export const button: CSSProperties = {
  border: `1px solid ${palette.border}`,
  borderRadius: 6,
  background: palette.surface,
  padding: "5px 12px",
  fontSize: 13,
  cursor: "pointer",
  color: palette.text,
};

export const activeButton: CSSProperties = {
  ...button,
  background: palette.accent,
  borderColor: palette.accent,
  color: "#fff",
};

export const mutedText: CSSProperties = { color: palette.muted, fontSize: 13 };

export function severityColor(severity: string): string {
  if (severity === "critical" || severity === "error") return palette.bad;
  if (severity === "warning") return palette.warn;
  return palette.muted;
}
