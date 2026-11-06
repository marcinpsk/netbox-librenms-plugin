/**
 * The navigation scheduler of the LibreNMS sync page, and the writer of the server-side selection draft.
 *
 * Every READ that changes what the sync tabs show is a navigation: a tab link, a page, sort or Per Page
 * link, Clear filters, a filter or search request, and Back or Forward. navigate() sends its READ through
 * the page's #librenms-sync-nav loader (hx-sync="this:replace"), so a newer navigation aborts an older one.
 * The filter form keeps its own htmx trigger (the search delay and queue last on the same loader). A filter takes
 * its identity at its first htmx:confirm, before htmx queues it, and a search already when the user types,
 * before its delay; so a filter that a later navigation replaced never goes, and an earlier navigation never
 * replaces it. The answer of a filter still shows while only later filters of its form came after it.
 * A response is accepted only while its navigation is the current one and no input came after it was
 * sent; a later input drops it and starts one new READ of its page URL.
 * This script is the only writer of the page's browser history, and it handles every popstate itself.
 *
 * A converted table (table[data-draft-id]) keeps its selection in the user's draft on the server. A
 * checkbox paints at once; the writer sends the new row states in one batch per 100 ms of input, each
 * write with the draft revision of the last acknowledgement. A row's choice is a write too, after which
 * the row is read again (a row READ, which is not a navigation).
 *
 * Every element of a tab or its dialogs that posts carries data-draft-command, and that marker alone makes
 * its request a command of the writer. The value says what else the command does: "draft" names the user's
 * draft (a bulk submit claims it), "refresh" answers with new tab content, "action" does neither. An
 * element with hx-confirm asks first: its command is admitted when the user confirms.
 *
 * Every operation runs in ONE queue, one at a time in admission order (see admit()): the writes, the
 * commands and the READs. So a READ never shows a state from before an operation admitted ahead of it.
 */
(function () {
    'use strict';

    const LOADER_ID = 'librenms-sync-nav';
    const PAGE_URL_HEADER = 'X-Sync-Page-Url';
    const NAVIGATION_LINKS = '#librenms-sync-tabs [data-sync-nav], #librenms-sync-tabs thead th > a[href^="?"]';
    const DRAFT_TABLE = 'table[data-draft-id]';
    const COMMAND = '[data-draft-command]';
    const ROW_CHECKBOX = 'td input[type="checkbox"][name="select"]';
    const REVISION_HEADER = 'X-Draft-Revision';
    const ROW_READ_EVENT = 'librenms:row-read';
    const ROW_STALE_EVENT = 'lnms-row-stale';
    const RESULTS_STALE_EVENT = 'lnms-results-stale';
    const NOTICE_ID = 'librenms-draft-notice';
    const BATCH_DELAY_MS = 100;
    const CONFLICT_NOTICE_MS = 10000;
    // The operations that write the draft with a fetch: a batch of row states, and a choice or Set Primary IP.
    const WRITES = new Set(['batch', 'write']);
    // The READs that a newer navigation replaces while they wait in the queue.
    const SUPERSEDED_READS = new Set(['navigation', 'filter', 'row']);

    let navEpoch = 0;
    let inputEpoch = 0;
    // The navigation that waits in the queue or for its READ; it shows the draft, so nothing else reads it.
    let pendingNav = null;
    // The filter form whose request is pendingNav: htmx drops a queued request of a removed form with no event.
    let pendingForm = null;
    // The filter form whose filters took every navigation epoch from first to navEpoch, or null.
    let filterRun = null;
    // The navigation epoch of each filter request, by its triggering event: htmx confirms a queued request
    // again with the same event when it sends it, and a delayed search with the input event that started it.
    const filterEpochs = new WeakMap();
    // The page URL that the last navigation shows, or will show when its READ is accepted.
    let destination = window.location.href;
    // The navigation and input epochs at which each READ was sent, by its XMLHttpRequest.
    const reads = new WeakMap();
    // True while htmx swaps the answer of a page READ, which holds the whole page.
    let pageSwap = false;

    // The draft writer of this document: one writer, so its acknowledgements give the next expected revision.
    const writer = {
        // The table whose presentation the writer took its draft from.
        table: null,
        draftId: null,
        revision: null,
        urls: {},
        // The operations that wait, in admission order; see admit().
        queue: [],
        // The operation that runs now. A write or a command runs until its answer; a READ ends when it is sent.
        running: null,
        timer: null,
        // A write failed: it waits at the head of the queue for a retry, and nothing else runs.
        failed: false,
        // Another window changed the draft: input waits for the fresh presentation, which this page reads.
        stale: false,
        // The last choice this page sent per row, until a row READ shows the row again.
        choices: new Map(),
    };

    function idle() {
        return !writer.queue.length && !writer.running;
    }

    // An operation that changes the draft or NetBox: a write, or a command.
    function writes(op) {
        return WRITES.has(op.kind) || op.kind === 'command';
    }

    // Whether a write or a command waits or runs: then a table on the page can be older than the draft.
    function writesPending() {
        return writer.queue.some(writes) || Boolean(writer.running && writes(writer.running));
    }

    /**
     * Admit one operation at the end of the queue, and run the queue.
     *
     * The kinds: "batch" (row states), "write" (a choice or Set Primary IP), "command" (a marked element's
     * request), "navigation" (a READ of the loader), "filter" (a filter request that waits for its turn),
     * "row" (a row READ) and "turn" (flush()). The supersession rules are these, and only these: a newer
     * navigation removes the queued navigations, filters and row READs (reserveNavigation()); row states join
     * only the batch at the tail of the queue (tailBatch()); a choice replaces only a choice of its row at the
     * tail (enqueue()); a failed write drops every queued operation that is not a write (failure()); a
     * conflict drops the queued writes (conflict()).
     *
     * @param {Object} op - The operation: its kind, run(op) that calls done(op) when it ends, and drop(error).
     * @returns {void}
     */
    function admit(op) {
        if (!WRITES.has(op.kind) && writer.failed) {
            // An operation that is not input tries the failed write again first.
            writer.failed = false;
            hideNotice();
        }
        writer.queue.push(op);
        pump();
    }

    /**
     * Start the operation at the head of the queue when nothing runs. The newest batch waits for the batch
     * time while nothing waits behind it.
     *
     * @returns {void}
     */
    function pump() {
        if (writer.running || writer.failed) return;
        const next = writer.queue[0];
        if (!next) return;
        if (next.kind === 'batch' && writer.queue.length === 1 && writer.timer !== null) return;
        writer.queue.shift();
        if (next.kind === 'batch') {
            clearTimeout(writer.timer);
            writer.timer = null;
        }
        // An operation that writes changes what the tab shows: a READ sent before it is read again after it.
        if (writes(next)) bumpInputEpoch();
        writer.running = next;
        next.run(next);
    }

    /**
     * End the running operation *op*, and start the next one after the current task: htmx ends a request
     * (and lets its element send again) only when its answer's handlers return.
     *
     * @param {Object} op - The operation that ended.
     * @returns {void}
     */
    function done(op) {
        if (writer.running !== op) return;
        writer.running = null;
        // A table that came while this operation wrote could not be taken then.
        if (writes(op)) adoptPresentation();
        queueMicrotask(pump);
    }

    function dropQueued(test, error) {
        const dropped = writer.queue.filter(test);
        writer.queue = writer.queue.filter((op) => !test(op));
        dropped.forEach((op) => op.drop?.(error));
    }

    /**
     * Wait for this page's turn in the queue: every operation admitted before has ended.
     *
     * A failed write is tried again first, and a new failure rejects: the page keeps the failure notice.
     *
     * @returns {Promise<?number>} The last acknowledged draft revision: null while the page has no draft table.
     */
    function flush() {
        return new Promise((resolve, reject) => {
            admit({
                kind: 'turn',
                run: (op) => {
                    resolve(writer.revision);
                    done(op);
                },
                drop: reject,
            });
        });
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
     * Show the sync page at *url* in the sync tabs, when its turn in the queue comes.
     *
     * @param {string} url - A URL of the sync page.
     * @param {'push'|'replace'|'none'} history - How an accepted response enters the browser history.
     * @returns {Promise<void>} Resolves when the READ is sent, or when a newer navigation or a failed write stops it.
     */
    function navigate(url, history) {
        const epoch = reserveNavigation();
        const target = new URL(url, window.location.href).href;
        destination = target;
        return new Promise((resolve) => {
            admit({
                kind: 'navigation',
                run: (op) => {
                    document.getElementById(LOADER_ID).dispatchEvent(
                        new CustomEvent('librenms:navigate', { detail: { url: target, history, navEpoch: epoch } })
                    );
                    resolve();
                    done(op);
                },
                drop: (error) => {
                    if (error) endNavigation(epoch);
                    resolve();
                },
            });
        });
    }

    // A navigation takes its identity when the user starts it, so a later one wins also while both wait.
    function reserveNavigation(form = null) {
        navEpoch += 1;
        if (!form || filterRun?.form !== form) filterRun = form ? { form, first: navEpoch } : null;
        pendingNav = navEpoch;
        pendingForm = form;
        dropQueued((op) => SUPERSEDED_READS.has(op.kind), null);
        return navEpoch;
    }

    // htmx sends the later filters of the run after this one, so its answer can show until they come.
    function inFilterRun(read) {
        return Boolean(read.form) && filterRun?.form === read.form && read.navEpoch >= filterRun.first;
    }

    function endNavigation(epoch) {
        if (pendingNav !== epoch) return;
        pendingNav = null;
        pendingForm = null;
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
     * Only a writer with no write pending takes it: an accepted READ was sent after every write admitted
     * before it, and no input came after it. A table the writer took before keeps the writer's own
     * revision, which later acknowledgements moved on. The writer never goes back to an older revision of
     * its draft: it keeps its own and reads the tab again.
     *
     * @returns {void}
     */
    function adoptPresentation() {
        const table = draftTable();
        if (!table || table === writer.table || writesPending()) return;
        const revision = Number(table.dataset.draftRevision);
        // A table of an older revision of this draft shows rows that a later command changed: read the tab again.
        const older = table.dataset.draftId === writer.draftId && revision < writer.revision;
        writer.table = table;
        writer.draftId = table.dataset.draftId;
        writer.revision = older ? writer.revision : revision;
        writer.urls = {
            selection: table.dataset.selectionUrl,
            choice: table.dataset.choiceUrl,
            management: table.dataset.managementUrl,
        };
        writer.stale = false;
        writer.choices.clear();
        // Also while the navigation that brought the table waits for its end: that READ is the stale one.
        if (older) navigate(destination, 'replace');
    }

    function rowByKey(key) {
        // Compare the keys instead of building a selector: no row key may reach a selector.
        return Array.from(draftTable()?.querySelectorAll('tr[data-row-key]') || []).find((row) => row.dataset.rowKey === key);
    }

    /**
     * Read one row of the draft table again, when its turn comes; a row on no shown page is not read.
     *
     * @param {string} key - The row key.
     * @returns {void}
     */
    function readRow(key) {
        if (writer.queue.some((op) => op.kind === 'row' && op.key === key)) return;
        admit({
            kind: 'row',
            key,
            run: (op) => {
                // After a conflict the page reads the whole table again.
                if (!writer.stale) rowByKey(key)?.dispatchEvent(new CustomEvent(ROW_READ_EVENT));
                done(op);
            },
        });
    }

    // A navigation that waits shows the draft with its own history entry, so it is the fresh READ.
    function readResults() {
        if (pendingForm && !pendingForm.isConnected) endNavigation(pendingNav);
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

    // Run a write: post it with the draft and the revision of the last acknowledgement, and end at its answer.
    function sendWrite(op) {
        const fields = op.kind === 'batch' ? { assignments: JSON.stringify(Object.fromEntries(op.rows)) } : op.fields;
        const body = new FormData();
        body.set('draft_id', writer.draftId);
        body.set('expected_revision', String(writer.revision));
        Object.entries(fields).forEach(([name, value]) => body.set(name, value));
        fetch(op.kind === 'batch' ? writer.urls.selection : op.url, {
            method: 'POST',
            body,
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((response) => {
                if (response.status === 204) {
                    acknowledge(response);
                } else if (response.status === 409) {
                    conflict();
                } else {
                    failure(op);
                }
            }, () => failure(op))
            .finally(() => done(op));
    }

    function acknowledge(response) {
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
     * The operations that wait run after the write in flight ends, so a navigation that waits is the READ.
     *
     * @returns {void}
     */
    function conflict() {
        writer.stale = true;
        dropQueued((op) => WRITES.has(op.kind), null);
        clearTimeout(writer.timer);
        writer.timer = null;
        showNotice('The selection changed in another window. The tab now shows it; select again.', null);
        readResults();
    }

    function failure(op) {
        // The write goes first again at the retry; a newer state of a row that the user set meanwhile goes after it.
        writer.queue.unshift(op);
        writer.failed = true;
        dropQueued((other) => !WRITES.has(other.kind), new Error('The selection is not saved.'));
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

    // Input joins the batch at the tail; behind any other operation it starts a new batch, which goes after it.
    function tailBatch() {
        const tail = writer.queue.at(-1);
        if (tail?.kind === 'batch') return tail.rows;
        const batch = { kind: 'batch', rows: new Map(), run: sendWrite };
        writer.queue.push(batch);
        return batch.rows;
    }

    function setRowState(checkbox) {
        const row = checkbox.closest('tr[data-row-key]');
        if (!row) return;
        const checked = checkbox.checked;
        tailBatch().set(row.dataset.rowKey, { state: checked ? 'on' : 'none', token: checked ? row.dataset.evidence : null });
    }

    /**
     * Return the evidence token of each management row as the page shows it, by row key.
     *
     * The table carries the tokens of every management row, also of one that no page shows; a row that
     * the page shows gives its own token, which a row READ can have changed.
     *
     * @param {HTMLTableElement} table - The writer's draft table.
     * @returns {Object<string, string>} The tokens.
     */
    function shownManagementEvidence(table) {
        const evidence = JSON.parse(table.dataset.managementEvidence);
        table.querySelectorAll('tr[data-row-key][data-mgmt-ip="true"]').forEach((row) => {
            if (row.dataset.evidence) {
                evidence[row.dataset.rowKey] = row.dataset.evidence;
            } else {
                delete evidence[row.dataset.rowKey];
            }
        });
        return evidence;
    }

    function selectableBoxes(table) {
        return Array.from(table.querySelectorAll(ROW_CHECKBOX)).filter((box) => !box.disabled);
    }

    /**
     * Return whether the writer can take input on *table*; else the page says so and reads the tab again.
     *
     * Input has no draft to go to on a table whose draft the writer did not take, or while the page waits
     * for the draft that another window changed. The page never drops such input without a notice.
     *
     * @param {?HTMLTableElement} table - The draft table of the input.
     * @returns {boolean} True when the input can go to the writer's draft.
     */
    function accepts(table) {
        if (!writer.stale && table && table === writer.table) return true;
        showNotice('This change was not saved. The tab now shows the saved selection; select again.', null);
        readResults();
        return false;
    }

    /**
     * Take one user input on the writer's draft table.
     *
     * @param {HTMLInputElement[]} boxes - The row checkboxes the input set.
     * @returns {void}
     */
    function takeInput(boxes) {
        bumpInputEpoch();
        boxes.forEach(setRowState);
        schedule();
        pump();
    }

    function enqueue(write) {
        bumpInputEpoch();
        // A choice of the same row that is the last queued operation is replaced: the last choice counts.
        const tail = writer.queue.at(-1);
        if (write.row && tail?.kind === 'write' && tail.row === write.row) {
            tail.fields = write.fields;
            return;
        }
        admit({ kind: 'write', ...write, run: sendWrite });
    }

    document.addEventListener('change', function (event) {
        const control = event.target;
        if (!(control instanceof Element)) return;
        const table = control.closest(DRAFT_TABLE);
        if (table && control.matches('th input.toggle')) {
            if (!accepts(table)) return;
            const boxes = selectableBoxes(table).filter((box) => box.checked !== control.checked);
            boxes.forEach((box) => {
                box.checked = control.checked;
            });
            takeInput(boxes);
        } else if (table && control.matches(ROW_CHECKBOX)) {
            if (accepts(table)) takeInput([control]);
        } else if (table && control.matches('select.vrf-select')) {
            const row = control.closest('tr[data-row-key]');
            const key = row?.dataset.rowKey;
            if (!key) return;
            const shown = writer.choices.get(key)
                ?? (Array.from(control.options).find((option) => option.defaultSelected) || control.options[0])?.value;
            // TomSelect can announce the value the row shows already; only a new value is a choice.
            if (control.value === shown || !accepts(table)) return;
            writer.choices.set(key, control.value);
            enqueue({ url: writer.urls.choice, row: key, fields: { row: key, value: control.value } });
        } else if (control.matches('#set-primary-ip-toggle-cb') && draftTable()) {
            if (!accepts(draftTable())) return;
            const evidence = JSON.stringify(shownManagementEvidence(draftTable()));
            enqueue({ url: writer.urls.management, fields: { selected: control.checked ? 'on' : 'off', evidence } });
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
        if (accepts(table)) takeInput(range);
    });

    // Clear on the off-page notice clears the rows that the notice counts; the tab then shows the draft again.
    document.addEventListener('click', function (event) {
        const clear = event.target instanceof Element ? event.target.closest('[data-clear-offpage]') : null;
        if (!clear || !accepts(draftTable())) return;
        bumpInputEpoch();
        const rows = tailBatch();
        JSON.parse(clear.dataset.clearOffpage).forEach((key) => rows.set(key, { state: 'none', token: null }));
        schedule();
        readResults();
    });

    // The unload carries the row states that no batch carries yet; the server takes them only for this revision.
    // A write or command that waits is lost with the page (the unload prompt asks), so the rows go without it.
    window.addEventListener('pagehide', function () {
        if (writer.stale || !writer.urls.selection) return;
        const rows = new Map();
        writer.queue.forEach((op) => op.kind === 'batch' && op.rows.forEach((state, key) => rows.set(key, state)));
        if (!rows.size) return;
        const running = writer.running;
        const body = new FormData();
        body.set('csrfmiddlewaretoken', csrfToken());
        body.set('draft_id', writer.draftId);
        // A write or a claim in flight takes the next revision when it commits.
        const nextRevision = running && (WRITES.has(running.kind) || running.claims);
        body.set('expected_revision', String(writer.revision + (nextRevision ? 1 : 0)));
        body.set('assignments', JSON.stringify(Object.fromEntries(rows)));
        navigator.sendBeacon(writer.urls.selection, body);
        writer.queue = writer.queue.filter((op) => op.kind !== 'batch');
    });

    // A write that no answer acknowledged yet could be lost with the page, so the browser asks first.
    window.addEventListener('beforeunload', function (event) {
        if (!writesPending()) return;
        event.preventDefault();
        event.returnValue = '';
    });

    function isBulkSubmit(event) {
        const name = event.detail.triggeringEvent?.submitter?.name;
        return name !== 'sync_one' && name !== 'create_vrf';
    }

    /**
     * Admit one command of a marked element; it sends its request when its turn comes.
     *
     * The command keeps the path and the submit button of its start: htmx posts the button that the user
     * clicked last, which can be another one by the time the command runs.
     *
     * @param {CustomEvent} event - The element's htmx:confirm event.
     * @param {string} command - The element's data-draft-command value.
     * @param {function(boolean): void} issue - The htmx function that sends the request.
     * @returns {void}
     */
    function admitCommand(event, command, issue) {
        const elt = event.detail.elt;
        // No request goes, so the page treats it as a request without an answer: the submit button works again.
        const refuse = () => elt.dispatchEvent(new CustomEvent('librenmsRequestFailed', { bubbles: true, detail: { elt } }));
        const submitter = event.detail.triggeringEvent?.submitter;
        admit({
            kind: 'command',
            elt,
            claims: command === 'draft' && isBulkSubmit(event),
            path: event.detail.path,
            submitter: submitter?.name ? { name: submitter.name, value: submitter.value } : null,
            xhr: null,
            run: (op) => {
                if (!elt.isConnected) {
                    // An operation before it (a refresh) replaced its element: it never goes with new evidence.
                    showNotice('The tab changed; the action was not sent. Check the row and try again.', null);
                    done(op);
                    return;
                }
                if (command === 'draft' && writer.stale) {
                    // The page waits for the draft that another window changed, so it writes nothing.
                    refuse();
                    done(op);
                    return;
                }
                issue(true);
                // htmx sends a command at once, or drops it (hx-sync, validation) with no event: then it ends here.
                if (!op.xhr) finishCommand(op, null, false);
            },
            drop: refuse,
        });
    }

    // A command (data-draft-command) and a filter request take their turn in the queue.
    document.addEventListener('htmx:confirm', function (event) {
        const elt = event.detail.elt;
        const command = elt.matches?.(COMMAND) ? elt.dataset.draftCommand : null;
        if (command) {
            const issue = event.detail.issueRequest;
            if (event.detail.question) {
                // The page's confirmation dialog asks first and then calls detail.issueRequest: the command
                // is admitted only after the user confirmed.
                event.detail.issueRequest = () => admitCommand(event, command, issue);
            } else {
                event.preventDefault();
                admitCommand(event, command, issue);
            }
            return;
        }
        if (!elt.matches?.('.sync-filter-form')) return;
        let epoch = filterEpochs.get(event.detail.triggeringEvent);
        if (epoch === undefined) {
            // The identity is taken before htmx queues the request behind a running READ.
            epoch = reserveNavigation(elt);
            filterEpochs.set(event.detail.triggeringEvent, epoch);
        } else if (epoch !== navEpoch) {
            // htmx sends a delayed or queued filter that a newer navigation replaced.
            event.preventDefault();
            return;
        }
        if (idle() && !writer.failed) return;
        event.preventDefault();
        admit({
            kind: 'filter',
            run: (op) => {
                event.detail.issueRequest(true);
                done(op);
            },
            drop: (error) => {
                if (error) endNavigation(epoch);
            },
        });
    });

    // A search takes its identity when the user types, before the search delay: a navigation sent before then
    // cannot replace the search field, and a later one replaces the search.
    document.addEventListener('input', function (event) {
        const field = event.target;
        if (!(field instanceof HTMLInputElement) || !field.matches('.sync-filter-search')) return;
        if (!field.form?.matches('.sync-filter-form')) return;
        filterEpochs.set(event, reserveNavigation(field.form));
    });

    /**
     * End the command *op* when its answer comes: before htmx swaps the answer (so the writer can take a
     * table that the answer brings), or at the end of a request that swaps nothing.
     *
     * A claim answers with the revision it left, and the input made during the submit goes with it. Any
     * other command leaves the draft as it is. Each answer asks for the tab's results again.
     *
     * @param {Object} op - The running command.
     * @param {?XMLHttpRequest} xhr - The command's request; null when htmx sent none.
     * @param {boolean} succeeded - Whether the answer is a success.
     * @returns {void}
     */
    function finishCommand(op, xhr, succeeded) {
        const revision = succeeded ? xhr.getResponseHeader(REVISION_HEADER) : null;
        // A claim that htmx did not send left the draft as it was.
        if (op.claims && op.xhr && revision === null) {
            // No claim, or its answer is lost: the input made during the submit cannot go, and the page reads the draft.
            writer.stale = true;
            dropQueued((other) => WRITES.has(other.kind), null);
            readResults();
        } else if (op.claims && op.xhr) {
            writer.revision = Number(revision);
        }
        done(op);
    }

    function isCommandAnswer(detail) {
        return writer.running?.kind === 'command' && writer.running.xhr === detail.xhr;
    }

    document.addEventListener('htmx:afterRequest', function (event) {
        // A page READ whose swap did not happen leaves no flag behind for a later request.
        pageSwap = false;
        const read = reads.get(event.detail.xhr);
        if (read && !read.rowKey && !read.region) endNavigation(read.navEpoch);
        if (isCommandAnswer(event.detail)) finishCommand(writer.running, event.detail.xhr, event.detail.successful);
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
        const parameters = event.detail.parameters;
        const op = writer.running;
        if (elt.id === LOADER_ID) {
            event.detail.path = event.detail.triggeringEvent.detail.url;
            return;
        }
        if (op?.kind === 'command' && op.elt === elt && elt instanceof HTMLFormElement) {
            // htmx posts the button that the user clicked last; the command posts the one that started it.
            event.detail.path = op.path;
            Array.from(elt.elements).forEach((control) => {
                if (control.type === 'submit' && control.name) parameters.delete(control.name);
            });
            if (op.submitter) parameters.set(op.submitter.name, op.submitter.value);
        }
        if (elt.matches?.('[data-draft-command="draft"]') && writer.draftId) {
            parameters.set('draft_id', writer.draftId);
            parameters.set('expected_revision', String(writer.revision));
            // A row action syncs the row that the page shows: its evidence names the row's state and VRF.
            const rowKey = parameters.get('sync_one');
            if (rowKey) parameters.set('evidence', rowByKey(rowKey)?.dataset.evidence || '');
        }
    });

    document.addEventListener('htmx:beforeRequest', function (event) {
        const { elt, xhr } = event.detail;
        const op = writer.running;
        // htmx fires this one on the element that sends the request, before any swap.
        if (op?.kind === 'command' && !op.xhr && elt === op.elt) op.xhr = xhr;
        if (elt.id === LOADER_ID) {
            const { url, history, navEpoch: epoch } = event.detail.requestConfig.triggeringEvent.detail;
            reads.set(xhr, { navEpoch: epoch, inputEpoch, url, history });
        } else if (elt.matches('.sync-filter-form')) {
            // The filter keeps the identity it took at its first confirm.
            const epoch = filterEpochs.get(event.detail.requestConfig.triggeringEvent);
            reads.set(xhr, { navEpoch: epoch, inputEpoch, url: null, history: 'push', form: elt });
        } else if (elt.matches('tr[data-row-key]') && elt.closest(DRAFT_TABLE)) {
            reads.set(xhr, { navEpoch, inputEpoch, rowKey: elt.dataset.rowKey });
        } else if (draftTable() && event.detail.target?.contains(draftTable())) {
            // A tab fragment or a data refresh replaces the draft table too. A fragment is sent outside the
            // queue; a refresh is a command, which runs when nothing else does.
            const command = isCommandAnswer(event.detail);
            reads.set(xhr, { navEpoch, inputEpoch, region: true, draft: true, sentIdle: command || !writesPending() });
        } else if (event.detail.requestConfig.verb === 'get' && event.detail.target?.closest?.('#librenms-sync-tabs')) {
            // Any other READ of a tab region, such as a tab fragment that the cache check reloads.
            reads.set(xhr, { navEpoch, inputEpoch, region: true, draft: false });
        }
    });

    // The gate runs before htmx reads any header, out-of-band content or swap of the response.
    document.addEventListener('htmx:beforeOnLoad', function (event) {
        const read = reads.get(event.detail.xhr);
        if (!read || read.region) return;
        const superseded = read.navEpoch !== navEpoch;
        if (superseded && !inFilterRun(read)) {
            event.preventDefault();
        } else if (read.inputEpoch !== inputEpoch) {
            event.preventDefault();
            // A later filter of the same form reads the page after this one.
            if (superseded) return;
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
            // A navigation or a search that started after the READ was sent replaces it; a draft region also
            // waits for the writes. The page then reads its current destination (a navigation that waits is it).
            const draftChanged = read.draft && (read.inputEpoch !== inputEpoch || !read.sentIdle);
            if (read.navEpoch !== navEpoch || draftChanged) {
                event.detail.shouldSwap = false;
                readResults();
            }
        }
        if (isCommandAnswer(event.detail) && event.detail.shouldSwap) {
            finishCommand(writer.running, event.detail.xhr, !event.detail.isError);
        }
        if (read?.region) return;
        if (!read?.accepted || read.rowKey || event.defaultPrevented || !event.detail.shouldSwap) return;
        writeHistory(pageUrl(read, event.detail.xhr), read.history);
        pageSwap = Boolean(read.url);
    });

    // A page READ shows only the sync tabs: the page's own out-of-band parts (NetBox's empty messages
    // container) would remove the messages that the last request showed.
    document.addEventListener('htmx:oobBeforeSwap', function (event) {
        if (pageSwap) event.detail.shouldSwap = false;
    });

    // A swap can bring a new draft table (a READ, a filter or a data refresh) or a new row (a row READ). The
    // writer takes it when htmx inserts it, in the same task, so no input on it comes before (htmx settles it later).
    document.addEventListener('htmx:afterSwap', function (event) {
        pageSwap = false;
        const read = reads.get(event.detail.xhr);
        // The row now shows the draft's choice: a choice after this swap compares with the row.
        if (read?.accepted && read.rowKey) writer.choices.delete(read.rowKey);
        adoptPresentation();
    });

    document.addEventListener('htmx:afterSettle', function (event) {
        const read = reads.get(event.detail.xhr);
        if (!read?.accepted || !read.rowKey) return;
        rowByKey(read.rowKey)?.dispatchEvent(new CustomEvent('librenms:row-swapped', { bubbles: true }));
    });

    // Back or Forward can restore this document from the browser's cache, with the draft as it showed it then.
    window.addEventListener('pageshow', function (event) {
        if (event.persisted && document.getElementById(LOADER_ID)) navigate(window.location.href, 'none');
    });

    // Another script can cancel a filter at htmx:configRequest, and htmx then sends nothing and fires no event.
    // The window gets the event after every document listener.
    window.addEventListener('htmx:configRequest', function (event) {
        if (!event.defaultPrevented || !event.detail.elt.matches?.('.sync-filter-form')) return;
        endNavigation(filterEpochs.get(event.detail.triggeringEvent));
    });

    // A row READ that fails (the snapshot or the row is gone) shows the tab as it is now.
    document.addEventListener('htmx:responseError', function (event) {
        if (reads.get(event.detail.xhr)?.rowKey) readResults();
    });

    window.librenmsSyncDraft = Object.freeze({ navigate, flush, bumpInputEpoch });
})();
