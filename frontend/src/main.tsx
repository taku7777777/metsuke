// Entry point: mount the client app onto the shell's #app node. The shell loads this
// bundle with `defer`, so the DOM is parsed by the time we run; we still guard readyState
// so the same bundle mounts correctly when eval'd synchronously (the jsdom render gate).
import { render } from "preact";
import { App } from "./app";
import "./styles.css";

function mount(): void {
  const host = document.getElementById("app");
  if (host) {
    render(<App />, host);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
