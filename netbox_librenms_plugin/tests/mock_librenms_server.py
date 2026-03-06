"""Minimal HTTP mock for LibreNMS API responses.

Usage in tests (add to conftest.py or inline):

    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    @pytest.fixture
    def librenms_server():
        with librenms_mock_server() as server:
            yield server
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class _LibreNMSHandler(BaseHTTPRequestHandler):
    """Request handler that dispatches to registered route responses."""

    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress request logs in tests

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        routes = self.server.routes  # type: ignore[attr-defined]
        if path in routes:
            status, body = routes[path]
            self._send_json(status, body)
        else:
            self._send_json(404, {"status": "error", "message": f"No mock for {path}"})

    def do_POST(self):
        self.do_GET()


class MockLibreNMSServer:
    """Context-manager wrapper around a simple HTTP mock server.

    Attributes:
        url (str): Base URL for the mock server (e.g. "http://127.0.0.1:PORT").
        routes (dict): Mapping of URL path → (status_code, body_dict).
    """

    def __init__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _LibreNMSHandler)
        self._server.routes = {}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        _, port = self._server.server_address
        self.url = f"http://127.0.0.1:{port}"

    def register(self, path: str, body: dict, status: int = 200):
        """Register a mock response for a URL path."""
        self._server.routes[path] = (status, body)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            import warnings

            warnings.warn(
                f"MockLibreNMSServer thread {self._thread.ident} did not exit within 5 s; "
                "socket may not be fully released",
                ResourceWarning,
                stacklevel=2,
            )

    # ------- default LibreNMS-shaped responses -------

    def add_device_response(self, device_id: int = 1, hostname: str = "test-host"):
        self.register(
            "/api/v0/devices",
            {"status": "ok", "id": device_id, "hostname": hostname},
        )

    def device_info_response(
        self,
        device_id: int = 1,
        hostname: str = "test-host",
        hardware: str = "WS-C3560X-24T-S",
        os: str = "ios",
        serial: str = "SN123",
    ):
        self.register(
            f"/api/v0/devices/{device_id}",
            {
                "status": "ok",
                "devices": [
                    {
                        "device_id": device_id,
                        "hostname": hostname,
                        "hardware": hardware,
                        "os": os,
                        "serial": serial,
                        "sysName": hostname,
                    }
                ],
            },
        )

    def ports_response(self, device_id: int = 1, ports=None):
        if ports is None:
            ports = [
                {
                    "port_id": 101,
                    "ifName": "GigabitEthernet0/1",
                    "ifDescr": "GigabitEthernet0/1",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifAdminStatus": "up",
                    "ifAlias": "uplink",
                    "ifPhysAddress": "aa:bb:cc:dd:ee:01",
                    "ifMtu": 1500,
                    "ifVlan": 1,
                    "ifTrunk": 0,
                }
            ]
        self.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": ports})

    def auth_error_response(self, path="/api/v0/devices"):
        self.register(path, {"status": "error", "message": "Authentication failed"}, status=401)


@contextmanager
def librenms_mock_server():
    """Context manager that starts and stops a MockLibreNMSServer."""
    server = MockLibreNMSServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
