"""
Browser checks of the sync page navigation scheduler (sync_draft.js) on a page shaped like a NetBox page.

HTMX loads in the head without a global, and the plugin scripts end the body. The tab links and the
paginator are the real templates. The test server renders the page for each query and can hold its
READs, so a test decides the order in which the responses arrive.
"""

from html import escape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from test_sync_cache_browser import TEMPLATE_DIR, _navigation_loader, _serve_page_scripts, _sync_document
from test_sync_filters_browser import (
    PAGE_URL,
    TABLE,
    _answer,
    _filter_page,
    _filter_tab,
    _next_request,
    _route_query,
    _serve_filter_tab,
)

PAGE_PATH = urlsplit(PAGE_URL).path
TABS = ("interfaces", "cables", "ipaddresses")
OTHER_PAGE_URL = "https://plugin.example.com/dcim/devices/2/"


def _engine():
    from django.conf import settings
    from django.template import Engine, Library
    from django.template.library import import_library

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    sync_query = Library()

    @sync_query.simple_tag(name="sync_tab_url")
    def sync_tab_url(prefix="", tab="", **changes):
        return f"?tab={tab}&" + "&".join(f"{prefix}{key}={value}" for key, value in sorted(changes.items()))

    engine = Engine(dirs=[str(TEMPLATE_DIR.parent)])
    engine.template_libraries["librenms_sync_query"] = sync_query
    engine.template_libraries["i18n"] = import_library("django.templatetags.i18n")
    return engine


def _page_body(query):
    """Render the sync tabs for *query*: the real tab links and paginator, a NetBox sort link and an object link."""
    from django.template import Context

    tab = query.get("tab", "interfaces")
    engine = _engine()
    tab_nav = engine.get_template("netbox_librenms_plugin/inc/_sync_tab_nav.html").render(
        Context(
            {
                "active_sync_tab": tab,
                "sync_tab_urls": {name: f"{PAGE_PATH}?tab={name}" for name in TABS},
                "sync_cache_status": {},
                "interface_name_field": None,
            }
        )
    )
    table = SimpleNamespace(
        prefix=f"{tab}_",
        tab=tab,
        page=SimpleNamespace(
            has_previous=False,
            has_next=True,
            next_page_number=2,
            smart_pages=[1, 2],
            number=1,
            start_index=1,
            end_index=1,
        ),
        paginator=SimpleNamespace(num_pages=2, per_page=25, count=2, get_page_lengths=[25, 50]),
    )
    paginator = engine.get_template("netbox_librenms_plugin/inc/paginator.html").render(Context({"table": table}))
    shown = "&".join(f"{key}={value}" for key, value in sorted(query.items()))
    return f"""{_navigation_loader()}
        <div id="librenms-sync-tabs" data-active-tab="{tab}">
          <ul id="librenmsSync">{tab_nav}</ul>
          <output id="shown">{escape(shown)}</output>
          {paginator}
          <table><thead><tr><th><a id="sort-name" href="?tab={tab}&amp;{tab}_sort=name">Name</a></th></tr></thead></table>
          <a id="other-object" href="{OTHER_PAGE_URL}">Another device</a>
        </div>"""


def _query(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


class _SyncPage:
    """Serve the sync page for each query; hold its READs while ``hold`` is set."""

    def __init__(self, page):
        self.page = page
        self.hold = False
        self.held = []
        self.reads = []
        self.loads = []
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        _serve_page_scripts(page)
        page.route(f"{PAGE_URL}**", self._serve)
        page.route(
            OTHER_PAGE_URL, lambda route: route.fulfill(status=200, content_type="text/html", body="<p>other</p>")
        )

    def _serve(self, route):
        if route.request.headers.get("hx-request") == "true":
            self.reads.append(route.request.url)
            if self.hold:
                self.held.append(route)
                return
        else:
            self.loads.append(route.request.url)
        answer(route)

    def open(self, query="tab=interfaces"):
        self.page.goto(f"{PAGE_URL}?{query}")
        # The scheduler takes the history at the body's first htmx:load, which comes after DOMContentLoaded.
        self.page.wait_for_function("history.state && history.state.lnmsSync === true")

    def next_held(self, count):
        return _next_request(self.page, self.held, count)


def answer(route):
    route.fulfill(status=200, content_type="text/html", body=_sync_document(_page_body(_query(route.request.url))))


def answer_aborted(route):
    """Answer a READ that a newer navigation aborted: the browser has dropped it, so nothing can arrive."""
    answer(route)


def _shown(page, query):
    expect(page.locator("#shown")).to_have_text(query)


def _history_length(page):
    return page.evaluate("history.length")


def test_a_tab_link_loads_the_tab_in_place_and_adds_one_history_entry(page):
    server = _SyncPage(page)
    server.open()
    entries = _history_length(page)
    assert page.evaluate("htmxTest.config.historyEnabled") is False

    page.click("#cables-tab")

    _shown(page, "tab=cables")
    assert server.reads == [f"{PAGE_URL}?tab=cables"]
    assert server.loads == [f"{PAGE_URL}?tab=interfaces"]
    assert page.url == f"{PAGE_URL}?tab=cables"
    assert _history_length(page) == entries + 1
    assert page.evaluate("history.state") == {"lnmsSync": True, "url": f"{PAGE_URL}?tab=cables"}
    expect(page.locator("#cables-tab")).to_have_class("nav-link active")
    assert server.errors == []


def test_page_sort_and_per_page_links_load_in_place_and_other_links_leave_the_page(page):
    server = _SyncPage(page)
    server.open()

    page.click(".pagination a.page-link >> text=2")
    _shown(page, "interfaces_page=2&interfaces_per_page=25&tab=interfaces")
    page.click("#sort-name")
    _shown(page, "interfaces_sort=name&tab=interfaces")
    page.click(".dropdown-menu a.dropdown-item >> text=50")
    _shown(page, "interfaces_page=1&interfaces_per_page=50&tab=interfaces")

    assert len(server.reads) == 3 and len(server.loads) == 1
    assert _query(page.url) == {"tab": "interfaces", "interfaces_per_page": "50", "interfaces_page": "1"}
    page.click("#other-object")
    page.wait_for_url(OTHER_PAGE_URL)


def test_a_newer_navigation_wins_over_a_delayed_read(page):
    server = _SyncPage(page)
    server.open()
    entries = _history_length(page)
    server.hold = True

    page.click("#cables-tab")
    older = server.next_held(1)
    page.click("#ipaddresses-tab")
    newer = server.next_held(2)
    answer_aborted(older)
    page.wait_for_timeout(300)
    _shown(page, "tab=interfaces")
    answer(newer)

    _shown(page, "tab=ipaddresses")
    assert page.url == f"{PAGE_URL}?tab=ipaddresses"
    assert _history_length(page) == entries + 1


def test_a_delayed_back_then_a_tab_switch_shows_the_tab(page):
    server = _SyncPage(page)
    server.open()
    page.click("#cables-tab")
    _shown(page, "tab=cables")
    entries = _history_length(page)
    server.hold = True

    page.go_back()
    back = server.next_held(1)
    assert back.request.url == f"{PAGE_URL}?tab=interfaces"
    page.click("#ipaddresses-tab")
    tab = server.next_held(2)
    answer_aborted(back)
    answer(tab)

    _shown(page, "tab=ipaddresses")
    assert page.url == f"{PAGE_URL}?tab=ipaddresses"
    # The tab replaced the forward entry that Back left.
    assert _history_length(page) == entries
    assert page.evaluate("history.state.url") == f"{PAGE_URL}?tab=ipaddresses"


def test_two_rapid_backs_move_one_entry_each_and_the_last_wins(page):
    server = _SyncPage(page)
    server.open()
    for tab in ("cables", "ipaddresses"):
        page.click(f"#{tab}-tab")
        _shown(page, f"tab={tab}")
    entries = _history_length(page)
    server.hold = True

    page.go_back()
    first = server.next_held(1)
    page.go_back()
    second = server.next_held(2)
    answer_aborted(first)
    answer(second)

    _shown(page, "tab=interfaces")
    assert page.url == f"{PAGE_URL}?tab=interfaces"
    assert _history_length(page) == entries
    assert [_query(url)["tab"] for url in server.reads[-2:]] == ["cables", "interfaces"]
    server.hold = False
    page.go_forward()
    _shown(page, "tab=cables")


def test_a_legacy_htmx_entry_with_a_cached_snapshot_is_never_installed(page):
    server = _SyncPage(page)
    server.open("tab=cables")
    page.evaluate(
        """url => {
            window.htmxRestores = 0;
            document.body.addEventListener('htmx:historyRestore', () => { window.htmxRestores += 1; });
            // Entries and a snapshot that htmx wrote before the scheduler owned the history.
            const stale = '<div id="librenms-sync-tabs"><output id="shown">stale snapshot</output></div>';
            sessionStorage.setItem('htmx-history-cache', JSON.stringify([
                {url: url + '?tab=interfaces', content: stale, title: '', scroll: 0},
            ]));
            history.replaceState({htmx: true}, '', url + '?tab=interfaces');
            history.pushState({htmx: true}, '', url + '?tab=cables');
        }""",
        PAGE_PATH,
    )

    page.go_back()

    _shown(page, "tab=interfaces")
    assert server.reads == [f"{PAGE_URL}?tab=interfaces"]
    assert page.evaluate("window.htmxRestores") == 0


def test_an_input_after_a_read_was_sent_drops_it_and_reads_the_current_page_once(page):
    server = _SyncPage(page)
    server.open()
    entries = _history_length(page)
    server.hold = True

    page.click("#cables-tab")
    stale = server.next_held(1)
    page.evaluate("librenmsSyncDraft.bumpInputEpoch()")
    answer(stale)
    again = server.next_held(2)

    _shown(page, "tab=interfaces")
    assert again.request.url == f"{PAGE_URL}?tab=cables"
    answer(again)
    _shown(page, "tab=cables")
    page.wait_for_timeout(300)
    assert len(server.reads) == 2
    assert _history_length(page) == entries + 1


def test_flush_resolves_at_once_while_no_tab_writes_a_draft(page):
    _SyncPage(page).open()

    assert page.evaluate("librenmsSyncDraft.flush()") is None


def test_one_search_adds_one_history_entry_with_the_page_url(page):
    held = _serve_filter_tab(page)
    page.wait_for_function("history.state && history.state.lnmsSync === true")
    entries = _history_length(page)

    page.fill("#id_interfaces_q", "eth")
    route = _next_request(page, held, 1)
    _answer(route)

    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    page_url = f"{PAGE_URL}?{urlsplit(route.request.url).query}"
    assert page.url == page_url
    assert page.evaluate("history.state") == {"lnmsSync": True, "url": page_url}
    assert _history_length(page) == entries + 1


def test_a_search_response_after_a_later_input_reads_its_page_url_instead(page):
    held = _serve_filter_tab(page)
    reads = []
    page.on("request", lambda request: reads.append(request.url) if request.url.startswith(PAGE_URL) else None)
    page.fill("#id_interfaces_q", "eth")
    route = _next_request(page, held, 1)
    page.evaluate("librenmsSyncDraft.bumpInputEpoch()")

    _answer(route)

    # The page READ renders the tab for the page URL that the filter response named.
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    assert reads == [f"{PAGE_URL}?{urlsplit(route.request.url).query}"]
    assert len(held) == 1


def test_a_tab_switch_during_a_search_wins(page):
    held = _serve_filter_tab(page)
    page.evaluate(
        """() => document.getElementById('librenms-sync-tabs').insertAdjacentHTML(
            'afterbegin', '<a id="go-cables" data-sync-nav href="?tab=cables&amp;server_key=primary">Cables</a>')"""
    )
    page.fill("#id_interfaces_q", "eth")
    search = _next_request(page, held, 1)

    page.click("#go-cables")
    page.wait_for_url(f"{PAGE_URL}?tab=cables&server_key=primary")
    _answer(search)
    page.wait_for_timeout(300)

    assert page.url == f"{PAGE_URL}?tab=cables&server_key=primary"
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "")


PAGE_TWO = f"{PAGE_URL}?tab=interfaces&server_key=primary&interfaces_page=2"


def _hold_page_two(page):
    """Add a page link to the filter tab, and hold its READ until the test answers it."""
    page.evaluate(
        """() => document.getElementById('librenms-sync-tabs').insertAdjacentHTML(
            'afterbegin',
            '<a id="go-page-two" data-sync-nav '
            + 'href="?tab=interfaces&amp;server_key=primary&amp;interfaces_page=2">2</a>')"""
    )
    reads = []
    page.route(PAGE_TWO, lambda route: reads.append(route))
    return reads


def _answer_page(route):
    route.fulfill(status=200, content_type="text/html", body=_filter_page(_route_query(route)))


def test_a_page_answer_that_comes_while_a_search_waits_for_its_delay_does_not_drop_the_search(page):
    held = _serve_filter_tab(page)
    reads = _hold_page_two(page)
    page.click("#go-page-two")
    for _ in range(100):
        if reads:
            break
        page.wait_for_timeout(20)
    search = page.locator("#id_interfaces_q")

    # The page answer comes right after the typing, while the search still waits for its delay.
    search.press_sequentially("eth")
    _answer_page(reads[0])

    route = _next_request(page, held, 1)
    assert parse_qs(urlsplit(route.request.url).query)["interfaces_q"] == ["eth"]
    _answer(route)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    assert parse_qs(urlsplit(page.url).query)["interfaces_q"] == ["eth"]
    expect(search).to_have_value("eth")


def test_a_navigation_after_the_typing_wins_over_the_search_that_waits_for_its_delay(page):
    held = _serve_filter_tab(page)
    reads = _hold_page_two(page)

    page.locator("#id_interfaces_q").press_sequentially("eth")
    page.click("#go-page-two")
    # The search's delay ends while the page read waits.
    page.wait_for_timeout(800)
    for _ in range(100):
        if reads:
            break
        page.wait_for_timeout(20)
    _answer_page(reads[0])

    page.wait_for_url(PAGE_TWO)
    page.wait_for_timeout(300)
    assert held == []
    expect(page.locator("#id_interfaces_q")).to_have_value("")


FRAGMENT_URL = "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-cache-fragment/interfaces/"
FRAGMENT_LOADER = (
    f'<span id="fragment-loader" class="d-none" data-fragment-loader hx-get="{FRAGMENT_URL}"'
    ' hx-trigger="librenms:load-fragment" hx-target="#interface-sync-content" hx-swap="innerHTML"></span>'
)


def test_a_fragment_answer_that_comes_while_a_search_waits_for_its_delay_does_not_drop_the_search(page):
    held = _serve_filter_tab(page, extra=FRAGMENT_LOADER)
    fragments = []
    page.route(f"{FRAGMENT_URL}**", lambda route: fragments.append(route))
    # The tab reloads its fragment, as after a data refresh, and the user types meanwhile.
    page.evaluate("document.getElementById('fragment-loader').dispatchEvent(new CustomEvent('librenms:load-fragment'))")
    for _ in range(100):
        if fragments:
            break
        page.wait_for_timeout(20)
    search = page.locator("#id_interfaces_q")

    search.press_sequentially("eth")
    fragments[0].fulfill(status=200, content_type="text/html", body=_filter_tab({}))

    route = _next_request(page, held, 1)
    assert parse_qs(urlsplit(route.request.url).query)["interfaces_q"] == ["eth"]
    _answer(route)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(search).to_have_value("eth")
