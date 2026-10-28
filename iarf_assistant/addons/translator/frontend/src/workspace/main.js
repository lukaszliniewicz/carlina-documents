import { mount } from "svelte";
import WorkspaceGate from "./WorkspaceGate.svelte";

const root = document.getElementById("translator-root");
if (root) {
  mount(WorkspaceGate, { target: root });
}
