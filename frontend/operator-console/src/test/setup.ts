import { vi } from "vitest";

class TestResizeObserver implements ResizeObserver {
  disconnect = vi.fn();
  observe = vi.fn();
  unobserve = vi.fn();
}

vi.stubGlobal("ResizeObserver", TestResizeObserver);
