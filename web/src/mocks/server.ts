// Node-side MSW server for tests (vitest/jest) exercising the API client offline.
// Node has no location origin, so handlers are bound to an absolute one; pass the
// same origin as the client's base when constructing a server for another URL.
import { setupServer } from "msw/node";
import { createHandlers } from "./handlers";

export const MOCK_ORIGIN = "http://localhost";

export const server = setupServer(...createHandlers(MOCK_ORIGIN));

