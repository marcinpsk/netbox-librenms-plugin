"""
Shared drivers for tests that call a view's ``get``/``post`` directly.

Production always reaches a view through ``dispatch()``, which runs ``View.setup()`` and binds
``self.request``. The object-scoped lookups read it, so a test that calls ``view.post(request, ...)``
straight would hit an unset attribute. These drivers bind the request the same way.

The builders below give a view a REAL request, a REAL user and REAL permission grants, so the
permission gate, ``restrict()`` and the messages framework all run as they do in production. A
view test that instead patches ``Device``/``Interface`` and stubs ``objects.get`` re-asserts its
own assumptions: a ``MagicMock`` answers any attribute, so such a test stays green while the real
query path is broken. Reserve mocks for the LibreNMS HTTP boundary and for errors a local DB
cannot produce (a lock ``DatabaseError``, a ``save()`` that raises).
"""

from netbox_librenms_plugin.librenms_api import LibreNMSAPI

import json
from contextlib import contextmanager
from html.parser import HTMLParser
from uuid import uuid4

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.tests.conftest import make_superuser


def bind_and_call(view, request, method, **kwargs):
    """Call ``view.<method>(request, **kwargs)`` with the request bound as ``setup()`` binds it."""
    view.setup(request, **kwargs)
    return getattr(view, method)(request, **kwargs)


def post(view, request, **kwargs):
    """POST into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "post", **kwargs)


def get(view, request, **kwargs):
    """GET into *view* with the request bound (see :func:`bind_and_call`)."""
    return bind_and_call(view, request, "get", **kwargs)


# =============================================================================
# Real users and real permission grants
# =============================================================================
#
# NetBox enforces permissions only through ObjectPermissionBackend, which ignores Django's
# ``user_permissions`` m2m. A grant therefore has to be a real ``ObjectPermission`` row, and the
# user must be re-read afterwards to drop the per-instance permission cache.
# ``make_superuser`` (re-exported from conftest) covers the unconstrained case.


def grant(user, action, model, *, constraints=None, name=None):
    """Grant a real object permission, optionally constrained, and return the user with a fresh permission cache."""
    from core.models import ObjectType
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    op = ObjectPermission.objects.create(
        name=name or f"{user.username}-{action}-{model._meta.model_name}-{ObjectPermission.objects.count()}",
        actions=[action],
        constraints=constraints,
    )
    op.object_types.set([ObjectType.objects.get_for_model(model)])
    op.users.set([user])
    return get_user_model().objects.get(pk=user.pk)


def make_user_with_perms(username, perm_specs, *, constraints=None, plugin_write=True):
    """Create a real non-superuser with exact, optionally constrained grants and optional plugin write access."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username=username, password="x")
    if plugin_write:
        from netbox_librenms_plugin.models import LibreNMSSettings

        for action in ("view", "change"):
            user = grant(user, action, LibreNMSSettings, name=f"{username}-plugin-{action}")
    for action, model in perm_specs:
        user = grant(user, action, model, constraints=constraints)
    return user


def superuser_chassis(device):
    """Return the chassis scope of *device* as a superuser sees it: every member is visible."""
    from netbox_librenms_plugin.sync_view_scope import SyncViewScope

    return SyncViewScope.for_request(make_superuser(), device).chassis


def plugin_perms():
    """The two plugin permission strings every write view checks."""
    return (PERM_VIEW_PLUGIN, PERM_CHANGE_PLUGIN)


# =============================================================================
# Real requests
# =============================================================================


def make_request(method="post", data=None, *, user=None, path="/", **factory_kwargs):
    """Build a real Django request with a user, session, message storage and django-htmx's ``request.htmx``."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory
    from django_htmx.middleware import HtmxDetails

    request = getattr(RequestFactory(), method)(path, data if data is not None else {}, **factory_kwargs)
    request.user = user if user is not None else make_superuser()
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    request.htmx = HtmxDetails(request)
    return request


def queued_request(user):
    """Return the ``copy_safe_request()`` copy that the import view queues with a background job."""
    from uuid import uuid4

    from utilities.request import copy_safe_request

    request = make_request(user=user)
    request.id = uuid4()
    return copy_safe_request(request)


def render_module_tab(device, *, user=None, server_key="default"):
    """Render the Modules tab fragment from its cached snapshot, as a module action re-renders it; return the HTML."""
    from django.urls import reverse

    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request(
        "get",
        {"tab": "modules", "server_key": server_key},
        user=user,
        path=reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk}),
    )
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    context = view.get_context_data(request, device, server_key=server_key)
    return view.render_sync_partial(request, device, context["server_key"], {"module_sync": context}).content.decode()


class _SyncTabParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.containers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and attrs.get("id") == "librenms-sync-tabs":
            self.containers.append(attrs)


def sync_tab_attributes(html):
    """Return the attributes of the one rendered sync container."""
    parser = _SyncTabParser()
    parser.feed(str(html))
    parser.close()
    assert len(parser.containers) == 1
    return parser.containers[0]


class _TitledIconParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.icons = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "i" and "title" in attrs:
            self.icons.append(attrs)


def titled_icons(html):
    """Return the attributes of each ``<i>`` with a title, after one HTML unescape."""
    parser = _TitledIconParser()
    parser.feed(str(html))
    parser.close()
    return parser.icons


def assert_titled_icons_are_named(html):
    """Assert each titled icon is an image named by its title, or hidden beside text; return the icons."""
    icons = titled_icons(html)
    assert icons, "the markup has no titled icon"
    for attrs in icons:
        if attrs.get("aria-hidden") == "true":
            continue
        assert attrs.get("role") == "img", attrs
        assert attrs.get("aria-label") == attrs["title"], attrs
    return icons


class _ModuleTabParser(HTMLParser):
    """Collect the forms, the hx-get controls and the member selects of a rendered Modules tab."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms, self.links, self.selects = [], [], []
        self.manifest = None
        self.table_attrs = {}
        self._open_forms = []
        # The named form <select> that is open, and the open <option> without a value attribute.
        self._select = self._text_option = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._open_forms.append({"action": attrs.get("action", ""), "fields": {}})
            self.forms.append(self._open_forms[-1])
        elif tag == "table" and attrs.get("id") == "librenms-module-table":
            self.table_attrs = attrs
        elif tag == "input" and attrs.get("name"):
            self._input(attrs)
        elif tag == "select":
            self._open_select(attrs)
        elif tag == "option" and self._select is not None:
            self._option(attrs)
        if attrs.get("hx-get"):
            self.links.append({"hx-get": attrs["hx-get"], "action": attrs.get("data-action", "")})

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._text_option is not None:
            self._text_option["fields"][self._text_option["name"]] += data.strip()

    def handle_endtag(self, tag):
        if tag == "form" and self._open_forms:
            self._open_forms.pop()
        elif tag == "option":
            self._text_option = None
        elif tag == "select":
            self._select = self._text_option = None

    def _input(self, attrs):
        # A browser submits a checkbox or radio only when it is checked, and "on" without a value.
        toggle = attrs.get("type") in ("checkbox", "radio")
        if self._open_forms and (not toggle or "checked" in attrs):
            self._open_forms[-1]["fields"][attrs["name"]] = attrs.get("value", "on" if toggle else "")
        if attrs.get("type") == "hidden" and attrs["name"].startswith("selection_grant_"):
            self.links.append({"selection_grant": attrs["name"], "value": attrs.get("value", "")})

    def _open_select(self, attrs):
        if attrs.get("data-module") is not None:
            self.selects.append(attrs)
        if self._open_forms and attrs.get("name"):
            self._select = {"fields": self._open_forms[-1]["fields"], "name": attrs["name"], "state": None}

    def _option(self, attrs):
        # A browser submits the first selected option, or the first option when none is selected.
        select = self._select
        state = "selected" if "selected" in attrs else "first"
        if select["state"] is None or (state == "selected" and select["state"] == "first"):
            select["fields"][select["name"]] = attrs.get("value", "")
            select["state"] = state
            self._text_option = None if "value" in attrs else select


def module_tab_forms(html):
    """Return the parsed forms, hx-get controls, member selects and table attributes of a rendered Modules tab."""
    return _ModuleTabParser(html)


def rendered_module_form(html, path_part, ent_index=None):
    """Return ``(action URL, fields)`` of the one rendered form whose URL holds *path_part* for *ent_index*."""
    matches = [
        form
        for form in module_tab_forms(html).forms
        if path_part in form["action"]
        and (
            ent_index is None or str(ent_index) in (form["fields"].get("ent_index"), form["fields"].get("parent_index"))
        )
    ]
    assert len(matches) == 1, f"expected one {path_part} form for {ent_index}, found {len(matches)}"
    return matches[0]["action"], dict(matches[0]["fields"])


def rendered_preview_url(html, ent_index):
    """Return the mismatch preview URL the rendered Replace button of row *ent_index* opens."""
    from urllib.parse import parse_qs, urlsplit

    for link in module_tab_forms(html).links:
        url = link.get("hx-get", "")
        if "module-mismatch-preview" in url and parse_qs(urlsplit(url).query).get("ent_index") == [str(ent_index)]:
            return url
    raise AssertionError(f"row {ent_index} renders no Replace button")


def rendered_preview_query(device, ent_index, *, user=None, server_key="default"):
    """Return the query that the rendered Replace button of row *ent_index* opens the mismatch preview with."""
    from urllib.parse import parse_qsl, urlsplit

    url = rendered_preview_url(render_module_tab(device, user=user, server_key=server_key), ent_index)
    return dict(parse_qsl(urlsplit(url).query))


def rendered_modal_form(device, ent_index, path_part, *, user=None, server_key="default"):
    """Return ``(action URL, fields)`` of a form in the modal that the rendered Replace button of row *ent_index* opens."""
    from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

    query = rendered_preview_query(device, ent_index, user=user, server_key=server_key)
    request = make_request("get", query, user=user)
    view = make_view(ModuleMismatchPreviewView, request, librenms_api=LibreNMSAPI(server_key=server_key))
    response = get(view, request, pk=device.pk)
    assert response.status_code == 200, response.content
    return rendered_module_form(response.content.decode(), path_part, ent_index)


# =============================================================================
# The IP tab's selection draft, through its real endpoints
# =============================================================================


def ip_tab(client, owner, *, server_key="default", **query):
    """
    Render the IP tab as the user's page shows it, from the cached snapshot.

    Returns:
        SimpleNamespace: ``html``, ``table`` (the table's attributes) and ``rows`` (each row's attributes by row key).

    """
    from types import SimpleNamespace

    from django.urls import reverse

    from netbox_librenms_plugin.tests._html_helpers import open_tags

    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk, "tab": "ipaddresses"},
    )
    response = client.get(url, {"server_key": server_key, "ipaddresses_per_page": "1000", **query})
    assert response.status_code == 200, response.content
    html = response.content.decode()
    tables = [attrs for attrs in open_tags(html, "table") if attrs.get("id") == "librenms-ipaddress-table"]
    assert len(tables) == 1, "the IP tab renders no table"
    rows = {attrs["data-row-key"]: attrs for attrs in open_tags(html, "tr") if attrs.get("data-row-key")}
    return SimpleNamespace(html=html, table=tables[0], rows=rows)


def answers_in_place(response):
    """
    Return whether *response* is the in-place answer of a sync page action to an htmx request.

    The answer carries the messages and the event that makes the page read the tab again through its
    navigation scheduler; it never sends the browser to a URL of its own, nor reloads the page.
    """
    return (
        response.status_code == 200
        and not response.has_header("HX-Redirect")
        and not response.has_header("HX-Refresh")
        and response.get("HX-Reswap") == "none"
        and "lnms-results-stale" in json.loads(response.get("HX-Trigger", "{}"))
    )


def switch_set_primary_ip(client, table, selected):
    """
    Post the Set Primary IP command of an IP tab as its page does, with the evidence of the management rows it shows.

    Args:
        client (Client): A signed-in client.
        table (dict): The attributes of the tab's draft table (``ip_tab(...).table``).
        selected (str): ``"on"`` or ``"off"``.

    Returns:
        HttpResponse: The command's response.

    """
    return client.post(
        table["data-management-url"],
        {
            "draft_id": table["data-draft-id"],
            "expected_revision": table["data-draft-revision"],
            "selected": selected,
            "evidence": table["data-management-evidence"],
        },
    )


def select_ip_rows(client, owner, row_ids, *, server_key="default", vrfs=None, **query):
    """
    Select *row_ids* in the user's IP draft as the tab does: each VRF of *vrfs* first, then one batch of checks.

    Each check carries the evidence token that the tab renders for the row with its chosen VRF.

    Args:
        client (Client): A signed-in client.
        owner (Device | VirtualMachine): The subject of the sync page.
        row_ids (Iterable[str]): The row keys to select.
        server_key (str): The LibreNMS server of the tab.
        vrfs (dict[str, str] | None): The VRF to choose per row key: "" is Global, else a VRF pk.
        **query: More page query parameters, for example ``interface_name_field``.

    Returns:
        dict[str, str]: The ``draft_id`` and ``expected_revision`` that a bulk submit posts.

    """
    tab = ip_tab(client, owner, server_key=server_key, **query)
    draft_id, revision = tab.table["data-draft-id"], int(tab.table["data-draft-revision"])
    for row_id, value in (vrfs or {}).items():
        response = client.post(
            tab.table["data-choice-url"],
            {"draft_id": draft_id, "expected_revision": revision, "row": row_id, "value": value},
        )
        assert response.status_code == 204, (response.status_code, response.content)
        revision = int(response["X-Draft-Revision"])
    if vrfs:
        tab = ip_tab(client, owner, server_key=server_key, **query)
    row_ids = list(row_ids)
    if row_ids:
        missing = [row_id for row_id in row_ids if not tab.rows.get(row_id, {}).get("data-evidence")]
        assert not missing, f"the tab offers no selection of {missing}"
        assignments = {row_id: {"state": "on", "token": tab.rows[row_id]["data-evidence"]} for row_id in row_ids}
        response = client.post(
            tab.table["data-selection-url"],
            {"draft_id": draft_id, "expected_revision": revision, "assignments": json.dumps(assignments)},
        )
        assert response.status_code == 204, (response.status_code, response.content)
        revision = int(response["X-Draft-Revision"])
    return {"draft_id": draft_id, "expected_revision": str(revision)}


def submit_ip_selection(client, owner, row_ids, *, server_key="default", vrfs=None, query=None, headers=None, **fields):
    """
    Select *row_ids* through the tab's draft (see :func:`select_ip_rows`), then post the bulk IP sync with the draft.

    Args:
        query (dict | None): More page query parameters for the tab and the submit URL.
        headers (dict | None): Extra request headers of the submit, for example ``HTTP_HX_REQUEST``.
        **fields: More submit fields, for example ``set-primary-ip-toggle``.

    Returns:
        HttpResponse: The submit's response.

    """
    from urllib.parse import urlencode

    from django.urls import reverse

    draft = select_ip_rows(client, owner, row_ids, server_key=server_key, vrfs=vrfs, **(query or {}))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk},
    )
    if query:
        url = f"{url}?{urlencode(query)}"
    return client.post(url, {"server_key": server_key, **draft, **fields}, **(headers or {}))


def sync_ip_row(client, owner, row_id, *, server_key="default", vrf=None, headers=None, **fields):
    """
    Post the row action of one IP row (``sync_one``) as its page does, after choosing *vrf* for it through the draft when given.

    Args:
        vrf (str | None): The VRF to choose first: "" is Global, else a VRF pk; None chooses nothing.
        headers (dict | None): Extra request headers of the post, for example ``HTTP_HX_REQUEST``.
        **fields: More post fields, for example ``create-missing-interfaces-toggle``.

    Returns:
        HttpResponse: The post's response.

    """
    from django.urls import reverse

    if vrf is not None:
        select_ip_rows(client, owner, [], server_key=server_key, vrfs={row_id: vrf})
    # The button posts what its page shows: the draft, its revision and the row's evidence.
    tab = ip_tab(client, owner, server_key=server_key)
    shown = {
        "draft_id": tab.table["data-draft-id"],
        "expected_revision": tab.table["data-draft-revision"],
        "evidence": tab.rows.get(row_id, {}).get("data-evidence", ""),
    }
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": owner._meta.model_name, "pk": owner.pk},
    )
    return client.post(url, {"server_key": server_key, "sync_one": row_id, **shown, **fields}, **(headers or {}))


def write_ip_rows(owner, user, selected, *, server_key="default", **fields):
    """
    Run the IP writer on *selected* as the sync does after its draft check.

    For a row that the tab offers for no selection: the writer must refuse it on its own.

    Args:
        owner (Device | VirtualMachine): The subject of the sync page, with a stored IP snapshot.
        user (User): The user who syncs.
        selected (dict[str, int | None]): The VRF pk of each row ID; None is Global.
        server_key (str): The LibreNMS server of the snapshot.
        **fields: The submit's other fields, for example ``create-missing-interfaces-toggle``.

    Returns:
        SimpleNamespace: ``results`` (the writer's results), ``messages`` (the texts the sync shows) and
            ``request`` (the request that holds the messages).

    """
    from types import SimpleNamespace

    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    request = make_request("post", {"server_key": server_key, **fields}, user=user)
    view = make_view(SyncIPAddressesView, request)
    view._post_server_key = server_key
    assessment = view.assess_cached_snapshot(owner)
    results = view.process_ip_sync(request, selected, assessment, owner, owner._meta.model_name)
    view.display_sync_results(request, results)
    return SimpleNamespace(results=results, messages=message_texts(request), request=request)


def read_ip_row(client, owner, row_id, *, server_key="default", vrf=None, **query):
    """
    Read one IP row as the page's row read does, after choosing *vrf* for it through the draft when given.

    Args:
        vrf (str | None): The VRF to choose first: "" is Global, else a VRF pk; None chooses nothing.

    Returns:
        SimpleNamespace: ``html`` (the ``<tr>``), ``status`` (the status cell's markup), ``selectable``
            (the row has an enabled checkbox) and ``shown_vrf`` (the value of the selected VRF option).

    """
    import re
    from types import SimpleNamespace

    from netbox_librenms_plugin.tests._html_helpers import open_tags

    if vrf is not None:
        select_ip_rows(client, owner, [], server_key=server_key, vrfs={row_id: vrf}, **query)
    response = client.get(ip_tab(client, owner, server_key=server_key, **query).rows[row_id]["hx-get"])
    assert response.status_code == 200, (response.status_code, response.content)
    html = response.content.decode()
    status = re.search(r'<td[^>]*data-col="status"[^>]*>(.*?)</td>', html, flags=re.S).group(1)
    boxes = [attrs for attrs in open_tags(html, "input") if attrs.get("name") == "select"]
    shown = re.search(r'<option value="(\d*)"\s+selected>', html)
    return SimpleNamespace(
        html=html,
        status=status,
        selectable=bool(boxes) and "disabled" not in boxes[0],
        shown_vrf=shown.group(1) if shown else "",
    )


def _message_level(name):
    """Map a message name to its level constant because NetBox remaps error tags to ``danger``."""
    from django.contrib import messages

    levels = {
        "debug": messages.DEBUG,
        "info": messages.INFO,
        "success": messages.SUCCESS,
        "warning": messages.WARNING,
        "error": messages.ERROR,
    }
    try:
        return levels[name.lower()]
    except (AttributeError, KeyError):
        raise ValueError(f"unknown message level {name!r}") from None


def messages_on(request):
    """Return the messages recorded on *request* as ``[(level_name, text), ...]``."""
    from django.contrib.messages import constants, get_messages

    names = {level: name.lower() for name, level in constants.DEFAULT_LEVELS.items()}
    return [(names.get(m.level, str(m.level)), str(m.message)) for m in get_messages(request)]


def message_texts(request, level=None):
    """Return the recorded message texts, optionally only those at *level* (e.g. ``"error"``)."""
    from django.contrib.messages import get_messages

    wanted = None if level is None else _message_level(level)
    return [str(m.message) for m in get_messages(request) if wanted is None or m.level == wanted]


@contextmanager
def change_logging(request):
    """Run the block as NetBox runs *request*: each save in it records a change record."""
    from netbox.context_managers import event_tracking

    request.id = uuid4()
    with event_tracking(request):
        yield


def update_change(obj):
    """Return the one change-log record of an update of *obj*."""
    from core.models import ObjectChange
    from django.contrib.contenttypes.models import ContentType

    return ObjectChange.objects.get(
        changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk, action="update"
    )


def assert_update_logged(obj, field, before, after):
    """Assert the one change-log record of the update of *obj* holds *field* (a dotted path) before and after."""
    change = update_change(obj)
    assert change.prechange_data is not None, "the update has no before-state"
    logged = []
    for data in (change.prechange_data, change.postchange_data):
        for key in field.split("."):
            data = data[key]
        logged.append(data)
    assert logged == [before, after]


def missing_pk(model, offset=1000):
    """Return a primary key that is above every current row for *model*."""
    highest_pk = model.objects.order_by("-pk").values_list("pk", flat=True).first()
    return (highest_pk or 0) + offset


def assert_locked_before_update(captured, table):
    """Verify that exactly one ``SELECT ... FOR UPDATE`` locks *table* before its update."""
    statements = [q["sql"] for q in captured.captured_queries]
    quoted_table = f'"{table.lower()}"'
    locks = [i for i, sql in enumerate(statements) if quoted_table in sql.lower() and "for update" in sql.lower()]
    updates = [i for i, sql in enumerate(statements) if sql.lower().lstrip().startswith(f"update {quoted_table}")]
    assert locks, f"{table} was never locked with SELECT ... FOR UPDATE"
    assert updates, f"{table} was never updated, so the lock ordering is untested"
    assert len(locks) == len(updates) == 1, (
        f"{table} must have exactly one lock/update pair: locks={locks} updates={updates}"
    )
    assert locks[0] < updates[0], f"{table} was updated before it was locked: locks={locks} updates={updates}"


# =============================================================================
# Real views
# =============================================================================


def make_view(view_class, request=None, *, librenms_api=None, **attrs):
    """Instantiate and bind a real view with the configured LibreNMS client."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    view = view_class()
    if librenms_api is not False:
        if librenms_api is None:
            librenms_api = LibreNMSAPI()
        view._librenms_api = librenms_api
    view.setup(request if request is not None else make_request())
    for name, value in attrs.items():
        setattr(view, name, value)
    return view
