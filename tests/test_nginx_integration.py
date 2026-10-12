"""Opt-in Docker tests: localhost high ports, temporary state, no ACME calls."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid


SOURCE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS") == "1", "set RUN_DOCKER_TESTS=1")
class NginxIntegrationTests(unittest.TestCase):
    def docker(self, *arguments, check=True):
        return subprocess.run(
            ["docker", *arguments], text=True, capture_output=True, check=check,
            timeout=120,
        )

    def port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def backend(self, label, port=0):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"app": label, "path": self.path,
                                   "host": self.headers.get("Host"),
                                   "proto": self.headers.get("X-Forwarded-Proto"),
                                   "upgrade": self.headers.get("Upgrade")}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def localize(self, config):
        # Keep every test listener on localhost and avoid production ports.
        return (config.replace("listen [::]:80", "# listen [::]:80")
                .replace("listen [::]:443", "# listen [::]:443")
                .replace("listen 80", f"listen 127.0.0.1:{self.http_port}")
                .replace("listen 443", f"listen 127.0.0.1:{self.https_port}"))

    def site(self, template, domain, backend_port=None, aliases=None):
        config = (SOURCE / "templates" / template).read_text()
        names = aliases or domain
        config = config.replace("example.com www.example.com", names)
        config = config.replace("example.com", domain)
        if backend_port is not None:
            config = config.replace("127.0.0.1:18081", f"127.0.0.1:{backend_port}")
        (self.conf / (domain + ".conf")).write_text(self.localize(config))

    def request(self, host, path="/", tls=False, headers=None):
        port = self.https_port if tls else self.http_port
        connection = socket.create_connection(("127.0.0.1", port), timeout=3)
        if tls:
            try:
                connection = self.context.wrap_socket(connection, server_hostname=host)
            except Exception:
                connection.close()
                raise
        with connection:
            fields = {"Host": host, "Connection": "close", **(headers or {})}
            request = f"GET {path} HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in fields.items()) + "\r\n"
            connection.sendall(request.encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def reload(self):
        result = self.docker("exec", self.name, "nginx", "-t", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.docker("exec", self.name, "nginx", "-s", "reload")

    def wait_for(self, host, expected_body, tls=False):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                status, _, body = self.request(host, tls=tls)
                if status == 200 and expected_body in body:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {host}: {self.docker('logs', self.name).stderr}")

    def test_site_lifecycle(self):
        temporary = tempfile.TemporaryDirectory(prefix="nginx-integration-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copytree(SOURCE / "nginx", root / "nginx")
        self.conf = root / "nginx" / "conf.d"
        self.http_port, self.https_port = self.port(), self.port()
        # Keep the ports distinct if the OS immediately reuses a released port.
        while self.https_port == self.http_port:
            self.https_port = self.port()
        default = self.conf / "00-default.conf"
        default.write_text(self.localize(default.read_text()))
        webroot = root / "www"
        challenge = webroot / ".well-known" / "acme-challenge"
        challenge.mkdir(parents=True)
        (challenge / "probe").write_text("acme-path-ok")
        certificates = root / "certificates"
        certificates.mkdir()
        static = root / "static" / "static.example.net"
        static.mkdir(parents=True)
        (static / "index.html").write_text("static-site-ok")
        (static / ".env").write_text("must-not-be-served")
        self.site("http.conf.example", "example.com",
                  aliases="example.com www.example.com")
        self.site("http.conf.example", "app.example.net")

        image = next(line.split("=", 1)[1] for line in (SOURCE / ".env.example").read_text().splitlines()
                     if line.startswith("NGINX_IMAGE="))
        self.name = "nginx-stack-test-" + uuid.uuid4().hex[:12]
        self.addCleanup(lambda: self.docker("rm", "-f", self.name, check=False))
        result = self.docker(
            "run", "--detach", "--name", self.name, "--network", "host",
            "--entrypoint", "nginx",
            "--volume", f"{root / 'nginx' / 'nginx.conf'}:/etc/nginx/nginx.conf:ro",
            "--volume", f"{self.conf}:/etc/nginx/conf.d:ro",
            "--volume", f"{root / 'nginx' / 'snippets'}:/etc/nginx/snippets:ro",
            "--volume", f"{certificates}:/etc/letsencrypt:ro",
            "--volume", f"{webroot}:/var/www/certbot:ro",
            "--volume", f"{root / 'static'}:/srv/sites:ro",
            image, "-g", "daemon off;", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.assertEqual(self.request("unknown.example")[0], 404)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail(self.docker("logs", self.name).stderr)
                time.sleep(0.05)
        self.context = ssl._create_unverified_context()
        with self.assertRaises(ssl.SSLError):
            self.request("unknown.example", tls=True)
        for domain in ("example.com", "www.example.com", "app.example.net"):
            status, _, body = self.request(domain, "/.well-known/acme-challenge/probe")
            self.assertEqual((status, body), (200, b"acme-path-ok"))
            self.assertEqual(self.request(domain, "/.well-known/acme-challenge/missing")[0], 404)

        # Temporary trusted certificates stand in for issuance; no CA is contacted.
        certificate = root / "test.pem"
        key = root / "test.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=example.com",
             "-addext", "subjectAltName=DNS:example.com,DNS:www.example.com,DNS:app.example.net,DNS:static.example.net",
             "-keyout", str(key), "-out", str(certificate)],
            capture_output=True, check=True,
        )
        self.context = ssl.create_default_context(cafile=str(certificate))
        for domain in ("example.com", "app.example.net", "static.example.net"):
            lineage = certificates / "live" / domain
            lineage.mkdir(parents=True)
            shutil.copyfile(certificate, lineage / "fullchain.pem")
            shutil.copyfile(key, lineage / "privkey.pem")
        first = self.backend("first")
        second = self.backend("second")
        self.site("https.conf.example", "example.com", first.server_port,
                  aliases="example.com www.example.com")
        self.site("https.conf.example", "app.example.net", second.server_port)
        self.site("static.conf.example", "static.example.net")
        self.reload()
        self.wait_for("example.com", b'"app": "first"', tls=True)
        self.wait_for("app.example.net", b'"app": "second"', tls=True)
        self.wait_for("static.example.net", b"static-site-ok", tls=True)
        status, headers, _ = self.request("example.com", "/path?x=1")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], "https://example.com/path?x=1")
        self.assertEqual(self.request("example.com", "/.well-known/acme-challenge/probe")[0], 200)
        status, _, body = self.request("www.example.com", "/path?x=1", tls=True,
                                       headers={"Upgrade": "websocket"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"app": "first", "path": "/path?x=1",
                                           "host": "www.example.com", "proto": "https",
                                           "upgrade": "websocket"})
        self.assertEqual(self.request("static.example.net", "/.env", tls=True)[0], 403)
        self.assertEqual(self.request("static.example.net", "/missing", tls=True)[0], 404)
        self.assertEqual(self.request("example.com", tls=True,
                                      headers={"Host": "unknown.example"})[0], 404)
        with self.assertRaises(ssl.SSLError):
            self.request("unknown.example", tls=True)

        # Redeploy on the same host port without reloading Nginx.
        first.shutdown()
        first.server_close()
        self.assertEqual(self.request("example.com", tls=True)[0], 502)
        self.assertEqual(self.request("app.example.net", tls=True)[0], 200)
        self.backend("replacement", first.server_port)
        self.wait_for("example.com", b'"app": "replacement"', tls=True)

        invalid = self.conf / "invalid.conf"
        invalid.write_text("invalid_directive;\n")
        self.assertNotEqual(self.docker("exec", self.name, "nginx", "-t", check=False).returncode, 0)
        self.assertEqual(self.request("app.example.net", tls=True)[0], 200)
        invalid.unlink()
        self.reload()


if __name__ == "__main__":
    unittest.main()
