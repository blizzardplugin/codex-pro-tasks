import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path

MAX_BODY = 256 * 1024
MAX_PROMPT = 48000
MAX_SUMMARY = 16000
LEASE_SECONDS = 180
HEARTBEAT_SECONDS = 20
ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class Problem(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def ident(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise Problem("Identificador inválido.")
    return value


def text(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Problem(f"{label} precisa conter de 1 a {maximum} caracteres.")
    return value.strip()


def remote(value):
    value = text(value, 1000, "Repositório")
    if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        return value
    if Path(value).is_absolute() and "\n" not in value and "\0" not in value:
        return value  # A local repository must also exist on the worker.
    raise Problem("Use uma URL HTTPS do GitHub ou caminho absoluto de repositório local.")


def branch(value):
    value = text(value, 150, "Branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value) or any(x in value for x in ["..", "//", "@{"]) or value.endswith(("/", ".lock", ".")):
        raise Problem("Nome de branch inválido.")
    return value


def token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def matches(value, expected):
    return hmac.compare_digest(digest(value), expected)


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def commands(value):
    if not isinstance(value, list) or len(value) > 10:
        raise Problem("Configure até dez comandos de verificação.")
    for command in value:
        if not isinstance(command, list) or not 1 <= len(command) <= 32 or any(not isinstance(x, str) or not x or len(x) > 1000 or "\0" in x for x in command):
            raise Problem("Cada comando precisa ser uma lista não vazia de argumentos.")
    return value


def coordinator_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ["", "/"]:
        raise Problem("URL do coordenador inválida.")
    if parsed.scheme == "https" and parsed.hostname:
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ["localhost", "127.0.0.1", "::1"]:
        return value.rstrip("/")
    raise Problem("Use HTTPS ou HTTP em localhost por túnel SSH. TLS não pode ser desativado.")


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"tasks"} or not isinstance(plan["tasks"], list) or not 1 <= len(plan["tasks"]) <= 8:
        raise Problem("O plano precisa ter entre uma e oito tarefas.")
    normalized = []
    for index, task in enumerate(plan["tasks"]):
        if not isinstance(task, dict) or set(task) != {"title", "prompt", "depends_on"}:
            raise Problem("Formato de tarefa do plano inválido.")
        deps = task["depends_on"]
        if not isinstance(deps, list) or any(type(n) is not int or not 0 <= n < index for n in deps) or len(set(deps)) != len(deps):
            raise Problem("Dependências devem apontar somente para tarefas anteriores.")
        normalized.append(dict(title=text(task["title"], 160, "Título"),
                               prompt=text(task["prompt"], MAX_PROMPT, "Instrução"), depends_on=deps))
    return {"tasks": normalized}


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tasks"],
    "properties": {"tasks": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "prompt", "depends_on"],
        "properties": {"title": {"type": "string"}, "prompt": {"type": "string"},
                       "depends_on": {"type": "array", "items": {"type": "integer"}}}}}}
}
