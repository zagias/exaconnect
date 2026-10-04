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
BASE = f"https://{HOST}"
SCREENS = [
    ("/", "Overview"),
    ("/sites", "Sites"),
    ("/traffic/applications", "Traffic: seen on your network"),
    ("/traffic/rules", "Traffic: rules"),
    ("/traffic/classes", "Traffic: classes"),
    ("/decisions", "Decisions"),
    ("/fabric", "Fabric"),
    ("/internet", "Internet"),
    ("/order", "Order"),
    ("/metering", "Metering"),
    ("/insights", "Insights"),
    ("/ask", "Ask your network"),
    ("/carrier", "Carrier view"),
    ("/account", "Account"),
    ("/admin/agents", "Admin: agents"),
    ("/admin/sites", "Admin: sites"),
    ("/admin/classes", "Admin: classes"),
    ("/admin/users", "Admin: users"),
    ("/admin/settings", "Admin: settings"),
    ("/admin/partners", "Admin: partners"),
    ("/admin/audit", "Admin: audit"),
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

        page.goto(BASE + "/", wait_until="networkidle")
        page.fill('input[type="email"]', os.environ["E2E_EMAIL"])
        page.fill('input[type="password"]', os.environ["E2E_PASSWORD"])
        page.click('button[type="submit"]')
        try:
            page.wait_for_selector("nav", timeout=15000)
            ok("signed in through the browser")
        except Exception:
            bad("sign-in did not reach the portal")
            return 1

        for path, name in SCREENS:
            errors.clear()
            api_fail.clear()
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(1500)
            alerts = [a.strip() for a in page.locator('[role="alert"]').all_inner_texts() if a.strip()]
            text = page.locator("main").inner_text() if page.locator("main").count() else page.inner_text("body")
            problems = []
            if re.search(r"Page not found|Something went wrong", text):
                problems.append("not found or crashed")
            if len(text.strip()) < 40:
                problems.append("blank")
            problems += [f"alert: {a[:120]}" for a in alerts]
            problems += [f"API {a}" for a in api_fail]
            problems += [f"console: {e}" for e in errors if "favicon" not in e]
            if problems:
                bad(f"{name} ({path}): " + "; ".join(problems[:4]))
            else:
                ok(f"{name} renders without errors")

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
