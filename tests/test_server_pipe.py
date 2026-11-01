"""A client that leaves before the answer is not a fault of the daemon."""

import io

from commandcenter import server
from tests.harness import Case


class Gone(io.RawIOBase):
    def write(self, b):
        raise BrokenPipeError(32, "Broken pipe")


class LeftEarly(Case):
    def test_an_error_for_a_client_that_left_is_dropped_quietly(self):
        h = server.Handler.__new__(server.Handler)
        h.wfile, h.request_version, h.requestline, h.command = Gone(), "HTTP/1.1", "POST /x HTTP/1.1", "POST"
        h.headers, h.client_address = {}, ("127.0.0.1", 1)
        h.log_message = lambda *a: None
        h.fail("boom", 500)
