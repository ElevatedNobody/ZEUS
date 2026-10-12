/* Local maths for answers: the LaTeX a model writes (\frac, \text, \times, ^ and _,
   \sqrt, Greek, aligned / cases / matrix) turned into HTML + CSS. No CDN, no library.

   The input is ALREADY HTML-escaped by M.answer; the output holds only the tags built
   here, so a model cannot inject markup through maths. A command this file does not
   know is shown by its name - never as a raw backslash command. */
(function () {
  "use strict";
  const M = window.ZEUS;

  const SYM = {
    times: "×", cdot: "·", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆", circ: "∘", bullet: "∙",
    le: "≤", leq: "≤", ge: "≥", geq: "≥", neq: "≠", ne: "≠", approx: "≈", sim: "∼", simeq: "≃", equiv: "≡",
    cong: "≅", propto: "∝", ll: "≪", gg: "≫", lt: "&lt;", gt: "&gt;",
    to: "→", rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", Leftarrow: "⇐", leftrightarrow: "↔",
    Leftrightarrow: "⇔", implies: "⇒", iff: "⇔", mapsto: "↦", uparrow: "↑", downarrow: "↓",
    infty: "∞", sum: "∑", prod: "∏", int: "∫", oint: "∮", partial: "∂", nabla: "∇",
    forall: "∀", exists: "∃", neg: "¬", lnot: "¬", land: "∧", lor: "∨", wedge: "∧", vee: "∨",
    in: "∈", notin: "∉", ni: "∋", subset: "⊂", subseteq: "⊆", supset: "⊃", supseteq: "⊇",
    cup: "∪", cap: "∩", emptyset: "∅", varnothing: "∅", setminus: "∖",
    angle: "∠", perp: "⊥", parallel: "∥", mid: "∣", degree: "°", prime: "′", therefore: "∴", because: "∵",
    ldots: "…", cdots: "⋯", vdots: "⋮", ddots: "⋱", dots: "…",
    langle: "⟨", rangle: "⟩", lceil: "⌈", rceil: "⌉", lfloor: "⌊", rfloor: "⌋", vert: "|", Vert: "‖",
    alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε", zeta: "ζ", eta: "η",
    theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π",
    varpi: "ϖ", rho: "ρ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ", phi: "φ", varphi: "φ",
    chi: "χ", psi: "ψ", omega: "ω", Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
    Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω", ell: "ℓ", hbar: "ℏ",
  };
  const SPACE = { ",": " ", ":": " ", ";": " ", "!": "", " ": " ", quad: " ", qquad: "  ",
    enspace: " ", thinspace: " ", medspace: " ", thickspace: " " };
  const IGNORE = new Set(["left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
    "biggl", "biggr", "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits", "nonumber", "notag", "label"]);
  const TEXT = new Set(["text", "textrm", "textnormal", "textsf", "texttt", "textit", "textbf", "mbox", "hbox"]);
  const FONT = new Set(["mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathbb", "mathfrak",
    "boldsymbol", "bm", "operatorname", "mathscr"]);
  const FUNCS = new Set(["sin", "cos", "tan", "sec", "csc", "cot", "sinh", "cosh", "tanh", "arcsin", "arccos",
    "arctan", "log", "ln", "lg", "exp", "max", "min", "sup", "inf", "lim", "det", "arg", "deg", "gcd", "mod", "Pr"]);
  const ACCENT = { hat: "̂", widehat: "̂", tilde: "̃", widetilde: "̃", vec: "⃗", dot: "̇", ddot: "̈", acute: "́", grave: "̀", check: "̌", breve: "̆" };
  const BB = { R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ", P: "ℙ", E: "𝔼" };
  const KNOWN = (name) => name in SYM || name in SPACE || IGNORE.has(name) || TEXT.has(name) || FONT.has(name)
    || FUNCS.has(name) || name in ACCENT || ["frac", "dfrac", "tfrac", "cfrac", "binom", "sqrt", "overline", "bar",
      "underline", "boxed", "begin", "end", "percent"].includes(name);

  /* ---------------------------------------------------------------- parser -- */
  function render(src, script) {        // script: inside ^{...} or _{...}
    let i = 0;
    const n = src.length;

    function readName() {                      // after a backslash
      if (i >= n) return "";
      if (/[a-zA-Z]/.test(src[i])) { const s = i; while (i < n && /[a-zA-Z]/.test(src[i])) i++; return src.slice(s, i); }
      return src[i++];                          // \, \% \{ ...
    }
    function rawGroup() {                      // '{' ... matching '}' as raw source
      while (i < n && src[i] === " ") i++;
      if (src[i] !== "{") return null;
      let depth = 0; const s = i;
      for (; i < n; i++) {
        if (src[i] === "\\") { i++; continue; }
        if (src[i] === "{") depth++;
        else if (src[i] === "}" && --depth === 0) { i++; return src.slice(s + 1, i - 1); }
      }
      return src.slice(s + 1);
    }
    function optional() {                      // [n] after \sqrt
      if (src[i] !== "[") return null;
      const close = src.indexOf("]", i);
      if (close < 0) return null;
      const v = src.slice(i + 1, close); i = close + 1; return v;
    }
    function arg() {                           // a group or one token
      while (i < n && src[i] === " ") i++;
      if (src[i] === "{") return render(rawGroup(), script);
      return atom();
    }
    function entity() {                        // &amp; &lt; ... pass through whole
      const semi = src.indexOf(";", i);
      if (semi > i && semi - i <= 8) { const e = src.slice(i, semi + 1); i = semi + 1; return e; }
      i++; return "&amp;";
    }
    function environment(name) {
      const open = `\\begin{${name}}`, close = `\\end{${name}}`;
      let depth = 1, j = i;
      while (j < n && depth) {
        if (src.startsWith(open, j)) { depth++; j += open.length; }
        else if (src.startsWith(close, j)) { if (--depth === 0) break; j += close.length; }
        else j++;
      }
      let body = src.slice(i, j);
      i = Math.min(n, j + close.length);
      if (name === "array" && body.startsWith("{")) body = body.slice(body.indexOf("}") + 1);   // column spec
      const rows = split(body, "\\\\").map((r) => split(r, "&amp;").map((c) => render(c.trim(), script)));
      const table = `<span class="m-table ${name.replace(/\*$/, "")}">` + rows.filter((r) => r.some((c) => c))
        .map((r) => `<span class="m-row">${r.map((c) => `<span class="m-cell">${c}</span>`).join("")}</span>`).join("") + "</span>";
      const delim = { pmatrix: ["(", ")"], bmatrix: ["[", "]"], Bmatrix: ["{", "}"], vmatrix: ["|", "|"], Vmatrix: ["‖", "‖"], cases: ["{", ""] }[name];
      return delim ? `<span class="m-delim">${delim[0]}</span>${table}<span class="m-delim">${delim[1]}</span>` : table;
    }
    function command() {
      i++;                                      // the backslash
      const name = readName();
      if (name in SPACE) return SPACE[name];
      if (/^\s$/.test(name)) return " ";                // backslash + newline: a space
      if (IGNORE.has(name)) { if (name === "label") rawGroup(); return ""; }
      if (name === "\\") return "<br>";
      if ("%$#_{}&".includes(name) && name.length === 1) return name === "&" ? "&amp;" : name;
      if (name === "|") return "‖";
      if (name in SYM) return `<span class="m-sym">${SYM[name]}</span>`;
      if (FUNCS.has(name)) return `<span class="m-fn">${name}</span>`;
      if (name === "frac" || name === "dfrac" || name === "tfrac" || name === "cfrac") {
        const num = arg(), den = arg();
        if (script) return `<span class="m-sfrac">${num}/${den}</span>`;   // 1/n in an exponent
        return `<span class="m-frac"><span class="m-num">${num}</span><span class="m-den">${den}</span></span>`;
      }
      if (name === "binom") { const a = arg(), b = arg(); return `(<span class="m-frac m-binom"><span class="m-num">${a}</span><span class="m-den">${b}</span></span>)`; }
      if (name === "sqrt") { const root = optional(); const x = arg(); return `<span class="m-sqrt">${root ? `<sup>${render(root)}</sup>` : ""}√<span class="m-rad">${x}</span></span>`; }
      if (TEXT.has(name)) { const t = rawGroup(); return `<span class="m-text${name === "textbf" ? " m-bold" : ""}">${(t || "").replace(/\\([%$#&_{}])/g, "$1")}</span>`; }
      if (name === "mathbb") { const t = rawGroup() || ""; return `<span class="m-roman">${t.replace(/[A-Z]/g, (c) => BB[c] || c)}</span>`; }
      if (FONT.has(name)) { const t = arg(); return `<span class="${name === "mathbf" || name === "boldsymbol" || name === "bm" ? "m-bold" : "m-roman"}">${t}</span>`; }
      if (name === "overline" || name === "bar") return `<span class="m-over">${arg()}</span>`;
      if (name === "underline") return `<span class="m-under">${arg()}</span>`;
      if (name === "boxed") return `<span class="m-box">${arg()}</span>`;
      if (name in ACCENT) return `${arg()}${ACCENT[name]}`;
      if (name === "begin") { const env = rawGroup() || ""; return environment(env); }
      if (name === "end") { rawGroup(); return ""; }
      if (name === "percent") return "%";
      return `<span class="m-text">${name}</span>`;         // unknown: its name, never "\name"
    }
    function atom() {
      const c = src[i];
      if (c === "\\") return command();
      if (c === "{") return render(rawGroup(), script);
      if (c === "&") return entity();
      i++;
      if (/[a-zA-Z]/.test(c)) return `<i>${c}</i>`;
      if (c === "-") return '<span class="m-op">−</span>';
      if ("+=<>±×÷".includes(c)) return `<span class="m-op">${c}</span>`;
      if (c === "*") return '<span class="m-op">∗</span>';
      if (c === "'") return "′";
      if (c === "~") return " ";
      if (c === "}") return "";
      return c;
    }

    let out = "";
    while (i < n) {
      const c = src[i];
      if (c === " " || c === "\n" || c === "\t") { i++; continue; }
      if (c === "^" || c === "_") {
        i++;
        const tag = c === "^" ? "sup" : "sub";
        const outer = script;
        script = true;
        const inner = arg();
        script = outer;
        out += `<${tag}>${inner}</${tag}>`;
        continue;
      }
      out += atom();
    }
    return out;
  }

  /* Split on a separator at brace depth 0 ("\\" for rows, "&amp;" for cells). */
  function split(body, sep) {
    const parts = [];
    let depth = 0, last = 0;
    for (let k = 0; k < body.length; k++) {
      const ch = body[k];
      if (ch === "\\" && sep !== "\\\\") { k++; continue; }
      if (ch === "{") depth++;
      else if (ch === "}") depth--;
      else if (depth === 0 && body.startsWith(sep, k)) {
        parts.push(body.slice(last, k)); k += sep.length - 1; last = k + 1;
      }
    }
    parts.push(body.slice(last));
    return sep === "\\\\" ? parts.map((p) => p.replace(/^\s*\[\d+(?:\.\d+)?(?:pt|em|ex|mm)\]/, "")) : parts;   // "\\[2pt]"
  }

  function html(tex, display) {
    const body = render(String(tex || "").trim());
    return display ? `<span class="math math-display">${body}</span>` : `<span class="math">${body}</span>`;
  }

  /* Find the maths in escaped text and swap it for placeholders (\u0001k\u0001),
     so the Markdown pass never sees it. Returns the text; `store` gets {html, display}. */
  function extract(text, store) {
    const put = (tex, display) => { store.push({ html: html(tex, display), display }); return `\u0001${store.length - 1}\u0001`; };
    let s = String(text || "").replace(/\u0001/g, "");
    s = s.replace(/\\\[([\s\S]+?)\\\]/g, (m, t) => put(t, true));
    s = s.replace(/\$\$([\s\S]+?)\$\$/g, (m, t) => put(t, true));
    s = s.replace(/\\\(([\s\S]+?)\\\)/g, (m, t) => put(t, false));
    // $...$ only when it reads as maths ("$5 and $10" stays money)
    s = s.replace(/(^|[^\\$\w])\$(?!\s)([^$\n]{1,300}?)(?<!\s)\$(?![\w$])/g, (m, lead, t) =>
      /[\\^_{}]/.test(t) ? lead + put(t, false) : m);
    // an environment written without delimiters is a display block
    s = s.replace(/\\begin\{([a-zA-Z]+\*?)\}[\s\S]*?\\end\{\1\}/g, (m) => put(m, true));
    // bare commands in running text: "margin = \frac{a}{b} \times 100"
    s = s.replace(/\\([a-zA-Z]+)((?:\s*\[[^\]\n]*\])?(?:\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})*(?:[_^](?:\{[^{}\n]*\}|\w))*)/g,
      (m, name) => (KNOWN(name) ? put(m, false) : m));
    return s.replace(/\\([%#])/g, "$1");                  // "12\%" in prose is "12%"
  }

  M.math = { render: html, extract };
})();
