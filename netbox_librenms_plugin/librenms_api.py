import json
import logging
import math
import threading
import time
import urllib.parse
from dataclasses import dataclass

import requests
import urllib3
from django.core.cache import cache
from netbox.plugins import get_plugin_config

# HTTP request timeout constants (in seconds)
DEFAULT_API_TIMEOUT = 10
EXTENDED_API_TIMEOUT = 20  # For endpoints that may take longer (e.g., device listing)
DEFAULT_CACHE_TIMEOUT = 300

# Short-lived cache for get_device_info(). The device-info header is fetched on every sync-tab
# render (and again on the post-action redirect), but a device's identity/metadata is stable, so
# a brief cache removes a redundant synchronous LibreNMS round-trip from each render without
# meaningfully staling the displayed values.
DEVICE_INFO_CACHE_TIMEOUT = 60

# LibreNMS answers 404 for a device it does not have; every other fault is a lookup error.
HTTP_NOT_FOUND = 404

logger = logging.getLogger(__name__)


def _validate_api_url(url):
    """Require a well-formed HTTP or HTTPS URL for the LibreNMS API."""
    if not isinstance(url, str):
        raise ValueError("LibreNMS API URLs must use HTTP or HTTPS and include a host.")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("LibreNMS API URLs must use HTTP or HTTPS and include a host.")
    try:
        # urlsplit() parses the port only when it is read, so an unusable one would otherwise
        # pass here, be offered in the server picker, and fail when a request is prepared.
        _ = parsed.port
    except ValueError:
        raise ValueError("LibreNMS API URLs must use a valid port.") from None
    if "?" in url or "#" in url:
        raise ValueError("LibreNMS API URLs must not include a query or fragment.")


def librenms_id_owned_message(librenms_id):
    """Return the conflict text that names neither the owner nor its model."""
    return f"LibreNMS ID {librenms_id} is already assigned to another NetBox object."


@dataclass(frozen=True)
class LibreNMSLookupError:
    """
    Why a LibreNMS lookup failed, when the object being absent is not the reason.

    ``status_code`` is None when nothing answered at all, so a server that reported a fault
    stays distinguishable from a server that could not be reached.
    """

    message: str
    status_code: int | None = None


def configured_cache_timeout(server_key):
    """Return the cache lifetime configured for one LibreNMS server."""
    servers_config = get_plugin_config("netbox_librenms_plugin", "servers")
    timeout = None
    if isinstance(servers_config, dict):
        server_config = servers_config.get(server_key)
        if isinstance(server_config, dict):
            timeout = server_config.get("cache_timeout")
    if timeout is None:
        timeout = get_plugin_config("netbox_librenms_plugin", "cache_timeout", DEFAULT_CACHE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return DEFAULT_CACHE_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_CACHE_TIMEOUT
    return max(1, int(timeout))


def build_librenms_api(server_key):
    """
    Build a :class:`LibreNMSAPI` for a server key, tolerating bad keys.

    ``LibreNMSAPI(server_key=...)`` raises ``KeyError`` for an unknown non-default
    key and ``ValueError`` when the URL/token is missing. Views take ``server_key``
    from request POST, where a stale page or tampered request can carry a key that
    no longer exists — returning ``None`` lets the caller surface a user-facing
    error instead of an unhandled 500.

    Args:
        server_key (str): The configured LibreNMS server key to build a client for.

    Returns:
        LibreNMSAPI | None: A client for *server_key*, or None when the key is
            unknown or the server is misconfigured.

    """
    try:
        return LibreNMSAPI(server_key=server_key)
    except (KeyError, ValueError):
        return None


class LibreNMSUnreachable(Exception):
    """
    The LibreNMS server did not answer, so a run that needs it cannot continue.

    Raised by the device fetch itself. LibreNMS answers a search that matches nothing
    with 200 and an empty list, so a failed fetch never means "no devices matched". The
    message carries the reason the API reported, so a caller can show it verbatim.
    """


class _TokenScopedSession(requests.Session):
    """
    Keep ``X-Auth-Token`` on the host the request was addressed to.

    ``requests`` drops only ``Authorization`` when a redirect crosses hosts and forwards every
    other header, so a LibreNMS that redirects elsewhere would hand the API token to whatever
    answered. Redirects still follow, which a reverse proxy in front of LibreNMS may rely on.
    """

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        # requests already decides this for Authorization: it strips on a host, scheme or port
        # change, allowing only the http:80 -> https:443 upgrade. Reuse that instead of keeping a
        # second, weaker copy of the rule for our own header.
        if self.should_strip_auth(response.request.url, prepared_request.url):
            prepared_request.headers.pop("X-Auth-Token", None)


# Module-level so every call shares one connection pool, and so tests have one seam to patch.
_session = _TokenScopedSession()


# The match probes of one server that may run at once. A probe that finds no free slot is not checked, so
# nothing queues. Known limit: a server that sends its headers slowly, inside each read timeout, holds its
# slot until it stops; http.client bounds the header size, and only that server's matching degrades.
MATCH_PROBE_WORKERS = 4
# A device answer is a few KiB; a longer one is no usable answer.
MATCH_PROBE_MAX_BYTES = 1 << 20
_match_probe_slots = {}
_match_probe_slots_lock = threading.Lock()


def _probe_slots(server_url):
    """Return the probe slots of the server at *server_url*, created on first use."""
    with _match_probe_slots_lock:
        if server_url not in _match_probe_slots:
            _match_probe_slots[server_url] = threading.BoundedSemaphore(MATCH_PROBE_WORKERS)
        return _match_probe_slots[server_url]


class _ProbeAnswer:
    """The answer of one match probe, which its worker sets once."""

    def __init__(self):
        self.ready = threading.Event()
        self.value = None
        self.error = None


def _keep_redirect_body_unread(response, **kwargs):
    """Drop the redirect target, so requests does not read the body to prepare a redirect that it never follows."""
    if response.is_redirect:
        del response.headers["location"]


def _read_body(response, ends_at):
    """Return the body of a streamed response, or None when it exceeds the size cap or *ends_at* passes."""
    parts, size = [], 0
    while part := response.raw.read1(65536, decode_content=False):
        size += len(part)
        if size > MATCH_PROBE_MAX_BYTES or time.monotonic() >= ends_at:
            return None
        parts.append(part)
    return b"".join(parts)


def _match_probe(answer, slots, url, headers, timeout, verify, ends_at):
    """
    Run one match probe GET with a session of its own, set *answer*, and free the probe slot.

    The body is read undecoded until it ends, passes the size cap, or the caller's deadline *ends_at* (a
    ``time.monotonic()`` value) passes. A transport or configuration error (a missing CA bundle
    raises a plain OSError) is no answer from the server. Any other error is a bug; the caller
    raises it while it still waits.
    """
    try:
        with _TokenScopedSession() as session:
            response = session.get(
                url,
                headers=headers,
                timeout=timeout,
                verify=verify,
                allow_redirects=False,
                stream=True,
                hooks={"response": _keep_redirect_body_unread},
            )
            try:
                if 300 <= response.status_code < 400:
                    answer.value = response.status_code, None
                elif response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    # The probe asked for none; an encoded body could expand past the size cap when decoded.
                    answer.value = None, None
                elif (body := _read_body(response, ends_at)) is None:
                    answer.value = None, None
                else:
                    try:
                        answer.value = response.status_code, json.loads(body)
                    except ValueError:
                        answer.value = response.status_code, None
            finally:
                response.close()
    except (requests.exceptions.RequestException, OSError, urllib3.exceptions.HTTPError):
        answer.value = None, None
    except Exception as exc:
        # Not a transport error, so a bug: the caller raises it unread, in its own thread, before its deadline.
        # nosemgrep: caught-error-text  # noqa: ERA001
        answer.error = exc
    finally:
        answer.ready.set()
        slots.release()


class LibreNMSAPI:
    """Client to interact with the LibreNMS API and retrieve interface data for devices."""

    @staticmethod
    def _is_usable_server_config(config):
        """
        Return True only for a server mapping that ``__init__`` can bind.

        A server entry is usable only when it is a dict carrying a non-empty
        ``librenms_url`` and ``api_token``, the same fields ``__init__`` requires
        before it will build a client. Sharing this predicate keeps the server
        picker (``get_available_servers``) and the auto-default fallback from
        offering, or silently selecting, a partially configured entry that would
        immediately raise ``ValueError``.

        Args:
            config (object): The candidate server configuration.

        Returns:
            bool: True if the configuration is a usable server mapping.

        """
        if not isinstance(config, dict) or not config.get("librenms_url") or not config.get("api_token"):
            return False
        try:
            _validate_api_url(config["librenms_url"])
        except (TypeError, ValueError):
            return False
        return True

    def __init__(self, server_key=None):
        """
        Initialize LibreNMS API client with support for multiple servers.

        Args:
            server_key: Key for specific server configuration. If None, uses selected server or default.

        """
        # Track whether the caller explicitly requested a specific server. A key auto-resolved
        # from LibreNMSSettings.selected_server is NOT explicit, so a stale stored key falls back
        # to the first available server rather than hard-failing (issue #110).
        # A blank/whitespace-only string is "no key", not an explicit request: treating "" as
        # explicit would mark the auto-resolved (and possibly stale) selected_server as explicit
        # and defeat that fallback, raising KeyError instead.
        if isinstance(server_key, str):
            server_key = server_key.strip() or None
        elif server_key is not None:
            # A non-string key (e.g. a list/dict from a tampered payload) is not a valid server
            # key and is unhashable — left as-is it would raise TypeError at the `not in
            # servers_config` membership check below. Treat it as unset so it fails cleanly via
            # the same fallback path as a blank string.
            server_key = None
        explicit_server_key = server_key is not None

        # If no server_key is provided, try to get the selected server from settings
        if not server_key:
            try:
                from netbox_librenms_plugin.models import LibreNMSSettings

                settings = LibreNMSSettings.objects.first()
                if settings:
                    server_key = settings.selected_server
            except (ImportError, AttributeError):
                pass

        # Default to 'default' if still no server_key
        server_key = server_key or "default"

        # Get server configuration
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        # If the requested server_key doesn't exist but there are configured servers,
        # only fall back to the first available server when using the auto-default key.
        # If a specific (non-default) server_key was requested but not found, raise
        # immediately to avoid silently using the wrong LibreNMS instance.
        if servers_config and isinstance(servers_config, dict) and server_key not in servers_config:
            # Only fail closed for an *explicitly* requested non-default key (tampered or
            # stale-page input). An auto-resolved/default key falls back instead (issue #110).
            if explicit_server_key and server_key != "default":
                available = list(servers_config.keys())
                raise KeyError(
                    f"Server '{server_key}' not found in LibreNMS plugin configuration. Available servers: {available}"
                )
            # Skip partially configured entries so the auto-default doesn't land on a server
            # missing its url/token (which __init__ would reject below) while a later entry is
            # fully usable.
            first_key = next(
                (k for k, cfg in servers_config.items() if self._is_usable_server_config(cfg)),
                None,
            )
            # #110: a non-empty servers_config with no usable entry must surface a clear error
            # rather than silently falling through to a (possibly stale) legacy single-server
            # config. build_librenms_api() converts this ValueError into a clean None.
            if first_key is None:
                raise ValueError("No valid LibreNMS server configuration entries found.")
            logger.info(
                "Server '%s' not found in config, falling back to '%s'",
                server_key,
                first_key,
            )
            server_key = first_key

        self.server_key = server_key

        if servers_config and isinstance(servers_config, dict) and server_key in servers_config:
            # Multi-server configuration
            config = servers_config[server_key]
            # The fallback above only guards the key-not-found case; a present-but-malformed
            # entry (e.g. {"default": "not-a-dict"}) would otherwise raise an opaque TypeError
            # at the key reads below. Fail with a clear configuration error instead (issue #110).
            if not isinstance(config, dict):
                raise ValueError(
                    f"LibreNMS server '{server_key}' is misconfigured "
                    f"(expected a mapping, got {type(config).__name__})."
                )
            # Read with .get() rather than direct indexing: a dict-shaped but incomplete entry
            # (e.g. {"default": {}}) passes the isinstance check, so config["librenms_url"] would
            # raise an opaque KeyError instead of the ValueError contract callers rely on. The
            # url/token completeness is enforced by the single guard at the end of __init__.
            self.librenms_url = config.get("librenms_url")
            self.api_token = config.get("api_token")
            self.cache_timeout = configured_cache_timeout(server_key)
            self.verify_ssl = config.get("verify_ssl", True)
        else:
            # Fallback to legacy single-server configuration. Legacy mode has only the implicit
            # default server, so a stale/tampered request key (e.g. build_librenms_api("ghost"))
            # must not survive as self.server_key — it would otherwise become the cache/redirect
            # discriminator under an unconfigured value. Normalize to "default".
            self.server_key = "default"
            self.librenms_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            self.api_token = get_plugin_config("netbox_librenms_plugin", "api_token")
            self.cache_timeout = configured_cache_timeout(self.server_key)
            self.verify_ssl = get_plugin_config("netbox_librenms_plugin", "verify_ssl", True)

        if not self.librenms_url or not self.api_token:
            raise ValueError(f"LibreNMS URL or API token is not configured for server '{server_key}'.")
        _validate_api_url(self.librenms_url)

        self.headers = {"X-Auth-Token": self.api_token}

    def test_connection(self):
        """
        Test connection to LibreNMS server by calling the /system endpoint.

        Returns:
            dict: System information if successful, error dict if failed

        """
        try:
            response = _session.get(
                f"{self.librenms_url}/api/v0/system",
                headers=self.headers,
                verify=self.verify_ssl,
                timeout=DEFAULT_API_TIMEOUT,
            )

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and data.get("status") == "ok":
                    system = data.get("system")
                    if isinstance(system, list) and system and isinstance(system[0], dict):
                        return system[0]

            # Handle different HTTP status codes with user-friendly messages
            if response.status_code == 401:
                return {
                    "error": True,
                    "message": "Authentication failed - check API token",
                }
            elif response.status_code == 403:
                return {
                    "error": True,
                    "message": "Access forbidden - check API token permissions",
                }
            elif response.status_code == 404:
                return {
                    "error": True,
                    "message": "API endpoint not found - check LibreNMS URL",
                }
            elif response.status_code >= 500:
                return {
                    "error": True,
                    "message": "LibreNMS server error - check server status",
                }
            else:
                return {
                    "error": True,
                    "message": f"HTTP {response.status_code} - unexpected server response",
                }

        except requests.exceptions.SSLError:
            return {
                "error": True,
                "message": "SSL certificate verification failed - try setting verify_ssl to false",
            }
        except requests.exceptions.ConnectionError:
            return {
                "error": True,
                "message": "Connection failed - check server URL and network connectivity",
            }
        except requests.exceptions.Timeout:
            return {
                "error": True,
                "message": "Connection timeout - server may be slow or unreachable",
            }
        except requests.exceptions.RequestException as e:
            return {"error": True, "message": f"Unexpected error: {e}"}

    @classmethod
    def get_available_servers(cls):
        """
        Get list of available server configurations.

        Returns:
            dict: Dictionary of server keys and their display names

        """
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        if servers_config and isinstance(servers_config, dict):
            # Multi-server configuration
            result = {}
            for key, config in servers_config.items():
                # Only offer servers that __init__ can actually bind: a dict with a non-empty
                # librenms_url and api_token. Mirroring the constructor's validation keeps a
                # malformed (non-mapping) or partially configured entry from appearing selectable
                # and then failing the moment it is chosen.
                if not cls._is_usable_server_config(config):
                    logger.warning(
                        "Skipping unusable LibreNMS server config %r (needs a valid HTTP(S) librenms_url and a non-empty api_token).",
                        key,
                    )
                    continue
                result[key] = config.get("display_name", key)
            return result
        else:
            # Legacy single-server configuration
            legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            legacy_token = get_plugin_config("netbox_librenms_plugin", "api_token")
            legacy_config = {"librenms_url": legacy_url, "api_token": legacy_token}
            if cls._is_usable_server_config(legacy_config):
                return {"default": f"Default Server ({legacy_url})"}
            return {}

    def get_device_info(self, device_id, use_cache=True):
        """
        Fetch device information from LibreNMS using its primary IP.

        Successful lookups are cached briefly (``DEVICE_INFO_CACHE_TIMEOUT``) per
        server/device so the device-info header doesn't re-hit LibreNMS on every
        sync-tab render. Failures are never cached, so a transient error doesn't
        persist for the cache window.

        Args:
            device_id: LibreNMS device ID
            use_cache: When False, bypass the short read cache and fetch live data
                (still refreshing the cache on success). Import decisions pass False so a
                value just corrected in LibreNMS isn't read back stale within the cache window.

        Returns:
            tuple: ``(success, data)``. On success, ``data`` is the device dictionary. For an
                absent device, ``data`` is ``None``. For a transport failure
                or another non-2xx response, ``data`` is a :class:`LibreNMSLookupError`.

        """
        cache_key = f"librenms_device_info_{self.server_key}_{device_id}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            response = _session.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
        except requests.exceptions.RequestException:
            # Nothing answered, so the device's existence is unknown rather than denied.
            logger.warning("LibreNMS could not be reached for device %s", device_id, exc_info=True)
            return False, LibreNMSLookupError("The LibreNMS server could not be reached.")

        if response.status_code == HTTP_NOT_FOUND:
            # The only answer that means the device is genuinely absent, so an earlier cached
            # hit is now wrong: drop it rather than serve it until the TTL expires.
            cache.delete(cache_key)
            return False, None
        # Checked explicitly rather than through raise_for_status(), which lets a terminal 3xx
        # through and would let its body be read as a device.
        if not 200 <= response.status_code < 300:
            logger.warning("LibreNMS returned HTTP %s for device %s", response.status_code, device_id)
            return False, LibreNMSLookupError(
                f"LibreNMS answered with HTTP {response.status_code} for this device.", response.status_code
            )

        from netbox_librenms_plugin.acquisition import responses
        from netbox_librenms_plugin.acquisition.snapshot import Outcome

        try:
            body = response.json()
        except ValueError:
            return False, None
        decoded = responses.device(device_id).decode(response.status_code, body)
        if decoded.outcome is not Outcome.OBTAINED:
            return False, None
        device_data = dict(body["devices"][0])
        # LibreNMS 26.5.0 returns the full location relationship object
        # (keys: id, location, lat, lng, timestamp, fixed_coordinates)
        # instead of a flat location name string. Normalise to the name so
        # downstream consumers receive a consistent value.
        location = device_data.get("location")
        if isinstance(location, dict):
            device_data["location"] = location.get("location")
        result = (True, device_data)
        cache.set(cache_key, result, timeout=DEVICE_INFO_CACHE_TIMEOUT)
        return result

    def get_response(
        self, path: str, params: dict | None = None, *, timeout=DEFAULT_API_TIMEOUT
    ) -> tuple[int | None, object]:
        """
        Issue a GET against the LibreNMS API and return ``(status_code, body)`` verbatim.

        Unlike the typed ``get_*`` helpers this performs no shape validation or normalisation:
        it returns the parsed JSON body exactly as LibreNMS sent it, for source acquisition and the
        data-shape capture. A transport error yields ``(None, None)`` and a non-JSON body yields
        ``(status_code, None)``.

        Args:
            path (str): API path relative to ``/api/v0/`` (e.g. ``"devices/1000"``).
            params (dict | None): Optional query parameters.
            timeout (int): The request timeout in seconds.

        Returns:
            tuple: ``(status_code, body)``; the status is None when no HTTP response arrived.

        """
        url = f"{self.librenms_url}/api/v0/{path.lstrip('/')}"
        try:
            response = _session.get(
                url,
                headers=self.headers,
                params=params or {},
                timeout=timeout,
                verify=self.verify_ssl,
            )
        except requests.exceptions.RequestException:
            return None, None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    def get_response_before(self, path: str, remaining) -> tuple[int | None, object] | None:
        """
        Issue a GET that must answer before a deadline, and return ``(status_code, body)``, or None.

        ``remaining()`` returns the seconds left. The request runs in a daemon thread, and this call
        waits for it only that long: a socket timeout limits each wait for data, not the whole
        request. The thread holds one of the ``MATCH_PROBE_WORKERS`` slots of its server, and stops
        reading the body at the deadline; a probe that finds no free slot does not run. A redirect is
        not followed: its own status comes back. The probe asks for an unencoded body. A transport
        error, an encoded body, or a body over ``MATCH_PROBE_MAX_BYTES`` yields ``(None, None)``, and a
        non-JSON body ``(status_code, None)``.

        Args:
            path (str): API path relative to ``/api/v0/``.
            remaining (Callable[[], float]): The seconds left before the deadline.

        Returns:
            tuple | None: ``(status, body)``; None when the deadline passed first or no probe slot was free.

        """
        left = remaining()
        if left <= 0:
            return None
        slots = _probe_slots(self.librenms_url)
        if not slots.acquire(blocking=False):
            return None
        try:
            answer = _ProbeAnswer()
            arguments = (
                answer,
                slots,
                f"{self.librenms_url}/api/v0/{path.lstrip('/')}",
                {**self.headers, "Accept-Encoding": "identity"},
                min(DEFAULT_API_TIMEOUT, left),
                self.verify_ssl,
                time.monotonic() + left,
            )
            threading.Thread(target=_match_probe, args=arguments, name="librenms-match-probe", daemon=True).start()
        except BaseException:
            slots.release()
            raise
        # An answer after the deadline is late, also an error: the request has moved on.
        if not answer.ready.wait(timeout=left) or remaining() <= 0:
            return None
        if answer.error is not None:
            raise answer.error
        return answer.value

    def add_device(self, data):
        """
        Add a device to LibreNMS.

        Args:
            data: Dictionary containing device data including:
                - hostname: Device hostname or IP
                - snmp_version: SNMP version (v1, v2c, or v3)
                - force_add: Skip checks for duplicate device and SNMP reachability (optional, default False)
                - port: SNMP port (optional, defaults to config value)
                - transport: SNMP transport protocol (optional: udp, tcp, udp6, tcp6)
                - port_association_mode: Port identification method (optional: ifIndex, ifName, ifDescr, ifAlias)
                - poller_group: Poller group ID (optional, defaults to 0)
                - community: SNMP community string (for v1 or v2c)
                - authlevel, authname, authpass, authalgo, cryptopass, cryptoalgo: SNMP v3 parameters

        Returns:
            tuple: (success: bool, message: str)

        """
        payload = {
            "hostname": data["hostname"],
            "snmpver": data["snmp_version"],
            "force_add": data.get("force_add", False),
        }

        # Add optional common fields if provided
        if data.get("port"):
            payload["port"] = data["port"]
        if data.get("transport"):
            payload["transport"] = data["transport"]
        if data.get("port_association_mode"):
            payload["port_association_mode"] = data["port_association_mode"]
        if data.get("poller_group") is not None:
            payload["poller_group"] = data["poller_group"]

        if data["snmp_version"] in ("v1", "v2c"):
            payload["community"] = data["community"]
        elif data["snmp_version"] == "v3":
            payload["authlevel"] = data["authlevel"]
            payload["authname"] = data["authname"]
            # Credential keys only apply at the auth levels that use them. Omit
            # empty values instead of sending empty strings — LibreNMS rejects
            # those for noAuthNoPriv / authNoPriv add-device requests.
            for key in ("authpass", "authalgo", "cryptopass", "cryptoalgo"):
                value = data.get(key)
                if value:
                    payload[key] = value

        try:
            response = _session.post(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                json=payload,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                return True, "Device added successfully."
            else:
                return False, result.get("message", "Unknown error.")
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def update_device_field(self, device_id, field_data):
        """
        Update a specific field for a device in LibreNMS.

        Args:
            device_id: LibreNMS device ID
            field_data: Dictionary containing field name and value

            e.g {
                    "field": ["location", "override_sysLocation"],
                    "data": [device.site.name, "1"]

        Returns:
            tuple (success: bool, message: str)

        """
        try:
            response = _session.patch(
                f"{self.librenms_url}/api/v0/devices/{device_id}",
                headers=self.headers,
                json=field_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()

            if result.get("status") == "ok":
                return True, "Device fields updated successfully"
            else:
                return False, result.get("message", "Unknown error occurred")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def get_locations(self):
        """
        Fetch locations data from LibreNMS.

        Args:
            None

        Returns:
            tuple: (success: bool, data: dict)

        """
        try:
            response = _session.get(
                f"{self.librenms_url}/api/v0/resources/locations",
                headers=self.headers,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()

            if "locations" in result:
                return True, result["locations"]
            else:
                return False, "No locations found or unexpected response format"
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            return False, error_message

    def add_location(self, location_data):
        """
        Add a location to LibreNMS.

        Args:
            location_data: Dictionary containing location data

            e.g location_data = {
                    "location": site.name,
                    "lat": str(site.latitude),
                    "lng": str(site.longitude)
                }

        Return:
            tuple: (success: bool, message: str)

        """
        try:
            response = _session.post(
                f"{self.librenms_url}/api/v0/locations",
                headers=self.headers,
                json=location_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()
            if result.get("status") == "ok":
                location_id = result["message"].split("#")[-1]
                return True, {"id": location_id, "message": result["message"]}
            else:
                return False, result.get("message") or "Unexpected response format"
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def update_location(self, location_name, location_data):
        """
        Update a location in LibreNMS.

        Args:
            location_name: LibreNMS Location name
            location_data: Dictionary containing location data

            e.g location_data = {
                    "lat": str(site.latitude),
                    "lng": str(site.longitude)
                }

        Returns:
            tuple: (success: bool, message: str)

        """
        try:
            encoded_location_name = urllib.parse.quote(location_name, safe="")
            response = _session.patch(
                f"{self.librenms_url}/api/v0/locations/{encoded_location_name}",
                headers=self.headers,
                json=location_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                return True, result["message"]
            else:
                return False, result.get("message") or "Unexpected response format"
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def get_device_inventory(self, device_id, missing_is_empty=False):
        """Read full inventory through the shared endpoint contract."""
        from netbox_librenms_plugin.acquisition import responses
        from netbox_librenms_plugin.acquisition.snapshot import Outcome

        endpoint = responses.inventory(device_id, missing_is_empty=missing_is_empty)
        status, body = self.get_response(endpoint.request.path, dict(endpoint.request.params))
        decoded = endpoint.decode(status, body)
        return (True, decoded.value) if decoded.outcome is Outcome.OBTAINED else (False, decoded.reason)

    def get_poller_groups(self):
        """
        Fetch all poller groups from LibreNMS.

        Route: /api/v0/poller_group

        Returns:
            tuple: (success: bool, data: list)

        Example poller group:
            {
                "id": 1,
                "group_name": "test",
                "descr": "test group"
            }

        """
        try:
            response = _session.get(
                f"{self.librenms_url}/api/v0/poller_group",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("status") == "ok":
                poller_groups = result.get("get_poller_group")
                if not isinstance(poller_groups, list):
                    return False, result.get("message") or "Unexpected response format: missing 'get_poller_group' list"
                if not all(isinstance(item, dict) for item in poller_groups):
                    return False, "Unexpected response format: invalid item shape in 'get_poller_group'"
                return True, poller_groups
            if isinstance(result, dict):
                return False, result.get("message") or "Unexpected response format"
            return False, "Unexpected response format: non-object JSON"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)

    def get_inventory_filtered(
        self, device_id, ent_physical_class=None, ent_physical_contained_in=None, missing_is_empty=False
    ):
        """Read a validated inventory query and its valid empty-result fallback."""
        from netbox_librenms_plugin.acquisition import responses
        from netbox_librenms_plugin.acquisition.snapshot import Outcome

        endpoint = responses.inventory_filtered(
            device_id, ent_physical_contained_in, physical_class=ent_physical_class, missing_is_empty=missing_is_empty
        )
        status, body = self.get_response(endpoint.request.path, dict(endpoint.request.params))
        filtered = endpoint.decode(status, body)
        if filtered.outcome is not Outcome.OBTAINED:
            return False, filtered.reason
        full = None
        if responses.inventory_fallback_required(filtered, endpoint.request.params, status):
            all_endpoint = responses.inventory(device_id, missing_is_empty=missing_is_empty)
            full_status, full_body = self.get_response(all_endpoint.request.path, dict(all_endpoint.request.params))
            full = all_endpoint.decode(full_status, full_body)
            if full.outcome is not Outcome.OBTAINED:
                return False, full.reason
        return True, responses.effective_inventory(filtered, full, params=endpoint.request.params, http_status=status)

    def list_devices(self, filters=None):
        """
        List all devices from LibreNMS with optional filtering.

        Route: /api/v0/devices

        Args:
            filters (dict, optional): Filter parameters:
                - type: Device type filter (e.g., 'network', 'server', 'storage')
                - location_id: Location ID filter (numeric ID from LibreNMS)
                - hostname: Hostname filter (partial match)
                - os: Operating system filter
                - version: OS version filter
                - hardware: Hardware model filter
                - features: Features filter
                - device_id: Specific device ID
                - query: Search query (searches across multiple fields)

        Returns:
            tuple: (success: bool, data: list)

        Example device:
            {
                "device_id": 1,
                "hostname": "router01.example.com",
                "sysName": "router01",
                "ip": "192.168.1.1",
                "hardware": "Cisco C9300-48P",
                "version": "IOS 16.9.4",
                "location": "Datacenter 1",
                "status": 1,
                "status_reason": "",
                "ignore": 0,
                "disabled": 0,
                "uptime": 3153600,
                "os": "ios",
                "type": "network",
                "serial": "ABC123456789",
                "icon": "cisco.svg",
                ...
            }

        """
        try:
            params = {}
            if filters:
                # Build query parameters from filters
                for key, value in filters.items():
                    if value is not None and value != "":
                        params[key] = value

            response = _session.get(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                params=params,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("status") == "ok":
                devices = result.get("devices")
                if not isinstance(devices, list):
                    msg = result.get("message")
                    return False, msg or "Unexpected response format: missing 'devices' list"
                if not all(isinstance(item, dict) for item in devices):
                    return False, "Unexpected response format: invalid item shape in 'devices'"
                # LibreNMS 26.5.0 returns the full location relationship object
                # (keys: id, location, lat, lng, timestamp, fixed_coordinates)
                # instead of a flat location name string. Normalise each device's
                # location to the name so downstream consumers receive a consistent value.
                for device in devices:
                    location = device.get("location")
                    if isinstance(location, dict):
                        device["location"] = location.get("location")
                return True, devices

            # LibreNMS API v0 always returns JSON objects, so result is always
            # a dict here; the isinstance guard is purely defensive.
            if isinstance(result, dict):
                return False, result.get("message") or "Unexpected response format"
            return False, "Unexpected response format"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)

    # =========================================================================
    # VLAN Methods
    # =========================================================================
