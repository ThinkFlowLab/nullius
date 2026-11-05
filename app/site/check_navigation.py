"""Browser regression check against a running documentation preview."""
import argparse
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright


def check(url):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width in (1440, 390):
                page = browser.new_page(viewport={'width': width, 'height': 800})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(urljoin(url.rstrip('/') + '/', 'getting-started/'))
                # Wait for Material's initial sidebar layout and sitemap load.
                page.wait_for_timeout(500)
                page.evaluate('window.navigationCheck = true')
                nav = page.locator('.md-nav--primary')
                if width == 1440:
                    rail = page.locator('.md-sidebar--primary .md-sidebar__scrollwrap')
                    for title in ('Supported capabilities', 'Supported agents', 'Supported benchmarks'):
                        link = nav.get_by_role('link', name=title, exact=True)
                        link.scroll_into_view_if_needed()
                        before = rail.evaluate('(el) => el.scrollTop')
                        assert before > 0, 'Test must exercise the scrolled sidebar'
                        link.click()
                        expect(page.locator('.md-content h1')).to_contain_text(title)
                        # Include delayed layout/centering, not just the click frame.
                        page.wait_for_timeout(500)
                        after, maximum = rail.evaluate('(el) => [el.scrollTop, el.scrollHeight - el.clientHeight]')
                        assert abs(after - min(before, maximum)) <= 1, (title, before, after)
                        assert page.evaluate('window.navigationCheck'), 'Full document reload'
                else:
                    page.locator('label.md-header__button[for="__drawer"]').click()
                    nav.get_by_role('link', name='Core concepts', exact=True).click()
                    expect(page.locator('.md-content h1')).to_contain_text('Core concepts')
                    expect(page.locator('#__drawer')).not_to_be_checked()
                    assert page.evaluate('window.navigationCheck'), 'Full document reload'
                assert not errors, errors
                print(f'PASS: navigation at {width}px')
                page.close()
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8793/')
    check(parser.parse_args().url)
