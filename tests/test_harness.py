"""Guards on the speed fixtures in tests/conftest.py.

A speed fixture that stops taking effect fails nothing: the suite just gets
slow again, which nobody notices until it is minutes slower. These tests
turn that into a failure.
"""

import httpx


def test_a_default_httpx_client_reuses_the_shared_tls_context(_shared_ssl_context):
    before = _shared_ssl_context.reuses

    httpx.Client().close()

    # More than one when HTTPS_PROXY adds a proxy transport beside the default.
    assert _shared_ssl_context.reuses > before


def test_a_client_with_its_own_tls_settings_gets_its_own_context(_shared_ssl_context):
    before = _shared_ssl_context.reuses

    httpx.Client(verify=False).close()

    assert _shared_ssl_context.reuses == before
