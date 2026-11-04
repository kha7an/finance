import http.client
import socket
import ssl
from types import SimpleNamespace

import pytest
import requests
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util.ssl_match_hostname import CertificateError

from budget_bot.telegram_api import (
    TelegramApiClient,
    _LocalDnsHTTPSConnection,
    _LocalDnsHTTPSConnectionPool,
    _LocalDnsProxyAdapter,
)


@pytest.mark.parametrize("tunnel_fails", [False, True])
def test_proxy_connect_resolves_ip_and_restores_tls_hostname(monkeypatch, tunnel_fails):
    connection = _LocalDnsHTTPSConnection("proxy.test")
    connection.set_tunnel("api.telegram.org", 443)
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "192.0.2.10")
    destinations = []

    def tunnel(self):
        destinations.append((self._tunnel_host, self._tunnel_port))
        if tunnel_fails:
            raise OSError("Tunnel rejected")

    monkeypatch.setattr(http.client.HTTPConnection, "_tunnel", tunnel)
    if tunnel_fails:
        with pytest.raises(OSError, match="Tunnel rejected"):
            connection._tunnel()
    else:
        connection._tunnel()
    assert destinations == [("192.0.2.10", 443)]
    assert connection._tunnel_host == "api.telegram.org"


@pytest.mark.parametrize("certificate_hostname", ["api.telegram.org", "wrong.test"])
def test_proxy_connect_keeps_tls_verification_and_sni(monkeypatch, certificate_hostname):
    connection = _LocalDnsHTTPSConnection("proxy.test")
    connection.set_cert(cert_reqs="CERT_REQUIRED")
    connection.set_tunnel("api.telegram.org", 443)
    sock = SimpleNamespace(
        getpeercert=lambda: {"subjectAltName": (("DNS", certificate_hostname),)},
        version=lambda: "TLSv1.3",
    )
    captured = {}
    monkeypatch.setattr(connection, "_new_conn", lambda: sock)
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "192.0.2.10")
    monkeypatch.setattr(http.client.HTTPConnection, "_tunnel", lambda self: None)

    def wrap(**kwargs):
        captured.update(kwargs)
        return sock

    monkeypatch.setattr("urllib3.connection.ssl_wrap_socket", wrap)
    if certificate_hostname != "api.telegram.org":
        with pytest.raises(CertificateError):
            connection.connect()
    else:
        connection.connect()
    assert captured["server_hostname"] == "api.telegram.org"
    assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    if certificate_hostname == "api.telegram.org":
        assert connection.is_verified


def test_proxy_adapter_does_not_change_other_sessions():
    adapter = _LocalDnsProxyAdapter()
    manager = adapter.proxy_manager_for("http://proxy.test:8080")
    assert manager.pool_classes_by_scheme["https"] is _LocalDnsHTTPSConnectionPool
    ordinary = requests.adapters.HTTPAdapter()
    assert ordinary.proxy_manager_for("http://proxy.test:8080").pool_classes_by_scheme["https"] is HTTPSConnectionPool


@pytest.mark.parametrize("proxy", ["", "socks5://proxy.test:1080", "http://proxy.test:8080"])
def test_telegram_selects_local_dns_adapter_only_for_http_proxy(proxy):
    settings = SimpleNamespace(
        telegram_bot_token="test-token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_file_base_url="https://api.telegram.org/file",
        telegram_timeout_seconds=60,
        use_env_proxy=False,
        telegram_proxy_url=proxy,
    )
    client = TelegramApiClient(SimpleNamespace(context=SimpleNamespace(settings=settings)))
    assert isinstance(client.session.get_adapter(client.base_url), _LocalDnsProxyAdapter) == proxy.startswith("http://")
