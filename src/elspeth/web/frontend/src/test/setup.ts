import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement window.matchMedia.
// Provide a minimal stub so components that use media queries (e.g. Layout)
// can render without throwing.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }),
});

// jsdom implements no scrolling API on Element — neither scrollTo nor
// scrollIntoView exists, and calling one throws TypeError rather than
// no-opping. Specs that assert scroll behaviour install their own spy; this
// baseline exists so the many specs that merely RENDER a scrolling component
// do not have to. scrollTop remains a plain writable property in jsdom, so
// container-scoped scrolling that assigns it needs no stub.
Object.defineProperty(Element.prototype, "scrollTo", {
  writable: true,
  configurable: true,
  value: function scrollTo(this: Element, options?: ScrollToOptions | number) {
    // Keep the observable side effect jsdom CAN model, so a spec that reads
    // scrollTop after a scroll sees the value the browser would leave.
    if (typeof options === "object" && options !== null && options.top !== undefined) {
      this.scrollTop = options.top;
    }
  },
});
