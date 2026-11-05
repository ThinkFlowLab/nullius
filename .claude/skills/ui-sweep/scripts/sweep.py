"""Click through the run builder and the catalogue, and keep a screenshot of each.

    uv run --with playwright python .claude/skills/ui-sweep/scripts/sweep.py \
        --url http://127.0.0.1:5000 --out <dir> [--browser <chromium>] [--only <benchmark key>]

Every benchmark the builder offers, on both runners, on every step that has a
choice, light and dark; the Agent step again at phone width with every
reference setup and the first agent or capability that fits chosen, each
settings tab opened. Then the Benchmarks, Agents and Capabilities pages. Nothing
is named here: the benchmarks are read from the page, so a new one is swept
too. It starts the builder over first (Start over), so a draft in this
browser profile is not left as it was; it writes only under --out.

Writes <out>/<theme>/<runner>-<benchmark>-<step>.png and <out>/report.json:
for each screenshot, the page's red lines, its JavaScript errors and every
card or heading whose text is cut (wider than its box) - the things the
rules in .claude/rules/ui.md forbid that a look can miss.
Exits with status 1 when a page error or cut text is found.
"""
import argparse
import json
import os
import re

from playwright.sync_api import sync_playwright

SHOWN = ", ".join(["#nullius-builder .nb-tile strong", "#nullius-builder h2", "#nullius-builder h3",
                   "#nullius-builder .nb-tile-line", "#nullius-builder .nb-subtab", ".bench-card strong",
                   ".bench-card p", ".std-card p"])
CUT = """() => [...document.querySelectorAll('""" + SHOWN + """')]
  .filter(e => e.offsetParent && (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1))
  .map(e => e.textContent.trim().slice(0, 80))"""


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "x"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True, help="the running app, e.g. http://127.0.0.1:5000")
    ap.add_argument("--out", required=True, help="where screenshots and report.json go")
    ap.add_argument("--browser", default=os.environ.get("UI_SWEEP_CHROMIUM") or None,
                    help="a Chromium executable, when Playwright's own is not installed")
    ap.add_argument("--only", default="", help="sweep this benchmark key alone")
    a = ap.parse_args()
    report = []

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": a.browser} if a.browser else {}))
        for theme in ("dark", "light"):
            pg = browser.new_page(viewport={"width": 1400, "height": 1000}, color_scheme=theme)
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))

            def shot(name):
                path = os.path.join(a.out, theme, name + ".png")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                pg.wait_for_timeout(250)
                pg.screenshot(path=path, full_page=True)
                report.append({"shot": os.path.relpath(path, a.out), "errors": list(errors),
                               "red": [t for t in pg.locator("#nullius-builder .nb-error").all_inner_texts() if t.strip()],
                               "cut": pg.evaluate(CUT)})
                errors.clear()

            def step(label):
                pg.locator("[data-step]").filter(has_text=re.compile(rf"^\s*\d+\s*{label}\s*$")).first.click()
                pg.wait_for_timeout(300)

            def steps():
                return [re.sub(r"^\s*\d+\s*", "", t).strip() for t in pg.locator("[data-step].nb-step").all_inner_texts()]

            pg.goto(a.url.rstrip("/") + "/new-run", wait_until="networkidle")
            skip = pg.get_by_role("button", name="No, I'll explore")
            if skip.count():
                skip.first.click()
            pg.locator("#nb-startover").click()
            pg.wait_for_timeout(300)

            for runner in range(pg.locator("[data-runner]").count()):
                step(steps()[0])
                pg.locator("[data-runner]").nth(runner).click()
                pg.wait_for_timeout(300)
                rname = slug(pg.locator("[data-runner]").nth(runner).inner_text().split("\n")[0])
                shot(f"{rname}-runner")
                step(steps()[1])
                attr = "data-std-bench" if pg.locator("[data-std-bench]").count() else "data-benchmark"
                keys = [k for k in (e.get_attribute(attr) for e in pg.locator(f"[{attr}]").all()) if k and k != "custom"]
                shot(f"{rname}-benchmark")
                for key in keys:
                    if a.only and key != a.only:
                        continue
                    step(steps()[1])
                    pg.locator(f'[{attr}="{key}"]').first.click()
                    pg.wait_for_timeout(300)
                    for label in steps()[2:]:
                        step(label)
                        if label not in ("Review",) and attr == "data-std-bench" and label == steps()[3]:
                            # who runs it: every reference setup on, then each settings tab
                            off = pg.locator('[data-cat="reference"][aria-pressed="false"]')
                            while off.count():                # each click redraws the step: find the next again
                                off.first.click()
                                pg.wait_for_timeout(250)
                            tabs = pg.locator("[data-set-tab]")
                            for i in range(tabs.count()):
                                pg.locator("[data-set-tab]").nth(i).click()
                                pg.wait_for_timeout(250)
                                shot(f"{rname}-{key}-{slug(label)}-tab{i + 1}")
                            if not tabs.count():
                                shot(f"{rname}-{key}-{slug(label)}")
                            if theme == "dark":
                                pg.set_viewport_size({"width": 390, "height": 900})
                                shot(f"{rname}-{key}-{slug(label)}-phone")
                                pg.set_viewport_size({"width": 1400, "height": 1000})
                        else:
                            shot(f"{rname}-{key}-{slug(label)}")
            for page in ("benchmarks", "agents", "capabilities"):
                pg.goto(a.url.rstrip("/") + "/" + page, wait_until="networkidle")
                shot(f"page-{page}")
            pg.close()
        browser.close()

    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    bad = [r for r in report if r["errors"] or r["cut"]]
    print(f"{len(report)} screenshots in {a.out}; {len(bad)} with a page error or cut text")
    for r in bad:
        print(" ", r["shot"], "errors:", r["errors"][:2], "cut:", r["cut"][:4])
    return int(bool(bad))


if __name__ == "__main__":
    raise SystemExit(main())
