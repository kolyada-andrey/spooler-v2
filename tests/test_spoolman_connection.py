"""spoolman.py — test_spoolman_connection() and the basic-auth header helper.

Network is always mocked here -- test_spoolman_connection() was separately
verified by hand against a real running Spoolman instance (confirmed the
real API: GET /api/v1/health -> {"status": "healthy"}), but these automated
tests must never make a real network call.
"""

import urllib.error
from unittest.mock import MagicMock, patch

import config
import spoolman


def test_auth_header_empty_when_no_user_configured():
    assert spoolman.spoolman_auth_header() == {}


def test_auth_header_set_when_user_configured():
    config.set("spoolman.auth_user", "admin")
    config.set("spoolman.auth_pass", "secret")
    headers = spoolman.spoolman_auth_header()
    assert headers["Authorization"].startswith("Basic ")


def test_spoolman_request_attaches_auth_header():
    config.set("spoolman.auth_user", "admin")
    config.set("spoolman.auth_pass", "secret")
    req = spoolman._spoolman_request("http://localhost:7912/api/v1/health")
    assert req.get_header("Authorization", "").startswith("Basic ")


def test_spoolman_request_sets_content_type_only_with_body():
    req_no_body = spoolman._spoolman_request("http://x/api")
    assert req_no_body.get_header("Content-type") is None
    req_with_body = spoolman._spoolman_request("http://x/api", method="POST", body=b"{}")
    assert req_with_body.get_header("Content-type") == "application/json"


@patch("spoolman.urllib.request.urlopen")
def test_test_connection_success(mock_urlopen):
    mock_urlopen.return_value.__enter__.return_value = MagicMock()
    result = spoolman.test_spoolman_connection()
    assert result["ok"] is True
    assert "ms" in result["message"]


@patch("spoolman.urllib.request.urlopen")
def test_test_connection_http_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "http://localhost:7912/api/v1/health", 401, "Unauthorized", {}, None
    )
    result = spoolman.test_spoolman_connection()
    assert result["ok"] is False
    assert "401" in result["message"]


@patch("spoolman.urllib.request.urlopen")
def test_test_connection_network_error(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError("timed out")
    result = spoolman.test_spoolman_connection()
    assert result["ok"] is False
    assert "Could not reach" in result["message"]
