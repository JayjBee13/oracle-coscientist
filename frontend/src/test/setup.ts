import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { clearToasts } from "../lib/toast";
import { BASE_TITLE } from "../lib/notifications";
import { resetCapabilitiesStore } from "../store/capabilities";
import { resetIdentityStore } from "../store/identity";
import { resetRunStore } from "../store/runs";
import { resetModelSettingsStore } from "../store/settings";
import { installMockEventSource, MockEventSource } from "./mockEventSource";

installMockEventSource();

beforeEach(() => {
  MockEventSource.reset();
  resetRunStore();
  resetCapabilitiesStore();
  resetIdentityStore();
  // Both hooks, not just one: the stored default carries a *draft*, so a table
  // edited by one test and left unsaved would arrive dirty in the next.
  resetModelSettingsStore();
  clearToasts();
  window.localStorage.clear();
  document.title = BASE_TITLE;
  // The forced theme is an attribute on a document that outlives the render, so
  // it leaks between tests exactly the way the title does.
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  cleanup();
  MockEventSource.reset();
  resetRunStore();
  resetCapabilitiesStore();
  resetIdentityStore();
  resetModelSettingsStore();
  clearToasts();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
