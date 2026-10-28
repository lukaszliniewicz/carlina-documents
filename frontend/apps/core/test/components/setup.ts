import { afterEach } from "vitest";
import { cleanup } from "@testing-library/svelte";

HTMLDialogElement.prototype.showModal = function showModal(): void {
  this.setAttribute("open", "");
};

HTMLDialogElement.prototype.close = function close(): void {
  this.removeAttribute("open");
  this.dispatchEvent(new Event("close"));
};

afterEach(() => cleanup());
