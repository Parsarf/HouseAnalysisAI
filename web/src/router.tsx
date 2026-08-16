/**
 * Minimal history-API router. The app has two routes today:
 *   /                     -> portfolio table
 *   /properties/:id       -> deal page
 * No dependency on react-router (package.json is frozen); if the app grows
 * beyond a handful of routes this file can be swapped for it behind the same
 * Link/usePath/navigate surface.
 */
import { useEffect, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";

function currentPath(): string {
  return window.location.pathname + window.location.search;
}

/** Subscribe to navigation; returns `pathname + search`. */
export function usePath(): string {
  const [path, setPath] = useState<string>(currentPath);
  useEffect(() => {
    const onChange = () => setPath(currentPath());
    window.addEventListener("popstate", onChange);
    return () => window.removeEventListener("popstate", onChange);
  }, []);
  return path;
}

export function navigate(to: string): void {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new Event("popstate"));
}

/** Update the URL (for shareable filter state) without triggering navigation. */
export function replaceUrl(to: string): void {
  window.history.replaceState(null, "", to);
}

export function Link(props: { to: string; children: ReactNode; style?: CSSProperties; className?: string; onClick?: () => void }) {
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    props.onClick?.();
    navigate(props.to);
  };
  return (
    <a href={props.to} onClick={onClick} style={props.style} className={props.className}>
      {props.children}
    </a>
  );
}

export type Route =
  | { name: "login" }
  | { name: "properties" }
  | { name: "deal"; propertyId: string }
  | { name: "dashboard" }
  | { name: "rankings" }
  | { name: "flags" }
  | { name: "changes" }
  | { name: "batches" }
  | { name: "problems" }
  | { name: "assumptions" }
  | { name: "settings" }
  | { name: "not-found" };

export function matchRoute(path: string): Route {
  const pathname = (path.split("?")[0] || "/").replace(/\/+$/, "") || "/";
  if (pathname === "/login") return { name: "login" };
  if (pathname === "/" || pathname === "/properties") return { name: "properties" };
  if (pathname === "/dashboard") return { name: "dashboard" };
  if (pathname === "/rankings") return { name: "rankings" };
  if (pathname === "/flags") return { name: "flags" };
  if (pathname === "/changes") return { name: "changes" };
  if (pathname === "/batches") return { name: "batches" };
  if (pathname === "/problems") return { name: "problems" };
  if (pathname === "/assumptions") return { name: "assumptions" };
  if (pathname === "/settings") return { name: "settings" };
  const deal = /^\/properties\/([0-9a-fA-F-]{32,36})$/.exec(pathname);
  if (deal) return { name: "deal", propertyId: deal[1] };
  return { name: "not-found" };
}
