import "@testing-library/jest-dom/vitest";

// jsdom does not implement the modal <dialog> API used by ConfirmDialog
// (M11C). This minimal stand-in reproduces the observable contract the
// component relies on: `open` reflects state, and `close()` fires `close`.
// Real focus trapping/Escape handling is verified in the Playwright suite.
if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}
