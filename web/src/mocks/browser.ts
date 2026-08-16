// Browser-side MSW worker. Call startMocks() before rendering to run the UI
// against the mock API with zero backend (WP-12 acceptance criterion 1).
import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

export const worker = setupWorker(...handlers);

export async function startMocks(): Promise<void> {
  await worker.start({ onUnhandledRequest: "bypass" });
}
