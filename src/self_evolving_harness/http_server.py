"""Numeric-loopback HTTP servers without an unnecessary reverse DNS lookup."""

from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Serve local HTTP on IPv4 loopback without resolving a server name."""

    def server_bind(self):
        if self.server_address[0] != "127.0.0.1":
            raise ValueError("Local HTTP servers must bind the numeric loopback address")
        # HTTPServer.server_bind resolves even numeric hosts with getfqdn().
        # These endpoints need no DNS-derived name, and reverse lookups can
        # stall for tens of seconds on macOS before readiness is published.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
