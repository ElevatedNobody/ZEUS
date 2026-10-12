/* ZEUS boot: sign-in first; the workspace is drawn only after the backend
   confirms a session. A lock or sign-out reloads this page from the top. */
(function () {
  "use strict";
  const M = window.ZEUS;
  document.addEventListener("DOMContentLoaded", async () => {
    const backend = await M.ready;
    if (!backend) {
      M.wave.attach();
      M.wave.mode("auth");
      document.body.appendChild(M.h("div.offline", M.h("div.stack", { style: "align-items:center" },
        M.badge(), M.h("h1", "ZEUS"),
        M.h("p.dim", "Not connected. This page runs only inside the ZEUS application - start it with START.bat."),
        M.h("p.note", "Nothing on this page is live."))));
      return;
    }
    M.auth.start((state) => M.shell.start(state));
  });
})();
