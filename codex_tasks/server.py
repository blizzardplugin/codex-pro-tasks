import hmac
import ipaddress
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .common import MAX_BODY, Problem, private_json, token
from .store import Store

WEB = Path(__file__).parent / "web"


def initialize(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = directory / "server.json"
    if config.exists():
        raise Problem("Configuração já existe; não será sobrescrita.", 409)
    private_json(config, {"admin_token": token(), "database": str((directory / "tasks.sqlite").resolve())})
    return config


class Coordinator(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, store, admin_token):
        self.store, self.admin_token = store, admin_token
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close(); return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release(); raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexProTasks/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # Do not log tokens, task prompts, query strings or worker credentials.

    def send(self, status, data, kind="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Problem("Content-Length inválido.")
        if not 0 < length <= MAX_BODY:
            raise Problem("Corpo da requisição ausente ou muito grande.", 413)
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise Problem("Use application/json.", 415)
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise Problem("JSON inválido.")
        if not isinstance(data, dict):
            raise Problem("O corpo precisa ser um objeto JSON.")
        return data

    def identity(self, worker=False):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) > 200:
            raise Problem("Autenticação necessária.", 401)
        key = header[7:]
        if worker:
            name = self.headers.get("X-Worker-ID", "")
            self.server.store.authenticate_worker(name, key)
            return name
        if not hmac.compare_digest(key, self.server.admin_token):
            raise Problem("Credencial de administrador inválida.", 401)

    def dispatch(self, method):
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            raise Problem("Não coloque credenciais ou parâmetros na URL.")
        path = parsed.path
        if method == "GET" and path in ["/", "/app.js", "/style.css"]:
            file, kind = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
            self.send(200, (WEB / file).read_bytes(), kind); return
        if path.startswith("/api/worker/"):
            name = self.identity(worker=True)
            if method != "POST":
                raise Problem("Método não permitido.", 405)
            data = self.body()
            if path == "/api/worker/claim":
                result = self.server.store.claim(name)
            elif path == "/api/worker/heartbeat":
                result = self.server.store.heartbeat(name, data.get("task"), data.get("attempt"))
            elif path == "/api/worker/complete":
                result = self.server.store.complete(name, data.get("task"), data)
            else:
                raise Problem("Endpoint não encontrado.", 404)
            self.send(200, result); return
        self.identity()
        if method == "GET" and path == "/api/state":
            self.send(200, self.server.store.snapshot()); return
        if method != "POST":
            raise Problem("Endpoint não encontrado.", 404)
        data = self.body()
        pieces = path.strip("/").split("/")
        if path == "/api/projects":
            result = self.server.store.add_project(data)
        elif path == "/api/workers":
            result = self.server.store.add_worker(data)
        elif path == "/api/tasks":
            result = {"id": self.server.store.create_task(data)}
        elif len(pieces) == 4 and pieces[:2] == ["api", "tasks"]:
            result = self.server.store.action(pieces[2], pieces[3])
        elif len(pieces) == 4 and pieces[:2] == ["api", "workers"] and pieces[3] == "resume":
            result = self.server.store.resume_worker(pieces[2])
        else:
            raise Problem("Endpoint não encontrado.", 404)
        self.send(200, result)

    def handle_method(self, method):
        try:
            self.dispatch(method)
        except Problem as error:
            self.send(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.send(500, {"error": "Erro interno. Consulte o estado local do coordenador."})

    def do_GET(self): self.handle_method("GET")
    def do_POST(self): self.handle_method("POST")


def serve(config_file, port):
    config = json.loads(Path(config_file).read_text())
    key = config.get("admin_token")
    if not isinstance(key, str) or len(key) < 32:
        raise Problem("Token administrativo ausente ou fraco.")
    server = Coordinator(("127.0.0.1", port), Store(config["database"]), key)
    print(f"Coordenador ativo na porta {server.server_port}, somente em loopback.")
    print(f"O token administrativo está no arquivo local {config_file}; não compartilhe esse token com agentes.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
