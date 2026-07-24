import pytest

from core.refresh_mgr import RefreshManager


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Cookie: sid=one; auth=two", "sid=one; auth=two"),
        (
            [{"name": "sid", "value": "one"}, {"name": "auth", "value": "two"}],
            "sid=one; auth=two",
        ),
        ({"cookies": [{"name": "sid", "value": "one"}]}, "sid=one"),
        ({"cookie": "sid=one; auth=two"}, "sid=one; auth=two"),
        ({"cookie": {"cookie": "sid=one"}}, "sid=one"),
    ],
)
def test_cookie_string_input_formats(value, expected):
    assert RefreshManager._cookie_string_from_input(value) == expected


def test_structured_move_bundle_preserves_cookie_and_firefly_header():
    bundle = {
        "cookie": "sid=one; auth=two",
        "headers": {"X-Arp-Session-Id": "arp-session"},
    }

    assert RefreshManager._cookie_string_from_input(bundle) == "sid=one; auth=two"
    assert RefreshManager._firefly_headers_from_input(bundle) == {
        "x-arp-session-id": "arp-session"
    }


def test_cookie_input_stops_excessive_nesting():
    value = {"cookie": {"cookie": {"cookie": {"cookie": {"cookie": "sid=one"}}}}}

    assert RefreshManager._cookie_string_from_input(value) == ""
