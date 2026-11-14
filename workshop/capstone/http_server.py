"""Numeric-loopback HTTP without a reverse DNS dependency."""

from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Keep local model endpoints independent of host name resolution."""

    def server_bind(self):
        if self.server_address[0] != "127.0.0.1":
            raise ValueError("Local HTTP servers must bind the numeric loopback address")
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
