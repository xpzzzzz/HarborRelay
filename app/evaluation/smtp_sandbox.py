"""Loopback-only SMTP fault sink. Receives synthetic .invalid addresses, never relays."""

import socketserver
import threading
from contextlib import contextmanager


class SmtpSink:
    def __init__(self):
        self.mode = "healthy"
        self.messages: list[bytes] = []
        self.commands: list[str] = []
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                self.wfile.write(b"220 synthetic.invalid ESMTP sandbox\r\n")
                while True:
                    raw = self.rfile.readline(8192)
                    if not raw:
                        return
                    verb, _, argument = (
                        raw.decode("ascii", "replace").strip().partition(" ")
                    )
                    verb = verb.upper()
                    owner.commands.append(verb)
                    if verb in {"EHLO", "HELO"}:
                        response = b"250-synthetic.invalid\r\n250 AUTH PLAIN\r\n"
                    elif verb == "AUTH":
                        response = b"535 synthetic authentication rejected\r\n"
                    elif verb in {"MAIL", "RCPT"}:
                        address = (
                            argument.split(":", 1)[-1].strip().split(" ")[0].strip("<>")
                        )
                        if not address.endswith(".invalid"):
                            response = b"550 sandbox only accepts reserved synthetic addresses\r\n"
                        elif verb == "RCPT" and (
                            owner.mode == "reject"
                            or (
                                owner.mode == "partial"
                                and address.startswith("reject@")
                            )
                        ):
                            response = b"550 synthetic recipient rejected\r\n"
                        else:
                            response = b"250 accepted\r\n"
                    elif verb == "DATA":
                        self.wfile.write(b"354 end with dot\r\n")
                        body = []
                        while True:
                            line = self.rfile.readline(65536)
                            if not line or line == b".\r\n":
                                break
                            body.append(line[1:] if line.startswith(b"..") else line)
                        if owner.mode == "data_error":
                            response = b"451 synthetic transient DATA failure\r\n"
                        else:
                            owner.messages.append(b"".join(body))
                            if owner.mode == "drop_after_data":
                                return  # Accepted by sink, acknowledgment lost: ambiguous delivery.
                            response = b"250 stored in local memory only\r\n"
                    elif verb == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    elif verb in {"RSET", "NOOP"}:
                        response = b"250 ok\r\n"
                    else:
                        response = b"502 unsupported in synthetic sink\r\n"
                    self.wfile.write(response)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@contextmanager
def smtp_sink():
    sink = SmtpSink()
    try:
        yield sink
    finally:
        sink.close()
