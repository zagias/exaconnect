"""Signs in to the public portal in a real browser and opens every screen.

Run by lab/ci/checks/m9-public-e2e.sh inside the Playwright image. Prints
"ok", "FAIL" or "note" lines. Reads the admin sign-in from the environment
and never prints it.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

HOST = os.environ["E2E_HOST"]
# E2E_BASE overrides the address for a local run over plain HTTP.
BASE = os.environ.get("E2E_BASE", f"https://{HOST}")
SCREENS = [
    ("/", "Overview"),
    ("/sites", "Sites"),
    ("/traffic/applications", "Traffic: seen on your network"),
    ("/traffic/rules", "Traffic: rules"),
    ("/traffic/classes", "Traffic: classes"),
    ("/decisions", "Routing moves"),
    ("/fabric", "Fabric"),
    ("/internet", "Internet"),
    ("/order", "Order"),
    ("/encryption", "Encryption"),
    ("/integrations", "Integrations"),
    ("/notices", "Carrier notices"),
    ("/metering", "Usage"),
    ("/billing", "Billing"),
    ("/insights", "Alerts"),
    ("/ask", "Ask your network"),
    ("/carrier", "Carrier view"),
    ("/account", "Account"),
    ("/admin/agents", "Admin: agents"),
    ("/admin/sites", "Admin: sites"),
    ("/admin/classes", "Admin: classes"),
    ("/admin/users", "Admin: users"),
    ("/admin/settings", "Admin: settings"),
    ("/admin/partners", "Admin: partners"),
    ("/admin/billing", "Admin: billing"),
    ("/admin/integrations", "Admin: integrations"),
    ("/admin/protection", "Admin: protection"),
    ("/admin/releases", "Admin: releases"),
    ("/admin/audit", "Admin: audit"),
    # Jibsy, in menu order (portal/src/nav.tsx), with every tab.
    ("/commai", "Jibsy: inbox"),
    ("/commai/contacts", "Jibsy: contacts"),
    ("/commai/voice", "Jibsy: phone, people and numbers"),
    ("/commai/voice/routing", "Jibsy: phone, call routing"),
    ("/commai/voice/changes", "Jibsy: phone, scheduled, history and bulk"),
    ("/commai/voice/access", "Jibsy: phone, access"),
    ("/commai/voice/orders", "Jibsy: phone, orders"),
    ("/commai/voice/numbers", "Jibsy: phone, numbers"),
    ("/commai/voice/ports", "Jibsy: phone, ports"),
    ("/commai/voice/emergency", "Jibsy: phone, emergency"),
    ("/commai/voice/fraud", "Jibsy: phone, fraud"),
    ("/commai/voice/billing", "Jibsy: phone, billing"),
    ("/commai/voice/me", "Jibsy: phone, my phone"),
    ("/commai/voice/carriers", "Jibsy: phone, carriers"),
    ("/commai/voice/phone", "Jibsy: phone, browser phone"),
    ("/commai/team", "Jibsy: team"),
    ("/commai/ai", "Jibsy: AI agents, profile and mode"),
    ("/commai/ai/tools", "Jibsy: AI agents, tools"),
    ("/commai/ai/knowledge", "Jibsy: AI agents, knowledge"),
    ("/commai/ai/gaps", "Jibsy: AI agents, knowledge gaps"),
    ("/commai/ai/call", "Jibsy: AI agents, browser call"),
    ("/commai/workflows", "Jibsy: workflows"),
    ("/commai/approvals", "Jibsy: approvals"),
    ("/commai/integrations", "Jibsy: apps, your apps"),
    ("/commai/catalogue", "Jibsy: apps, catalogue"),
    ("/commai/assistant", "Jibsy: assistant"),
    ("/commai/reports", "Jibsy: reports"),
    ("/commai/quality", "Jibsy: quality, review"),
    ("/commai/quality/flags", "Jibsy: quality, flags"),
    ("/commai/quality/gaps", "Jibsy: quality, knowledge gaps"),
    ("/commai/quality/follow-ups", "Jibsy: quality, follow-ups"),
    ("/commai/quality/satisfaction", "Jibsy: quality, satisfaction"),
    ("/commai/bill", "Jibsy: usage and bill"),
    ("/commai/setup", "Jibsy: getting started"),
    ("/commai/channels/web", "Jibsy: channels, website chat"),
    ("/commai/channels/whatsapp", "Jibsy: channels, WhatsApp"),
    ("/commai/channels/sms", "Jibsy: channels, SMS"),
    ("/commai/channels/email", "Jibsy: channels, email"),
    ("/commai/channels/messenger", "Jibsy: channels, Messenger"),
    ("/commai/channels/instagram", "Jibsy: channels, Instagram"),
    ("/commai/channels/telegram", "Jibsy: channels, Telegram"),
    ("/commai/countries", "Jibsy: countries"),
    ("/commai/languages", "Jibsy: languages"),
    ("/commai/governance", "Jibsy: AI governance"),
    ("/commai/settings", "Jibsy: settings, service"),
    ("/commai/settings/teams", "Jibsy: settings, teams and people"),
    ("/commai/settings/routing", "Jibsy: settings, routing"),
    ("/commai/settings/developers", "Jibsy: settings, webhooks and keys"),
    ("/commai/settings/sign-in", "Jibsy: settings, sign-in"),
    ("/commai/settings/organisation", "Jibsy: settings, organisation"),
    ("/commai/settings/roles", "Jibsy: settings, roles"),
    ("/commai/settings/security", "Jibsy: settings, security"),
    ("/commai/settings/data", "Jibsy: settings, data"),
    ("/commai/settings/help-centre", "Jibsy: settings, help centre"),
    ("/commai/partner", "Jibsy: partners"),
    ("/commai/me", "Jibsy: my settings"),
    ("/commai/golive", "Jibsy: go-live"),
    ("/commai/support", "Jibsy: support queue"),
]
failed = 0


def ok(msg: str) -> None:
    print(f"ok    {msg}", flush=True)


def bad(msg: str) -> None:
    global failed
    failed += 1
    print(f"FAIL  {msg}", flush=True)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: m.type == "error" and errors.append(m.text[:200]))
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        api_fail: list[str] = []
        page.on(
            "response",
            lambda r: "/api/" in r.url and r.status >= 500 and api_fail.append(f"{r.status} {r.url.split(HOST)[-1]}"),
        )

        # Signed out: the forgotten-password screens work and give nothing away.
        page.goto(BASE + "/forgot", wait_until="networkidle")
        if page.get_by_role("heading", name="Reset your password").count():
            page.fill('input[type="email"]', "nobody@example.invalid")
            page.click('button[type="submit"]')
            page.wait_for_timeout(1500)
            if page.locator('[role="status"]').all_inner_texts():
                ok("forgotten-password page answers")
            else:
                bad("forgotten-password page gave no answer")
        else:
            bad("forgotten-password page did not open")
        page.goto(BASE + "/reset/not-a-real-link", wait_until="networkidle")
        page.wait_for_timeout(1000)
        if "expired" in page.inner_text("body"):
            ok("a bad reset link is refused")
        else:
            bad("a bad reset link was not refused")
        errors.clear()

        page.goto(BASE + "/", wait_until="networkidle")
        page.fill('input[type="email"]', os.environ["E2E_EMAIL"])
        # Email first (ADR 0017): the password box appears once the email is checked.
        if not page.locator('input[type="password"]').count():
            page.click('button[type="submit"]')
            page.wait_for_selector('input[type="password"]', timeout=10000)
        page.fill('input[type="password"]', os.environ["E2E_PASSWORD"])
        page.click('button[type="submit"]')
        try:
            page.wait_for_selector(".shell main#main", timeout=15000)
            ok("signed in through the browser")
        except Exception:
            bad("sign-in did not reach the portal")
            return 1

        for path, name in SCREENS:
            errors.clear()
            api_fail.clear()
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(1500)
            # On a busy host the organisation can load late; give it a moment.
            for _ in range(10):
                if "Choose an organisation first" not in page.inner_text("body"):
                    break
                page.wait_for_timeout(1000)
            alerts = [a.strip() for a in page.locator('[role="alert"]').all_inner_texts() if a.strip()]
            text = page.locator("main").inner_text() if page.locator("main").count() else page.inner_text("body")
            problems = []
            if re.search(r"Page not found|Something went wrong|Choose an organisation first", text):
                problems.append("not found or crashed")
            if len(text.strip()) < 40:
                problems.append("blank")
            problems += [f"alert: {a[:120]}" for a in alerts]
            problems += [f"API {a}" for a in api_fail]
            # A 404 is how some screens learn "not set up for you yet" (no phone
            # extension, say); the page says so in words. 5xx are caught above.
            problems += [f"console: {e}" for e in errors if "favicon" not in e and "404 (Not Found)" not in e]
            if problems:
                bad(f"{name} ({path}): " + "; ".join(problems[:4]))
            else:
                ok(f"{name} renders without errors")

        # Jump to: Ctrl+K opens the search, a tab name finds it, Enter goes there,
        # "/" opens it again and Escape closes it.
        page.goto(BASE + "/commai", wait_until="networkidle")
        page.keyboard.press("Control+k")
        dialog = page.get_by_role("dialog", name="Jump to a screen")
        try:
            dialog.wait_for(timeout=3000)
            page.keyboard.type("numbers")
            page.keyboard.press("Enter")
            page.wait_for_url(re.compile(r"/commai/voice/numbers$"), timeout=5000)
            ok("jump to (Ctrl+K) finds Phone > Numbers and opens it")
        except Exception:
            bad(f"jump to (Ctrl+K) did not open Phone > Numbers (at {page.url.split(HOST)[-1]})")
        page.locator("main").click(position={"x": 5, "y": 5})
        page.keyboard.press("/")
        if dialog.count() and dialog.is_visible():
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if dialog.count():
                bad("jump to did not close on Escape")
            else:
                ok('jump to opens with "/" and closes with Escape')
        else:
            bad('jump to did not open with "/"')

        # The example alerts show and clear from the Insights screen.
        page.goto(BASE + "/insights", wait_until="networkidle")
        show = page.get_by_role("button", name=re.compile("Show example alerts"))
        if show.count():
            show.click()
            page.wait_for_timeout(2500)
            body = page.inner_text("body").lower()  # labels are upper-cased in CSS
            if "disaster watch" in body and "hurricane watch" in body:
                ok("example hurricane and earthquake alerts appear on Insights")
            else:
                bad("example alerts did not appear on Insights")
            clear = page.get_by_role("button", name=re.compile("Clear example alerts"))
            if clear.count():
                clear.click()
                page.wait_for_timeout(1500)
        browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
