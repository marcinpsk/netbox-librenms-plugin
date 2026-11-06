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
 * sent; a later input drops it and starts one new READ of its page URL after flush().
 * This script is the only writer of the page's browser history, and it handles every popstate itself.
 *
 * A converted table (table[data-draft-id]) keeps its selection in the user's draft on the server. A
 * checkbox paints at once; the writer sends the new row states in one batch per 100 ms of input, and one
 * write at a time in input order, each with the draft revision of the last acknowledgement. A row's choice is a
 * command too, after which the row is read again (a row READ, which is not a navigation). Every request
 * that reads the draft waits for flush(), so it never shows a state from before an input of this page.
 *
 * Every element of a tab or its dialogs that posts carries data-draft-command, and that marker alone makes
 * its request a command of the writer: it waits for flush(), it is the write in flight, and a READ sent
 * before it is read again after it. The value says what else the command does: "draft" names the user's
 * draft (a bulk submit claims it), "refresh" answers with new tab content, "action" does neither. An
 * element with hx-confirm asks first: its command starts when the user confirms.
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

    let navEpoch = 0;
    let inputEpoch = 0;
    // The navigation that waits for the writer or for its READ; it shows the draft, so nothing else reads it.
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

    // The draft writer of this document: one writer, so its acknowledgements give the next expected revision.
    const writer = {
        // The table whose presentation the writer took its draft from.
        table: null,
        draftId: null,
        revision: null,
        urls: {},
        // The writes that wait, in input order: batches of row states ({batch}, by row key) and commands.
        // Input after a command starts a new batch, so no write overtakes an older one.
        queue: [],
        inFlight: false,
        timer: null,
        // A write failed: the input stays here, and nothing reads the draft until a retry succeeds.
        failed: false,
        // Another window changed the draft: input waits for the fresh presentation, which this page reads.
        stale: false,
        // The command in flight that a tab element posted (data-draft-command): the element, whether the
        // command claims the draft (a bulk submit), and its request once htmx sends it. The answer's events
        // name the request: htmx can fire them on the swap target when the answer replaced the element.
        submitting: null,
        waiters: [],
        // The last choice this page sent per row, until a row READ shows the row again.
        choices: new Map(),
        // The rows to read again once the writer is idle.
        rowReads: new Set(),
    };

    function idle() {
        return !writer.queue.length && !writer.inFlight;
    }

    /**
     * Wait until the selection draft has acknowledged every input of this page.
     *
     * A failed write is tried again here, and a new failure rejects: the page keeps the failure notice.
     * After a conflict (another window changed the draft) it resolves once the writer is idle, because the
     * page must read the draft again; a navigation that waits is that READ.
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
    function reserveNavigation(form = null) {
        navEpoch += 1;
        if (!form || filterRun?.form !== form) filterRun = form ? { form, first: navEpoch } : null;
        pendingNav = navEpoch;
        pendingForm = form;
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
     * Only an idle writer takes it: an accepted READ was sent after flush() and no input came after it. A
     * table the writer took before keeps the writer's own revision, which later acknowledgements moved on.
     * The writer never goes back to an older revision of its draft: it keeps its own and reads the tab again.
     *
     * @returns {void}
     */
    function adoptPresentation() {
        const table = draftTable();
        if (!table || table === writer.table || !idle()) return;
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
     * Read one row of the draft table again, once the writer is idle; a row on no shown page is not read.
     *
     * @param {string} key - The row key.
     * @returns {void}
     */
    function readRow(key) {
        writer.rowReads.add(key);
        flush().then(() => {
            // After a conflict the page reads the whole table again.
            if (!writer.rowReads.delete(key) || writer.stale) return;
            rowByKey(key)?.dispatchEvent(new CustomEvent(ROW_READ_EVENT));
        }, () => writer.rowReads.delete(key));
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

    /**
     * Send the oldest write. The newest batch waits for the batch time, unless a READ waits for it.
     *
     * @returns {void}
     */
    function pump() {
        if (writer.inFlight || writer.failed || writer.stale) {
            if (writer.stale && idle()) settleWaiters();
            return;
        }
        const next = writer.queue[0];
        if (!next) {
            settleWaiters();
            return;
        }
        if (next.batch && writer.queue.length === 1 && writer.timer !== null && !writer.waiters.length) return;
        writer.queue.shift();
        if (next.batch) {
            send({
                url: writer.urls.selection,
                fields: { assignments: JSON.stringify(Object.fromEntries(next.batch)) },
                batch: next.batch,
            });
        } else {
            send(next);
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
     * The flushes that wait resolve when the write in flight ends, so a navigation that waits is the READ.
     *
     * @returns {void}
     */
    function conflict() {
        writer.stale = true;
        writer.queue = [];
        clearTimeout(writer.timer);
        writer.timer = null;
        showNotice('The selection changed in another window. The tab now shows it; select again.', null);
        readResults();
    }

    function failure(command) {
        // The write goes first again at the retry; a newer state of a row that the user set meanwhile goes after it.
        writer.queue.unshift(command.batch ? { batch: command.batch } : command);
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

    // Input joins the newest batch; after a command it starts a new batch, which goes after that command.
    function tailBatch() {
        const tail = writer.queue.at(-1);
        if (tail?.batch) return tail.batch;
        const batch = new Map();
        writer.queue.push({ batch });
        return batch;
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
        // A READ that waits for the writer gets the input at once.
        if (writer.waiters.length) pump();
    }

    function enqueue(command) {
        bumpInputEpoch();
        // A choice of the same row that is the last queued write is replaced: the last choice is the one that counts.
        const tail = writer.queue.at(-1);
        if (command.row && tail?.row === command.row) {
            tail.fields = command.fields;
        } else {
            writer.queue.push(command);
        }
        pump();
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

    // The unload carries the row states that no batch carries yet; the server takes them only for this revision.
    // A command that waits is lost with the page (the unload prompt asks), so the rows go without it.
    window.addEventListener('pagehide', function () {
        if (writer.stale || !writer.urls.selection) return;
        const rows = new Map();
        writer.queue.forEach((write) => write.batch?.forEach((state, key) => rows.set(key, state)));
        if (!rows.size) return;
        const body = new FormData();
        body.set('csrfmiddlewaretoken', csrfToken());
        body.set('draft_id', writer.draftId);
        // A command in flight takes the next revision when it commits.
        body.set('expected_revision', String(writer.revision + (writer.inFlight ? 1 : 0)));
        body.set('assignments', JSON.stringify(Object.fromEntries(rows)));
        navigator.sendBeacon(writer.urls.selection, body);
        writer.queue = writer.queue.filter((write) => !write.batch);
    });

    // A write that no answer acknowledged yet could be lost with the page, so the browser asks first.
    window.addEventListener('beforeunload', function (event) {
        if (idle()) return;
        event.preventDefault();
        event.returnValue = '';
    });

    function isBulkSubmit(event) {
        const name = event.detail.triggeringEvent?.submitter?.name;
        return name !== 'sync_one' && name !== 'create_vrf';
    }

    /**
     * Run one command of a tab element: wait for the writer, then send it as the write in flight.
     *
     * @param {CustomEvent} event - The element's htmx:confirm event.
     * @param {string} command - The element's data-draft-command value.
     * @param {function(boolean): void} issue - The htmx function that sends the request.
     * @returns {void}
     */
    function runCommand(event, command, issue) {
        const elt = event.detail.elt;
        // No request goes, so the page treats it as a request without an answer: the submit button works again.
        const refuse = () => elt.dispatchEvent(new CustomEvent('librenmsRequestFailed', { bubbles: true, detail: { elt } }));
        flush().then(() => {
            if (command === 'draft' && writer.stale) {
                // The page waits for the draft that another window changed, so it writes nothing.
                refuse();
                return;
            }
            // The command changes the tab like an input: a READ sent before it is read again after it.
            bumpInputEpoch();
            writer.inFlight = true;
            writer.submitting = { elt, claims: command === 'draft' && isBulkSubmit(event), xhr: null };
            issue(true);
            // htmx sends a command at once, or drops it (hx-sync, validation) with no event: then it ends here.
            if (writer.submitting?.elt === elt && !writer.submitting.xhr) finishCommand(null, false);
        }, refuse);
    }

    // A command (data-draft-command) waits for the writer and is then the write in flight. A filter request
    // reads the draft, so it waits for the writer too.
    document.addEventListener('htmx:confirm', function (event) {
        const elt = event.detail.elt;
        const command = elt.matches?.(COMMAND) ? elt.dataset.draftCommand : null;
        if (command) {
            const issue = event.detail.issueRequest;
            if (event.detail.question) {
                // The page's confirmation dialog asks first and then calls detail.issueRequest: the command
                // starts only after the user confirmed.
                event.detail.issueRequest = () => runCommand(event, command, issue);
            } else {
                event.preventDefault();
                runCommand(event, command, issue);
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
        flush().then(() => {
            // A newer navigation started while the filter waited for the writer.
            if (epoch === navEpoch) event.detail.issueRequest(true);
        }, () => endNavigation(epoch));
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
     * End the command in flight when its answer comes: before htmx swaps the answer (so the writer can take a
     * table that the answer brings), or at the end of a request that swaps nothing.
     *
     * A claim answers with the revision it left, and the input made during the submit goes with it. Any
     * other command leaves the draft as it is. Each answer asks for the tab's results again.
     *
     * @param {?XMLHttpRequest} xhr - The command's request; null when htmx sent none.
     * @param {boolean} succeeded - Whether the answer is a success.
     * @returns {void}
     */
    function finishCommand(xhr, succeeded) {
        const { claims } = writer.submitting;
        const sent = writer.submitting.xhr !== null;
        writer.submitting = null;
        writer.inFlight = false;
        const revision = succeeded ? xhr.getResponseHeader(REVISION_HEADER) : null;
        // A claim that htmx did not send left the draft as it was.
        if (claims && sent && revision === null) {
            // No claim, or its answer is lost: the input made during the submit cannot go, and the page reads the draft.
            writer.stale = true;
            writer.queue = [];
            readResults();
        } else if (claims && sent) {
            writer.revision = Number(revision);
        }
        pump();
    }

    function isCommandAnswer(detail) {
        return writer.submitting !== null && writer.submitting.xhr === detail.xhr;
    }

    document.addEventListener('htmx:afterRequest', function (event) {
        const read = reads.get(event.detail.xhr);
        if (read && !read.rowKey && !read.region) endNavigation(read.navEpoch);
        if (isCommandAnswer(event.detail)) finishCommand(event.detail.xhr, event.detail.successful);
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
        } else if (elt.matches?.('[data-draft-command="draft"]') && writer.draftId) {
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
        // htmx fires this one on the element that sends the request, before any swap.
        if (writer.submitting && !writer.submitting.xhr && elt === writer.submitting.elt) writer.submitting.xhr = xhr;
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
            // A tab fragment or a data refresh replaces the draft table too. A fragment is sent without a flush;
            // a refresh is a command, which went when the writer had nothing else to do.
            const command = isCommandAnswer(event.detail);
            reads.set(xhr, { navEpoch, inputEpoch, region: true, sentIdle: command || idle() });
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
            if (read.navEpoch !== navEpoch || read.inputEpoch !== inputEpoch || !read.sentIdle) {
                event.detail.shouldSwap = false;
                if (read.navEpoch === navEpoch) readResults();
            }
        }
        if (isCommandAnswer(event.detail) && event.detail.shouldSwap) {
            finishCommand(event.detail.xhr, !event.detail.isError);
        }
        if (read?.region) return;
        if (!read?.accepted || read.rowKey || event.defaultPrevented || !event.detail.shouldSwap) return;
        writeHistory(pageUrl(read, event.detail.xhr), read.history);
    });

    // A swap can bring a new draft table (a READ, a filter or a data refresh) or a new row (a row READ). The
    // writer takes it when htmx inserts it, in the same task, so no input on it comes before (htmx settles it later).
    document.addEventListener('htmx:afterSwap', function (event) {
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
