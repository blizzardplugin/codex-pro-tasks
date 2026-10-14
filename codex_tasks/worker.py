import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import (HEARTBEAT_SECONDS, Problem, coordinator_url, ident)
from .runner import Runner, RunError, subscription_auth


class Client:
    def __init__(self, url, worker, token):
        self.url, self.worker, self.token = coordinator_url(url), ident(worker), token
        if not isinstance(token, str) or not 32 <= len(token) <= 100:
            raise Problem("Token do coordenador ausente ou inválido. Não use uma credencial ChatGPT aqui.")

    def post(self, endpoint, body):
        request = urllib.request.Request(self.url + endpoint, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token,
                     "X-Worker-ID": self.worker}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(256 * 1024 + 1)
                if len(data) > 256 * 1024:
                    raise RunError("Resposta do coordenador muito grande.", "lease_lost")
                return json.loads(data)
        except urllib.error.HTTPError as error:
            raise RunError(f"Coordenador recusou a operação (HTTP {error.code}).", "lease_lost")
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise RunError("Coordenador indisponível; execução não pode continuar sem heartbeat.", "lease_lost")


class LocalLock:
    def __init__(self, path): self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if self.path.stat().st_size == 0:
                self.file.write(b"\0"); self.file.flush(); self.file.seek(0)
            try: msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close(); raise Problem("Já existe um agente usando esta pasta de trabalho.")
        else:
            import fcntl
            try: fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close(); raise Problem("Já existe um agente usando esta pasta de trabalho.")
        return self

    def __exit__(self, *_):
        self.file.close()


def run(config_file, once=False):
    config = json.loads(Path(config_file).read_text())
    client = Client(config["coordinator"], config["worker_id"], config["token"])
    allowed = config.get("allowed_projects")
    if not isinstance(allowed, list) or not allowed:
        raise Problem("Configure allowed_projects explicitamente neste agente.")
    root = Path(config.get("work_dir", ".workers/" + client.worker)).resolve()
    timeout = config.get("task_timeout_seconds", 3600)
    if type(timeout) is not int or not 60 <= timeout <= 14400:
        raise Problem("Timeout do agente precisa estar entre 60 e 14400 segundos.")
    runner = Runner(root, config.get("codex_command", "codex"), timeout)
    subscription_auth(runner.codex)
    with LocalLock(root / "worker.lock"):
        while True:
            response = client.post("/api/worker/claim", {})
            task = response["task"]
            if task is None:
                if response["worker_state"] != "ready":
                    print("Agente pausado ou ocupado. Confirme o estado no painel antes de reiniciar.")
                    return False
                if once: return True
                time.sleep(3); continue
            print(f"Executando tarefa {task['id']} ({task['kind']}) neste agente.")
            done, cancelled = threading.Event(), threading.Event()
            lease = dict(task=task["id"], attempt=task["attempt"])

            def authorize():
                if cancelled.is_set():
                    raise RunError("Tentativa interrompida.", "lease_lost")
                try: client.post("/api/worker/heartbeat", lease)
                except RunError:
                    cancelled.set(); raise

            def keepalive():
                while not done.wait(HEARTBEAT_SECONDS):
                    try: authorize()
                    except RunError: return

            heartbeat = threading.Thread(target=keepalive, daemon=True)
            heartbeat.start()
            report = {**lease, "ok": False}
            try:
                if task["project"] not in allowed:
                    raise RunError("Projeto não autorizado na configuração local deste agente.")
                result = runner.execute(task, cancelled, authorize)
                authorize()
                report.update(ok=True, result=result)
            except RunError as error:
                report.update(error_kind=error.kind, error=str(error))
            except KeyboardInterrupt:
                cancelled.set()
                report.update(error_kind="execution", error="Agente interrompido pelo operador.")
            except Exception:
                report.update(error_kind="execution", error="Falha local inesperada; confira os arquivos privados da tentativa no agente.")
            finally:
                done.set(); heartbeat.join(timeout=16)
            if not cancelled.is_set():
                client.post("/api/worker/complete", report)
            print("Resultado registrado." if report["ok"] else "Tarefa interrompida; nenhum sucesso foi anunciado.")
            if once or report.get("error_kind") in ["quota", "auth", "lease_lost"] or cancelled.is_set():
                return report["ok"]


def doctor(codex="codex"):
    import shutil
    import subprocess
    for name in ["git", codex]:
        if shutil.which(name) is None:
            raise Problem(f"Instale {name} antes de iniciar o agente.")
    subscription_auth(codex)
    help_text = subprocess.run([codex, "exec", "--help"], capture_output=True, text=True, timeout=30)
    if help_text.returncode or not all(flag in help_text.stdout for flag in ["--json", "--output-schema", "--output-last-message", "--sandbox"]):
        raise Problem("Atualize o Codex CLI: as opções necessárias não estão disponíveis.")
    print("Git e Codex CLI disponíveis; autenticação ChatGPT confirmada. O plano Pro e o saldo de uso não são consultados.")
