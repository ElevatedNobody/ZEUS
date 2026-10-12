"""
Maths in answers (ui/medha/js/math.js): a model's LaTeX is shown as maths - never
as raw \\frac, \\[ or \\times - money and Windows paths are left alone, and nothing a
model writes inside maths can become markup. Runs the real file under Node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATH_JS = ROOT / "ui" / "medha" / "js" / "math.js"
NODE = shutil.which("node")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { window: { ZEUS: {} } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const M = ctx.window.ZEUS;
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(cases.map((t) => { const store = []; const s = M.math.extract(esc(t), store);
  return s.replace(/\u0001(\d+)\u0001/g, (m, k) => store[+k].html); })));
"""

CASES = {
    "display": r"\[ \text{Net margin} = \frac{\text{PAT}}{\text{Revenue}} \times 100 \]",
    "dollars": r"$$x^2 + y_1 = 3$$",
    "inline": r"Tolerance \( \alpha \le 5\% \) applies.",
    "money": "It costs $5 and $10 per unit.",
    "path": r"Saved to C:\Users\NIRBHAY\Documents\report.docx",
    "bare": r"Growth = \frac{b}{a} - 1, i.e. 12\% a year",
    "aligned": r"\begin{aligned} a &= b + c \\ d &= e \end{aligned}",
    "cases": r"\[ f(x) = \begin{cases} 1 & x > 0 \\ 0 & \text{otherwise} \end{cases} \]",
    "sqrt": r"\( \sqrt{x^2 + 1} \approx \sqrt[3]{8} \)",
    "injection": r"\[ \text{<script>alert(1)</script>} + \frac{<b>1</b>}{2} \]",
    "unknown": r"\[ \foo{x} + \Delta \]",
    "cagr": r"\[ \text{CAGR} = \left( \frac{V_{end}}{V_{start}} \right)^{\frac{1}{n}} - 1 \]",
    "newline": "\\[ a \\\n+ b \\]",
}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if not NODE:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("math")
    (folder / "h.js").write_text(HARNESS, encoding="utf-8")
    (folder / "cases.json").write_text(json.dumps(list(CASES.values())), encoding="utf-8")
    out = subprocess.run([NODE, str(folder / "h.js"), str(MATH_JS), str(folder / "cases.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return dict(zip(CASES, json.loads(out.stdout)))


RAW = ("\\frac", "\\text", "\\times", "\\[", "\\]", "\\(", "\\)", "\\begin", "\\end", "\\left", "\\right", "\\sqrt",
       "\\alpha", "\\le", "\\approx", "\\Delta", "\\foo", "$$")


@pytest.mark.parametrize("name", [k for k in CASES if k not in ("money", "path")])
def test_no_raw_latex_is_left(rendered, name):
    html = rendered[name]
    assert not [r for r in RAW if r in html], html


def test_fractions_text_and_operators(rendered):
    html = rendered["display"]
    assert 'class="math math-display"' in html and 'class="m-frac"' in html
    assert "Net margin" in html and "PAT" in html and "Revenue" in html and "×" in html


def test_scripts_greek_roots_and_percent(rendered):
    assert "<sup>2</sup>" in rendered["dollars"] and "<sub>1</sub>" in rendered["dollars"]
    assert "α" in rendered["inline"] and "≤" in rendered["inline"] and "5%" in rendered["inline"]
    assert "√" in rendered["sqrt"] and "≈" in rendered["sqrt"] and "<sup>3</sup>" in rendered["sqrt"]
    assert "12% a year" in rendered["bare"] and 'class="m-frac"' in rendered["bare"]


def test_environments_become_tables(rendered):
    assert rendered["aligned"].count('class="m-row"') == 2
    assert rendered["cases"].count('class="m-row"') == 2 and "otherwise" in rendered["cases"]


def test_money_and_paths_are_not_maths(rendered):
    assert rendered["money"] == "It costs $5 and $10 per unit."
    assert rendered["path"] == r"Saved to C:\Users\NIRBHAY\Documents\report.docx"


def test_nothing_inside_maths_becomes_markup(rendered):
    html = rendered["injection"]
    assert "<script" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html


def test_an_unknown_command_shows_its_name(rendered):
    assert "foo" in rendered["unknown"] and "Δ" in rendered["unknown"]


def test_the_cagr_formula_a_model_writes(rendered):
    html = rendered["cagr"]
    assert "CAGR" in html and "<sub>" in html and html.count('class="m-frac"') == 1
    assert '<sup><span class="m-sfrac">1/<i>n</i></span></sup>' in html       # an exponent's fraction reads 1/n


def test_a_backslash_before_a_line_break_is_a_space_not_a_command(rendered):
    assert 'class="m-text">\n' not in rendered["newline"] and "a" in rendered["newline"]
