/**
 * The navigation scheduler of the LibreNMS sync page, and the writer of the server-side selection draft.
 *
 * Every READ that changes what the sync tabs show is a navigation: a tab link, a page, sort or Per Page
 * link, Clear filters, a filter or search request, and Back or Forward. navigate() sends its READ through
 * the page's #librenms-sync-nav loader (hx-sync="this:replace"), so a newer navigation aborts an older one.
 * The filter form keeps its own htmx trigger (the search delay and queue last on the same loader).
 * A response is accepted only while its navigation is the current one and no input came after it was
 * sent; a later input drops it and starts one new READ of its page URL after flush().
 * This script is the only writer of the page's browser history, and it handles every popstate itself.
 *
 * A converted table (table[data-draft-id]) keeps its selection in the user's draft on the server. A
 * checkbox paints at once; the writer sends the new row states in one batch per 100 ms of input, and one
 * command at a time, each with the draft revision of the last acknowledgement. A row's choice is a
 * command too, after which the row is read again (a row READ, which is not a navigation). Every request
 * that reads the draft waits for flush(), so it never shows a state from before an input of this page.
 */
(function () {
    'use strict';

    const LOADER_ID = 'librenms-sync-nav';
    const PAGE_URL_HEADER = 'X-Sync-Page-Url';
    const NAVIGATION_LINKS = '#librenms-sync-tabs [data-sync-nav], #librenms-sync-tabs thead th > a[href^="?"]';
    const DRAFT_TABLE = 'table[data-draft-id]';
    const ROW_CHECKBOX = 'td input[type="checkbox"][name="select"]';
    const REVISION_HEADER = 'X-Draft-Revision';
    const ROW_READ_EVENT = 'librenms:row-read';
    const ROW_STALE_EVENT = 'lnms-row-stale';
    const RESULTS_STALE_EVENT = 'lnms-results-stale';
    const NOTICE_ID = 'librenms-draft-notice';
    const BATCH_DELAY_MS = 100;
    const CONFLICT_NOTICE_MS = 10000;

    let navEpoch = 0;
    let inputEpoch = 0;
    // The navigation that waits for the writer or for its READ; it shows the draft, so nothing else reads it.
    let pendingNav = null;
    // The page URL that the last navigation shows, or will show when its READ is accepted.
    let destination = window.location.href;
    // The navigation and input epochs at which each READ was sent, by its XMLHttpRequest.
    const reads = new WeakMap();

    // The draft writer of this document: one writer, so its acknowledgements give the next expected revision.
    const writer = {
        // The table whose presentation the writer took its draft from.
        table: null,
        draftId: null,
        revision: null,
        urls: {},
        // The row states that no batch carries yet, by row key; a newer state of a row replaces the older.
        dirty: new Map(),
        // The commands that wait for the one in flight, in input order.
        commands: [],
        inFlight: false,
        timer: null,
        // A write failed: the input stays here, and nothing reads the draft until a retry succeeds.
        failed: false,
        // Another window changed the draft: input waits for the fresh presentation, which this page reads.
        stale: false,
        // The sync form whose bulk submit is the command in flight.
        submitting: null,
        waiters: [],
        // The last choice this page sent per row, until a row READ shows the row again.
        choices: new Map(),
        // The rows to read again once the writer is idle.
        rowReads: new Set(),
    };

    function idle() {
        return !writer.dirty.size && !writer.commands.length && !writer.inFlight;
    }

    /**
     * Wait until the selection draft has acknowledged every input of this page.
     *
     * A failed write is tried again here. A conflict (another window changed the draft) or a new failure
     * rejects, and the page then shows the draft again or keeps the failure notice.
     *
     * @returns {Promise<?number>} The last acknowledged draft revision: null while the page has no draft table.
     */
    function flush() {
        return new Promise((resolve, reject) => {
            writer.waiters.push({ resolve, reject });
            if (writer.failed) {
                writer.failed = false;
                hideNotice();
            }
            clearTimeout(writer.timer);
            writer.timer = null;
            pump();
        });
    }

    function settleWaiters(error) {
        const waiters = writer.waiters.splice(0);
        waiters.forEach((waiter) => (error ? waiter.reject(error) : waiter.resolve(writer.revision)));
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
     * @returns {Promise<void>} Resolves when the READ is sent, or when a newer navigation or a refused flush stops it.
     */
    function navigate(url, history) {
        const epoch = reserveNavigation();
        const target = new URL(url, window.location.href).href;
        destination = target;
        return flush().then(() => {
            if (epoch !== navEpoch) return;
            document.getElementById(LOADER_ID).dispatchEvent(
                new CustomEvent('librenms:navigate', { detail: { url: target, history, navEpoch: epoch } })
            );
        }, () => endNavigation(epoch));
    }

    // A navigation takes its identity when the user starts it, so a later one wins also while both wait.
    function reserveNavigation() {
        navEpoch += 1;
        pendingNav = navEpoch;
        return navEpoch;
    }

    function endNavigation(epoch) {
        if (pendingNav === epoch) pendingNav = null;
    }

    function writeHistory(url, history) {
        const target = new URL(url, window.location.href).href;
        const state = { lnmsSync: true, url: target };
        destination = target;
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

    // ============================================
    // THE DRAFT WRITER
    // ============================================

    function draftTable() {
        return document.querySelector(DRAFT_TABLE);
    }

    function csrfToken() {
        return document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
    }

    /**
     * Take the draft of a new presentation of the draft table: its draft ID, revision and command URLs.
     *
     * Only an idle writer takes it: an accepted READ was sent after flush() and no input came after it. A
     * table the writer took before keeps the writer's own revision, which later acknowledgements moved on.
     *
     * @returns {void}
     */
    function adoptPresentation() {
        const table = draftTable();
        if (!table || table === writer.table || !idle()) return;
        writer.table = table;
        writer.draftId = table.dataset.draftId;
        writer.revision = Number(table.dataset.draftRevision);
        writer.urls = {
            selection: table.dataset.selectionUrl,
            choice: table.dataset.choiceUrl,
            management: table.dataset.managementUrl,
        };
        writer.stale = false;
        writer.choices.clear();
    }

    function rowByKey(key) {
        // Compare the keys instead of building a selector: no row key may reach a selector.
        return Array.from(draftTable()?.querySelectorAll('tr[data-row-key]') || []).find((row) => row.dataset.rowKey === key);
    }

    /**
     * Read one row of the draft table again, once the writer is idle; a row on no shown page is not read.
     *
     * @param {string} key - The row key.
     * @returns {void}
     */
    function readRow(key) {
        writer.rowReads.add(key);
        flush().then(() => {
            if (!writer.rowReads.delete(key)) return;
            rowByKey(key)?.dispatchEvent(new CustomEvent(ROW_READ_EVENT));
        }, () => writer.rowReads.delete(key));
    }

    // A navigation that waits shows the draft with its own history entry, so it is the fresh READ.
    function readResults() {
        if (pendingNav !== null) return;
        navigate(destination, 'replace');
    }

    function schedule() {
        if (writer.timer === null) {
            writer.timer = setTimeout(() => {
                writer.timer = null;
                pump();
            }, BATCH_DELAY_MS);
        }
    }

    /**
     * Send the next write: the batch of row states first (so a later command never overtakes a click), then the queued commands.
     *
     * @returns {void}
     */
    function pump() {
        if (writer.inFlight || writer.failed || writer.stale) {
            if (writer.stale && idle()) settleWaiters();
            return;
        }
        if (writer.dirty.size && (writer.timer === null || writer.commands.length || writer.waiters.length)) {
            const batch = new Map(writer.dirty);
            writer.dirty.clear();
            send({
                url: writer.urls.selection,
                fields: { assignments: JSON.stringify(Object.fromEntries(batch)) },
                batch,
            });
        } else if (!writer.dirty.size && writer.commands.length) {
            send(writer.commands.shift());
        } else if (idle()) {
            settleWaiters();
        }
    }

    function send(command) {
        const body = new FormData();
        body.set('draft_id', writer.draftId);
        body.set('expected_revision', String(writer.revision));
        Object.entries(command.fields).forEach(([name, value]) => body.set(name, value));
        writer.inFlight = true;
        fetch(command.url, {
            method: 'POST',
            body,
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((response) => {
                if (response.status === 204) {
                    acknowledge(command, response);
                } else if (response.status === 409) {
                    conflict();
                } else {
                    failure(command);
                }
            }, () => failure(command))
            .finally(() => {
                writer.inFlight = false;
                pump();
            });
    }

    function acknowledge(command, response) {
        writer.revision = Number(response.headers.get(REVISION_HEADER));
        let events = {};
        try {
            events = JSON.parse(response.headers.get('HX-Trigger') || '{}');
        } catch (_) {
            events = {};
        }
        if (events[ROW_STALE_EVENT]) readRow(events[ROW_STALE_EVENT].key);
        if (Object.hasOwn(events, RESULTS_STALE_EVENT)) readResults();
    }

    /**
     * Another window changed the draft: drop this page's unsent input and show the draft again.
     *
     * @returns {void}
     */
    function conflict() {
        writer.stale = true;
        writer.dirty.clear();
        writer.commands = [];
        clearTimeout(writer.timer);
        writer.timer = null;
        settleWaiters(new Error('The selection changed in another window.'));
        showNotice('The selection changed in another window. The tab now shows it; select again.', null);
        readResults();
    }

    function failure(command) {
        // The input stays for the retry; a newer state of a row that the user set meanwhile wins.
        if (command.batch) {
            command.batch.forEach((state, key) => {
                if (!writer.dirty.has(key)) writer.dirty.set(key, state);
            });
        } else {
            writer.commands.unshift(command);
        }
        writer.failed = true;
        settleWaiters(new Error('The selection is not saved.'));
        showNotice('The selection is not saved, so the tab cannot change until it is.', retry);
    }

    function retry() {
        writer.failed = false;
        hideNotice();
        pump();
    }

    function showNotice(text, action) {
        let notice = document.getElementById(NOTICE_ID);
        if (!notice) {
            notice = document.createElement('div');
            notice.id = NOTICE_ID;
            notice.className = 'alert alert-warning shadow-sm position-fixed top-0 start-50 translate-middle-x mt-2';
            notice.style.zIndex = '1090';
            notice.setAttribute('role', 'alert');
            document.body.appendChild(notice);
        }
        clearTimeout(notice._hideTimer);
        notice.replaceChildren(document.createTextNode(text));
        notice.dataset.draftNotice = action ? 'failed' : 'conflict';
        if (action) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm btn-warning ms-2';
            button.textContent = 'Try again';
            button.addEventListener('click', action);
            notice.appendChild(button);
        } else {
            notice._hideTimer = setTimeout(hideNotice, CONFLICT_NOTICE_MS);
        }
    }

    function hideNotice() {
        document.getElementById(NOTICE_ID)?.remove();
    }

    function setRowState(checkbox) {
        const row = checkbox.closest('tr[data-row-key]');
        if (!row) return;
        const checked = checkbox.checked;
        writer.dirty.set(row.dataset.rowKey, { state: checked ? 'on' : 'none', token: checked ? row.dataset.evidence : null });
    }

    function selectableBoxes(table) {
        return Array.from(table.querySelectorAll(ROW_CHECKBOX)).filter((box) => !box.disabled);
    }

    /**
     * Take one user input on a draft table, or ignore it while the page waits for the fresh presentation.
     *
     * @param {HTMLInputElement[]} boxes - The row checkboxes the input set.
     * @returns {void}
     */
    function takeInput(boxes) {
        if (writer.stale || !writer.draftId) return;
        bumpInputEpoch();
        boxes.forEach(setRowState);
        schedule();
        // A READ that waits for the writer gets the input at once.
        if (writer.waiters.length) pump();
    }

    function enqueue(command) {
        if (writer.stale || !writer.draftId) return;
        bumpInputEpoch();
        // A queued choice of the same row is replaced: the last choice is the one that counts.
        const queued = command.row ? writer.commands.find((other) => other.row === command.row) : null;
        if (queued) {
            queued.fields = command.fields;
        } else {
            writer.commands.push(command);
        }
        pump();
    }

    document.addEventListener('change', function (event) {
        const control = event.target;
        if (!(control instanceof Element)) return;
        const table = control.closest(DRAFT_TABLE);
        if (table && control.matches('th input.toggle')) {
            const boxes = selectableBoxes(table).filter((box) => box.checked !== control.checked);
            boxes.forEach((box) => {
                box.checked = control.checked;
            });
            takeInput(boxes);
        } else if (table && control.matches(ROW_CHECKBOX)) {
            takeInput([control]);
        } else if (table && control.matches('select.vrf-select')) {
            const row = control.closest('tr[data-row-key]');
            const key = row?.dataset.rowKey;
            if (!key) return;
            const shown = writer.choices.get(key)
                ?? (Array.from(control.options).find((option) => option.defaultSelected) || control.options[0])?.value;
            // TomSelect can announce the value the row shows already; only a new value is a choice.
            if (control.value === shown) return;
            writer.choices.set(key, control.value);
            enqueue({ url: writer.urls.choice, row: key, fields: { row: key, value: control.value } });
        } else if (control.matches('#set-primary-ip-toggle-cb') && draftTable()) {
            enqueue({ url: writer.urls.management, fields: { selected: control.checked ? 'on' : 'off' } });
        }
    });

    // A shift-click sets every row from the last clicked row to this one to the state of the last clicked row.
    document.addEventListener('click', function (event) {
        const box = event.target;
        if (!(box instanceof HTMLInputElement) || !box.matches(ROW_CHECKBOX)) return;
        const table = box.closest(DRAFT_TABLE);
        if (!table) return;
        const anchor = table._draftAnchor;
        table._draftAnchor = box;
        if (!event.shiftKey || !anchor || !anchor.isConnected || anchor === box) return;
        const boxes = selectableBoxes(table);
        const [start, end] = [boxes.indexOf(anchor), boxes.indexOf(box)].sort((a, b) => a - b);
        if (start === -1) return;
        const range = boxes.slice(start, end + 1);
        range.forEach((other) => {
            other.checked = anchor.checked;
        });
        takeInput(range);
    });

    // The unload carries the row states that no batch carries yet; the server takes them only for this revision.
    window.addEventListener('pagehide', function () {
        if (!writer.dirty.size || writer.stale || !writer.urls.selection) return;
        const body = new FormData();
        body.set('csrfmiddlewaretoken', csrfToken());
        body.set('draft_id', writer.draftId);
        // A command in flight takes the next revision when it commits.
        body.set('expected_revision', String(writer.revision + (writer.inFlight ? 1 : 0)));
        body.set('assignments', JSON.stringify(Object.fromEntries(writer.dirty)));
        navigator.sendBeacon(writer.urls.selection, body);
        writer.dirty.clear();
    });

    // A command that no answer acknowledged yet could be lost with the page, so the browser asks first.
    window.addEventListener('beforeunload', function (event) {
        if (!writer.dirty.size && !writer.inFlight && !writer.commands.length) return;
        event.preventDefault();
        event.returnValue = '';
    });

    function isBulkSubmit(event) {
        const name = event.detail.triggeringEvent?.submitter?.name;
        return name !== 'sync_one' && name !== 'create_vrf';
    }

    // A filter request reads the draft, so it waits for the writer. A sync submit waits for the writer and
    // names the draft; the bulk submit claims the draft, so it is the writer's command in flight.
    document.addEventListener('htmx:confirm', function (event) {
        const elt = event.detail.elt;
        const draftForm = elt instanceof HTMLFormElement && elt.querySelector(DRAFT_TABLE);
        if (!draftForm && !elt.matches?.('.sync-filter-form')) return;
        if (!draftForm && idle() && !writer.failed) return;
        event.preventDefault();
        // No request goes, so the page treats it as a request without an answer: the submit button works again.
        const refuse = () => elt.dispatchEvent(new CustomEvent('librenmsRequestFailed', { bubbles: true, detail: { elt } }));
        const epoch = draftForm ? null : reserveNavigation();
        flush().then(() => {
            if (epoch !== null) {
                // A newer navigation started while the filter waited for the writer.
                if (epoch !== navEpoch) return;
                elt._lnmsNavEpoch = epoch;
            }
            if (draftForm && writer.stale) {
                // The page waits for the draft that another window changed, so it writes nothing.
                refuse();
                return;
            }
            if (draftForm && isBulkSubmit(event)) {
                writer.inFlight = true;
                writer.submitting = elt;
            }
            event.detail.issueRequest(true);
        }, () => {
            if (draftForm) refuse();
            else endNavigation(epoch);
        });
    });

    // The claim answers with the revision it left, and the input made during the submit goes with it.
    document.addEventListener('htmx:afterRequest', function (event) {
        const read = reads.get(event.detail.xhr);
        if (read && !read.rowKey && !read.region) endNavigation(read.navEpoch);
        if (!writer.submitting || event.detail.elt !== writer.submitting) return;
        writer.submitting = null;
        writer.inFlight = false;
        const revision = event.detail.successful ? event.detail.xhr.getResponseHeader(REVISION_HEADER) : null;
        if (revision === null) {
            // No claim, or its answer is lost: the input made during the submit cannot go, and the page reads the draft.
            writer.stale = true;
            writer.dirty.clear();
            writer.commands = [];
            readResults();
        } else {
            writer.revision = Number(revision);
        }
        pump();
    });

    document.addEventListener(RESULTS_STALE_EVENT, readResults);

    // ============================================
    // NAVIGATION AND THE READ GATE
    // ============================================

    disableHtmxHistory();
    document.body.addEventListener('htmx:load', takeHistory);
    document.addEventListener('DOMContentLoaded', adoptPresentation);

    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target instanceof Element ? event.target.closest(NAVIGATION_LINKS) : null;
        if (!link) return;
        event.preventDefault();
        navigate(link.href, 'push');
    });

    document.addEventListener('htmx:configRequest', function (event) {
        const elt = event.detail.elt;
        if (elt.id === LOADER_ID) {
            event.detail.path = event.detail.triggeringEvent.detail.url;
        } else if (elt instanceof HTMLFormElement && elt.querySelector(DRAFT_TABLE) && writer.draftId) {
            const parameters = event.detail.parameters;
            parameters.set('draft_id', writer.draftId);
            parameters.set('expected_revision', String(writer.revision));
            // A row action syncs the row that the page shows: its evidence names the row's state and VRF.
            const rowKey = parameters.get('sync_one');
            if (rowKey) parameters.set('evidence', rowByKey(rowKey)?.dataset.evidence || '');
        }
    });

    document.addEventListener('htmx:beforeRequest', function (event) {
        const { elt, xhr } = event.detail;
        if (elt.id === LOADER_ID) {
            const { url, history, navEpoch: epoch } = event.detail.requestConfig.triggeringEvent.detail;
            reads.set(xhr, { navEpoch: epoch, inputEpoch, url, history });
        } else if (elt.matches('.sync-filter-form')) {
            // A filter that waited for the writer keeps the identity of its start; any other is the newest navigation.
            const epoch = elt._lnmsNavEpoch ?? reserveNavigation();
            delete elt._lnmsNavEpoch;
            reads.set(xhr, { navEpoch: epoch, inputEpoch, url: null, history: 'push' });
        } else if (elt.matches('tr[data-row-key]') && elt.closest(DRAFT_TABLE)) {
            reads.set(xhr, { navEpoch, inputEpoch, rowKey: elt.dataset.rowKey });
        } else if (draftTable() && event.detail.target?.contains(draftTable())) {
            // A tab fragment or a data refresh replaces the draft table too; it is sent without a flush.
            reads.set(xhr, { navEpoch, inputEpoch, region: true, sentIdle: idle() });
        }
    });

    // The gate runs before htmx reads any header, out-of-band content or swap of the response.
    document.addEventListener('htmx:beforeOnLoad', function (event) {
        const read = reads.get(event.detail.xhr);
        if (!read || read.region) return;
        if (read.navEpoch !== navEpoch) {
            event.preventDefault();
        } else if (read.inputEpoch !== inputEpoch) {
            event.preventDefault();
            if (read.rowKey) {
                readRow(read.rowKey);
            } else {
                navigate(pageUrl(read, event.detail.xhr), read.history);
            }
        } else {
            read.accepted = true;
        }
    });

    // htmx writes its history before the swap too, so the swapped content reads the new URL.
    document.addEventListener('htmx:beforeSwap', function (event) {
        const read = reads.get(event.detail.xhr);
        if (read?.region) {
            // Decided here, not in beforeOnLoad: the fragment loader reads the request outcome, and only a swap is held.
            if (read.navEpoch !== navEpoch || read.inputEpoch !== inputEpoch || !read.sentIdle) {
                event.detail.shouldSwap = false;
                if (read.navEpoch === navEpoch) readResults();
            }
            return;
        }
        if (!read?.accepted || read.rowKey || event.defaultPrevented || !event.detail.shouldSwap) return;
        writeHistory(pageUrl(read, event.detail.xhr), read.history);
    });

    // A swap can bring a new draft table: a READ, a filter or a data refresh.
    document.addEventListener('htmx:afterSettle', function (event) {
        const read = reads.get(event.detail.xhr);
        if (read?.accepted && read.rowKey) {
            writer.choices.delete(read.rowKey);
            rowByKey(read.rowKey)?.dispatchEvent(new CustomEvent('librenms:row-swapped', { bubbles: true }));
        } else {
            adoptPresentation();
        }
    });

    // A row READ that fails (the snapshot or the row is gone) shows the tab as it is now.
    document.addEventListener('htmx:responseError', function (event) {
        if (reads.get(event.detail.xhr)?.rowKey) readResults();
    });

    window.librenmsSyncDraft = Object.freeze({ navigate, flush, bumpInputEpoch });
})();
