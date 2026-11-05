/**
 * The navigation scheduler of the LibreNMS sync page, and the hooks that the selection draft uses.
 *
 * Every READ that changes what the sync tabs show is a navigation: a tab link, a page, sort or Per Page
 * link, Clear filters, a filter or search request, and Back or Forward. navigate() sends its READ through
 * the page's #librenms-sync-nav loader (hx-sync="this:replace"), so a newer navigation aborts an older one.
 * The filter form keeps its own htmx trigger (the search delay and queue last on the same loader).
 * A response is accepted only while its navigation is the current one and no input came after it was
 * sent; a later input drops it and starts one new READ of its page URL after flush().
 * This script is the only writer of the page's browser history, and it handles every popstate itself.
 */
(function () {
    'use strict';

    const LOADER_ID = 'librenms-sync-nav';
    const PAGE_URL_HEADER = 'X-Sync-Page-Url';
    const NAVIGATION_LINKS = '#librenms-sync-tabs [data-sync-nav], #librenms-sync-tabs thead th > a[href^="?"]';

    let navEpoch = 0;
    let inputEpoch = 0;
    // The navigation and input epochs at which each READ was sent, by its XMLHttpRequest.
    const reads = new WeakMap();

    /**
     * Wait until the selection draft has acknowledged every input of this page.
     *
     * @returns {Promise<?number>} The last acknowledged draft revision: null while no tab writes a draft.
     */
    function flush() {
        return Promise.resolve(null);
    }

    /**
     * Count one local input, so a READ sent before it cannot replace what the user did.
     *
     * @returns {void}
     */
    function bumpInputEpoch() {
        inputEpoch += 1;
    }

    /**
     * Show the sync page at *url* in the sync tabs, after flush().
     *
     * @param {string} url - A URL of the sync page.
     * @param {'push'|'replace'|'none'} history - How an accepted response enters the browser history.
     * @returns {Promise<void>} Resolves when the READ is sent, or when a newer navigation replaced it.
     */
    function navigate(url, history) {
        navEpoch += 1;
        const epoch = navEpoch;
        const target = new URL(url, window.location.href).href;
        return flush().then(() => {
            if (epoch !== navEpoch) return;
            document.getElementById(LOADER_ID).dispatchEvent(
                new CustomEvent('librenms:navigate', { detail: { url: target, history, navEpoch: epoch } })
            );
        });
    }

    function writeHistory(url, history) {
        const target = new URL(url, window.location.href).href;
        const state = { lnmsSync: true, url: target };
        // The same URL again would only add an entry that Back shows as no change.
        if (history === 'push' && target !== window.location.href) {
            window.history.pushState(state, '', target);
        } else if (history !== 'none') {
            window.history.replaceState(state, '', target);
        }
    }

    function pageUrl(read, xhr) {
        // A filter request goes to the filter endpoint; its response names the page URL it shows.
        const url = read.url ?? xhr.getResponseHeader(PAGE_URL_HEADER);
        if (!url) throw new Error(`The filter response has no ${PAGE_URL_HEADER} header.`);
        return url;
    }

    // NetBox bundles htmx without a global, so its config is set through the meta that htmx reads on DOMContentLoaded.
    function disableHtmxHistory() {
        let meta = document.querySelector('meta[name="htmx-config"]');
        if (!meta) {
            meta = document.head.appendChild(document.createElement('meta'));
            meta.name = 'htmx-config';
            meta.content = '{}';
        }
        meta.content = JSON.stringify({ ...JSON.parse(meta.content), historyEnabled: false });
    }

    // htmx assigns window.onpopstate in its ready callback, just before the body's first htmx:load.
    function takeHistory(event) {
        if (event.target !== document.body) return;
        document.body.removeEventListener('htmx:load', takeHistory);
        if (!document.getElementById(LOADER_ID)) return;
        // Replace htmx's handler and keep no reference to it, so no history snapshot is ever installed.
        window.onpopstate = () => navigate(window.location.href, 'none');
        window.history.replaceState({ lnmsSync: true, url: window.location.href }, '', window.location.href);
    }

    disableHtmxHistory();
    document.body.addEventListener('htmx:load', takeHistory);

    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target instanceof Element ? event.target.closest(NAVIGATION_LINKS) : null;
        if (!link) return;
        event.preventDefault();
        navigate(link.href, 'push');
    });

    document.addEventListener('htmx:configRequest', function (event) {
        if (event.detail.elt.id === LOADER_ID) event.detail.path = event.detail.triggeringEvent.detail.url;
    });

    document.addEventListener('htmx:beforeRequest', function (event) {
        const { elt, xhr } = event.detail;
        if (elt.id === LOADER_ID) {
            const { url, history, navEpoch: epoch } = event.detail.requestConfig.triggeringEvent.detail;
            reads.set(xhr, { navEpoch: epoch, inputEpoch, url, history });
        } else if (elt.matches('.sync-filter-form')) {
            // A filter request that htmx sends, also one it queued, is the newest navigation.
            navEpoch += 1;
            reads.set(xhr, { navEpoch, inputEpoch, url: null, history: 'push' });
        }
    });

    // The gate runs before htmx reads any header, out-of-band content or swap of the response.
    document.addEventListener('htmx:beforeOnLoad', function (event) {
        const read = reads.get(event.detail.xhr);
        if (!read) return;
        if (read.navEpoch !== navEpoch) {
            event.preventDefault();
        } else if (read.inputEpoch !== inputEpoch) {
            event.preventDefault();
            navigate(pageUrl(read, event.detail.xhr), read.history);
        } else {
            read.accepted = true;
        }
    });

    // htmx writes its history before the swap too, so the swapped content reads the new URL.
    document.addEventListener('htmx:beforeSwap', function (event) {
        const read = reads.get(event.detail.xhr);
        if (!read?.accepted || event.defaultPrevented || !event.detail.shouldSwap) return;
        writeHistory(pageUrl(read, event.detail.xhr), read.history);
    });

    window.librenmsSyncDraft = Object.freeze({ navigate, flush, bumpInputEpoch });
})();
