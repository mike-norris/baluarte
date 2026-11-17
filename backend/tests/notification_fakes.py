"""Local stand-ins for an SMTP server and a chat webhook, served over TLS on 127.0.0.1.

Both present a certificate from the test CA (``pki`` in test_audit_shipping), so a client
trusting that CA connects and any other client fails verification. Nothing leaves the host.
"""

import base64
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class SmtpServer:
    """A small ESMTP server: STARTTLS or implicit TLS, AUTH PLAIN after TLS only, and it keeps
    each message's raw bytes and every command verb it was sent."""

    def __init__(
        self,
        pki: dict[str, Any],
        *,
        implicit_tls: bool = False,
        offer_starttls: bool = True,
        password: str = "",
        reject_recipients: bool = False,
    ) -> None:
        self.implicit_tls = implicit_tls
        self.offer_starttls = offer_starttls
        self.password = password
        self.reject_recipients = reject_recipients
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(pki["cert"], pki["key"])
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        self.commands: list[str] = []
        self.authenticated: list[str] = []
        self.handshake_failures = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                raw.settimeout(5)
                self._session(raw)
            except ssl.SSLError:
                self.handshake_failures += 1
            except OSError:
                pass
            finally:
                raw.close()

    def _session(self, raw: socket.socket) -> None:
        connection: socket.socket = raw
        secure = False
        if self.implicit_tls:
            connection = self.context.wrap_socket(raw, server_side=True)
            secure = True
        reader = connection.makefile("rb")

        def send(line: str) -> None:
            connection.sendall(line.encode() + b"\r\n")

        send("220 mail.example.test ESMTP")
        sender, recipients = "", []
        while True:
            line = reader.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb = text.split(" ", 1)[0].upper()
            self.commands.append(verb)
            if verb in ("EHLO", "HELO"):
                capabilities = ["mail.example.test"]
                if self.offer_starttls and not secure:
                    capabilities.append("STARTTLS")
                if secure:
                    capabilities.append("AUTH PLAIN")
                capabilities.append("8BITMIME")
                for index, capability in enumerate(capabilities):
                    last = index == len(capabilities) - 1
                    send(("250 " if last else "250-") + capability)
            elif verb == "STARTTLS" and self.offer_starttls and not secure:
                send("220 ready to start TLS")
                connection = self.context.wrap_socket(connection, server_side=True)
                reader = connection.makefile("rb")
                secure = True
            elif verb == "AUTH" and secure:
                _verb, _mechanism, data = text.split(" ", 2)
                _zid, user, password = base64.b64decode(data).decode().split("\0")
                if password == self.password:
                    self.authenticated.append(user)
                    send("235 authenticated")
                else:
                    send("535 authentication failed")
            elif verb == "MAIL":
                sender, recipients = text.split(":", 1)[1].strip(" <>"), []
                send("250 ok")
            elif verb == "RCPT":
                if self.reject_recipients:
                    send("550 no such mailbox")
                else:
                    recipients.append(text.split(":", 1)[1].strip(" <>"))
                    send("250 ok")
            elif verb == "DATA":
                send("354 end with a dot")
                lines = []
                while (chunk := reader.readline()) not in (b".\r\n", b""):
                    lines.append(chunk)
                self.messages.append(b"".join(lines))
                self.envelopes.append((sender, recipients))
                send("250 queued")
            elif verb == "QUIT":
                send("221 bye")
                return
            else:
                send("502 not implemented")

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


class WebhookServer:
    """An https endpoint that keeps each POST's path and JSON body and answers ``status``."""

    def __init__(self, pki: dict[str, Any], status: int = 200) -> None:
        received: list[tuple[str, Any]] = []
        self.received = received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.path, json.loads(self.rfile.read(length))))
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args: Any) -> None:
                pass

        class Server(HTTPServer):
            def handle_error(self, *_args: Any) -> None:
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pki["cert"], pki["key"])
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/hooks/T000/B000/0123456789abcdef") -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
