import { createContext, useContext, type ReactNode } from "react";
import type { MeResponse } from "./api";

interface AuthValue {
  user: MeResponse;
  signOut: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider(props: { value: AuthValue; children: ReactNode }) {
  return <AuthContext.Provider value={props.value}>{props.children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
