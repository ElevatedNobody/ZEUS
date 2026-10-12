"""
End-to-end test of the REAL ZEUS interface (Qt WebEngine + bridge + engine),
signed in THROUGH the page - the same auth/web_login.py path an operator uses.

Opt-in, because it opens a window:

    set AGNI_RUN_E2E=1
    python -m pytest tests/e2e/test_medha_e2e.py -v

tests/e2e/app_harness.py --web-login starts the app against a throw-away data
directory (DEMO plant data and the labelled SYNTHETIC knowledge fixture);
tests/e2e/cdp.py drives it with real mouse and key events over Chromium's
debugging protocol on 127.0.0.1. Nothing here talks to a network.
"""

from __future__ import annotations

import time

import pytest

from tests.e2e.process import App

pytestmark = pytest.mark.skipif(__import__("os").environ.get("AGNI_RUN_E2E") != "1",
                                reason="opens the real ZEUS window - set AGNI_RUN_E2E=1")

USER, PASSWORD = "e2e.operator", "E2E-Test-Passw0rd"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    data = tmp_path_factory.mktemp("medha_e2e")
    running = App(data, "--seed-demo", "--web-login", "--seed-fixture-kf")
    try:
        running.wait_for("READY", timeout=120)
        running.data = data
        yield running
    finally:
        running.stop()


@pytest.fixture(scope="module")
def signin(app):
    from tests.e2e.cdp import Page
    pg = Page(app.port, timeout=60)
    pg.evaluate("window.__errs = []; window.addEventListener('error', e => __errs.push(String(e.message))); true")
    pg.wait_for("!!document.querySelector('.auth-card input')", timeout=40)
    yield pg
    pg.close()


def _sign_in(page, password=PASSWORD):
    page.fill("input[name=username]", USER)
    page.fill("input[name=password]", password)
    page.click(".auth-actions .btn")


@pytest.fixture(scope="module")
def page(signin):
    _sign_in(signin, "wrong-password-123")
    signin.wait_for("document.querySelector('.auth-state').innerText.includes('ACCESS DENIED')", timeout=30)
    _sign_in(signin)
    signin.wait_for("!!document.querySelector('.rail-btn')", timeout=240)
    time.sleep(2)
    return signin


def _screen_text(page):
    return page.text(".screen") or ""


# ------------------------------------------------------------ before sign-in --

def test_nothing_protected_exists_before_sign_in(signin):
    assert signin.evaluate("!document.querySelector('.app')")
    assert "LOCKED" in signin.text(".auth-state")
    refused = signin.evaluate("ZEUS.call('status').then(() => 'answered', e => String(e))")
    assert refused != "answered" and "locked" in refused.lower()
    assert "not available" in signin.text(".auth-unavailable").lower()      # PIN / passkey, honestly


def test_a_wrong_password_is_denied_generically(signin):
    _sign_in(signin, "wrong-password-123")
    signin.wait_for("document.querySelector('.auth-state').innerText.includes('ACCESS DENIED')", timeout=30)
    assert "invalid credentials" in signin.text(".auth-msg").lower()
    assert signin.evaluate("!document.querySelector('.app')")


# ------------------------------------------------------------- signed in ----

def test_signed_in_through_the_page(page):
    assert page.text("#s-username") == USER
    assert "ADMIN" in page.text("#s-user")
    urls = page.evaluate("""[...document.querySelectorAll('script[src],link[href],img[src]')].map(e => e.src || e.href)""")
    assert urls and all(u.startswith(("agni://", "qrc:", "data:")) for u in urls), urls
    csp = page.evaluate("document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]').content")
    assert "connect-src 'none'" in csp and "http" not in csp


def test_the_status_strip_reflects_the_backend(page):
    page.wait_for("document.getElementById('s-network').innerText.length > 0", timeout=30)
    assert "SOVEREIGN" in page.text("#s-network").upper()
    status = page.evaluate("ZEUS.call('status')")
    assert status["network"]["mode"] == "sovereign"


def test_demo_data_is_labelled(page):
    page.evaluate("ZEUS.go('industrial'); true")
    page.wait_for("document.querySelector('.screen').innerText.includes('DEMO DATA')", timeout=60)


def test_quick_actions_are_all_present(page):
    page.evaluate("ZEUS.go('command'); true")
    labels = page.evaluate("[...document.querySelectorAll('.quick .btn')].map(b => b.innerText.trim())")
    for expected in ("Plant status", "Open alerts", "Summarize a document", "Search my documents", "Engineering calculation",
                     "Approval note", "Write code", "Prove it (security)", "Write a report", "Make a presentation", "Add a task"):
        assert expected in labels, labels


def _reply(page, text, timeout=240):
    page.wait_for("(t) => ZEUS.conversation.some(c => c.request === t && !c.pending)", text, timeout=timeout)
    return page.evaluate("(t) => ZEUS.conversation.filter(c => c.request === t).pop().result.response", text)


def test_a_quick_action_runs_a_real_command(page):
    page.evaluate("ZEUS.go('command'); true")
    page.click(".quick .btn", contains="Trend C-301")
    assert _reply(page, "show the trend for C-301").strip()


def test_open_alerts_opens_the_alerts_and_plant_status_says_there_is_no_live_connection(page):
    page.evaluate("ZEUS.go('command'); true")
    page.click(".quick .btn", contains="Open alerts")
    page.wait_for("!!document.querySelector('.tab.active') && document.querySelector('.tab.active').innerText === 'Alerts' "
                  "&& document.querySelector('.screen').innerText.includes('Open alerts (')", timeout=90)
    if page.evaluate("document.querySelectorAll('.alert-card').length"):
        text = page.text(".alert-card")
        assert "EQUIPMENT" in text.upper() and "RAISED" in text.upper() and "ACKNOWLEDGED" in text.upper()
    page.evaluate("ZEUS.go('command'); true")
    page.click(".quick .btn", contains="Plant status")
    page.wait_for("document.querySelector('.tab.active') && document.querySelector('.tab.active').innerText === 'Overview' "
                  "&& document.querySelector('.screen').innerText.includes('LIVE PLANT CONNECTION')", timeout=90)
    assert "NO LIVE PLANT CONNECTION" in _screen_text(page)            # the harness has stored DEMO readings only


def test_a_typed_command_runs_the_engineering_engine(page):
    page.fill("#cmd", "convert 10 bar to psi")
    page.key("Enter")
    assert "145.0" in _reply(page, "convert 10 bar to psi")


def test_the_answer_shows_its_pipeline(page):
    page.evaluate("ZEUS.go('command'); true")
    page.wait_for("document.querySelectorAll('.stage-pill').length > 0", timeout=30)


@pytest.mark.parametrize("screen", ["command", "company", "knowledge", "documents", "evidence", "reasoning", "excel", "deck",
                                    "report", "artifacts", "productivity", "industrial", "memory", "models", "agents",
                                    "automation", "settings", "security", "firewall", "audit", "vault"])
def test_every_screen_opens_with_backend_content(page, screen):
    page.evaluate("(s) => { ZEUS.go(s); return true; }", screen)
    page.wait_for("!document.querySelector('.screen .loading') && document.querySelector('.screen').innerText.length > 40", timeout=120)
    text = _screen_text(page)
    assert "undefined" not in text and "[object Object]" not in text, text[:400]
    assert not page.evaluate("document.querySelectorAll('.screen .callout.fail').length"), text[:600]


@pytest.mark.parametrize("tab, marker", [("Face login", "InsightFace"), ("Users", USER)])
def test_the_security_center_tabs(page, tab, marker):
    page.evaluate("(t) => { ZEUS.go('security', {tab: t}); return true; }", tab)
    page.wait_for("(m) => document.querySelector('.screen').innerText.includes(m)", marker, timeout=90)


def test_the_firewall_screen_names_its_layers(page):
    """The subtitle already says 'WFP filters' - wait for what only the LOADED screen shows:
    the privileges line and the ZEUS-owned rule list (or that none exist)."""
    page.evaluate("ZEUS.go('firewall'); true")
    page.wait_for("(() => { const t = document.querySelector('.screen').innerText.toUpperCase(); "
                  "return (t.includes('ZEUS-OWNED') || t.includes('NO ZEUS RULES EXIST')) && t.includes('WFP FILTERS (BIN'); })()",
                  timeout=120)
    text = _screen_text(page).upper()
    assert "ADMIN PRIVILEGES REQUIRED" in text or "ADMINISTRATOR" in text


def test_idle_costs_no_animation_frames(page):
    page.evaluate("ZEUS.go('command'); true")
    page.evaluate("""(() => { if (!window.__rafWrapped) { window.__raf = 0; const o = window.requestAnimationFrame.bind(window);
        window.requestAnimationFrame = (cb) => { window.__raf++; return o(cb); }; window.__rafWrapped = true; } return true; })()""")
    page.evaluate("window.__raf = 0; true")
    time.sleep(3)
    assert page.evaluate("window.__raf") / 3 <= 5


def test_a_high_risk_action_waits_for_a_person_and_deny_means_not_run(page, app):
    victim = app.data / "documents" / "keep_me.txt"
    victim.write_text("do not delete", encoding="utf-8")
    page.evaluate("(p) => { window.__del = ZEUS.tool('delete_file', {path: p}).then(r => ({ok: r}), e => ({error: String(e)})); return true; }", str(victim))
    page.wait_for("!!document.querySelector('#approval-scrim')", timeout=30)
    assert "delete_file" in page.text("#approval-scrim")
    page.click("#approval-scrim .btn.danger")
    result = page.wait_for("window.__del", timeout=30)
    assert victim.exists(), "the file was deleted although the approval was denied"
    assert "error" in result


def test_company_intelligence_timeline_filters_drill_down_and_zoom(page):
    """1989-2026 drawn from the database; a filter dims (never hides) the other years;
    a year opens its detail; zoom and Fit change the scale; no script error."""
    page.evaluate("window.__errs = []; ZEUS.go('company'); true")
    page.wait_for("document.querySelectorAll('.timeline .year').length === 38", timeout=120)
    missing = page.evaluate("document.querySelectorAll('.timeline .col.MISSING').length")
    page.evaluate("const s = document.querySelectorAll('.filters select')[2]; s.value = 'MISSING'; s.dispatchEvent(new Event('change')); true")
    assert page.evaluate("document.querySelectorAll('.timeline .year:not(.dim)').length") == missing
    assert page.evaluate("document.querySelectorAll('.timeline .year').length") == 38        # dimmed, not removed
    page.evaluate("const f = document.querySelector('.filters input'); f.value = '2021'; f.dispatchEvent(new Event('input')); "
                  "document.querySelectorAll('.filters select')[2].value = ''; document.querySelectorAll('.filters select')[2].dispatchEvent(new Event('change')); true")
    assert page.evaluate("[...document.querySelectorAll('.timeline .year:not(.dim) .label')].map(l => l.innerText)") == ["2021"]
    page.evaluate("[...document.querySelectorAll('.timeline .year')].find(y => y.querySelector('.label').innerText === '2021').click(); true")
    page.wait_for("!!document.querySelector('.year-detail h2') && document.querySelector('.year-detail h2').innerText === '2021' "
                  "&& !document.querySelector('.year-detail .loading')", timeout=60)
    before = page.evaluate("document.querySelector('.timeline').style.getPropertyValue('--year-w')")
    page.evaluate("[...document.querySelectorAll('#screen-actions .btn')].find(b => b.innerText.trim() === '+').click(); true")
    assert page.evaluate("document.querySelector('.timeline').style.getPropertyValue('--year-w')") != before
    page.evaluate("[...document.querySelectorAll('#screen-actions .btn')].find(b => b.innerText.trim() === 'Fit').click(); true")
    assert page.evaluate("(() => { const t = document.querySelector('.timeline'); return t.scrollWidth - t.clientWidth; })()") <= 4
    assert page.evaluate("window.__errs") == []


def test_an_unknown_bridge_method_is_refused(page):
    assert page.evaluate("ZEUS.call('eval_python', {code: '1'}).then(() => 'accepted', e => String(e))") != "accepted"


def test_asking_the_records_shows_live_stages_evidence_and_verified_files(page):
    page.evaluate("ZEUS.go('knowledge'); true")
    page.fill(".screen input.field", "Why did we make losses between 2019 and 2021 and what can we do?")
    page.click(".screen .btn.primary", contains="Ask")
    page.wait_for("!!document.querySelector('.screen .answer')", timeout=400)
    assert page.evaluate("document.querySelectorAll('.story .seg.done').length") == 8
    # the fixture corpus is labelled test data: checked, but never "verified company records"
    assert "VALIDATED · NOT COMPANY RECORDS" in _screen_text(page)
    assert "VERIFIED · COMPANY RECORDS" not in _screen_text(page)
    assert page.evaluate("document.querySelectorAll('.evidence-card').length") > 0
    page.click(".screen .btn", contains="Excel workbook")
    page.wait_for("[...document.querySelectorAll('.screen .card.raised')].some(c => c.innerText.includes('XLSX') && c.innerText.includes('VERIFIED'))", timeout=240)


def test_lock_tears_down_the_workspace_and_unlock_restores_it(page, app):
    from tests.e2e.cdp import Page
    page.evaluate("ZEUS.call('lock'); true")
    time.sleep(3)
    fresh = Page(app.port, timeout=30)
    try:
        fresh.wait_for("!!document.querySelector('.auth-card')", timeout=30)
        assert fresh.evaluate("!document.querySelector('.app')")
        assert "Unlock" in fresh.text(".auth-card")
        fresh.fill("input[name=password]", PASSWORD)
        fresh.click(".auth-actions .btn")
        fresh.wait_for("!!document.querySelector('.rail-btn')", timeout=60)
    finally:
        fresh.close()
