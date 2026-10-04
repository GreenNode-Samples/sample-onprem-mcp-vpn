"""The container health probe targets the address the server is bound to."""

import healthcheck
import pytest


@pytest.mark.parametrize(("env", "url"), [
    ({}, "http://127.0.0.1:8080/health"),
    ({"HOST": "", "PORT": "9000"}, "http://127.0.0.1:9000/health"),
    ({"HOST": "0.0.0.0"}, "http://127.0.0.1:8080/health"),
    ({"HOST": "::"}, "http://127.0.0.1:8080/health"),
    ({"HOST": "127.0.0.1"}, "http://127.0.0.1:8080/health"),
    ({"HOST": "192.168.10.20", "PORT": "8081"}, "http://192.168.10.20:8081/health"),
    ({"HOST": "fd00::5"}, "http://[fd00::5]:8080/health"),
])
def test_probe_url(env, url):
    assert healthcheck.probe_url(env) == url
