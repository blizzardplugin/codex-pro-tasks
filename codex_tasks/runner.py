import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .common import PLAN_SCHEMA, MAX_SUMMARY, Problem, private_json, validate_plan


class RunError(Exception):
    def __init__(self, message, kind="execution"):
        self.kind = kind
        super().__init__(message)


def stop_process(process):
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        # End background descendants of this command, without touching shared CLI daemons.
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    process.wait(timeout=15)


def subscription_auth(codex):
    # Login status identifies auth mode. It cannot prove Pro tier or remaining quota.
    if any(os.environ.get(k) for k in ["OPENAI_API_KEY", "CODEX_API_KEY"]):
        raise RunError("Há uma chave de API no ambiente. Remova-a neste agente antes de usar o modo por assinatura.", "auth")
    try:
        result = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RunError("Codex CLI ausente ou diagnóstico de login indisponível.", "auth")
    output = (result.stdout + result.stderr).lower()
    if result.returncode or "chatgpt" not in output or "api key" in output:
        raise RunError("Faça codex login pelo ChatGPT neste usuário de sistema. Login de API não é aceito.", "auth")


class Runner:
    def __init__(self, root, codex="codex", timeout=3600):
        self.root, self.codex, self.timeout = Path(root).resolve(), codex, timeout
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def command(self, args, directory, log, cancelled, timeout=180, input_text=None):
        log = Path(log)
        options = dict(cwd=directory, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(args, **options)
        except OSError:
            raise RunError(f"Não foi possível iniciar {Path(args[0]).name}; verifique a instalação local.")
        log.touch(mode=0o600, exist_ok=True)
        collected, overflow = bytearray(), threading.Event()

        def drain():
            with log.open("wb") as f:
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk: break
                    if f.tell() + len(chunk) > 10 * 1024 * 1024:
                        overflow.set(); break
                    f.write(chunk)
                    collected.extend(chunk)
                    if len(collected) > 64000:
                        del collected[:-64000]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        start = time.monotonic()
        writer = None
        if input_text is not None:
            def feed():
                try:
                    process.stdin.write(input_text.encode())
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        try:
            while process.poll() is None:
                if cancelled.is_set():
                    raise RunError("Execução interrompida: a tentativa perdeu sua autorização ou conexão.", "lease_lost")
                if overflow.is_set():
                    raise RunError("O processo ultrapassou o limite de log privado de 10 MiB.")
                if time.monotonic() - start > timeout:
                    raise RunError("Tempo máximo da execução atingido.", "timeout")
                time.sleep(0.1)
        finally:
            stop_process(process)
            reader.join(timeout=10)
            if writer: writer.join(timeout=5)
            process.stdout.close()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        return process.returncode, collected.decode("utf-8", errors="replace")

    def checked(self, args, directory, log, cancelled, timeout=180):
        code, output = self.command(args, directory, log, cancelled, timeout)
        if code:
            raise RunError(f"{Path(args[0]).name} falhou; diagnóstico privado salvo no agente em {Path(log).name}.")
        return output.strip()

    def execute(self, task, cancelled, authorize):
        subscription_auth(self.codex)
        project = task["project_config"]
        job = self.root / task["id"] / task["attempt"]
        if job.exists():
            raise RunError("Diretório da tentativa já existe; não será reutilizado nem sobrescrito.")
        job.mkdir(parents=True, mode=0o700)
        repo = job / "repo"
        self.checked(["git", "-c", "protocol.ext.allow=never", "clone", "--quiet", "--no-hardlinks", "--single-branch", "--branch", project["base"], "--", project["remote"], str(repo)],
                     job, job / "clone.log", cancelled)
        task_branch = f"codex-tasks/{task['id']}-{task['attempt'][:8]}"
        self.checked(["git", "checkout", "-b", task_branch], repo, job / "branch.log", cancelled)
        for index, dep in enumerate(task["dependencies"]):
            if not dep.get("changed"): continue
            ref = f"dependency-{index}"
            self.checked(["git", "fetch", "origin", f"{dep['branch']}:refs/remotes/origin/{ref}"],
                         repo, job / f"dependency-{index}.log", cancelled)
            self.checked(["git", "-c", "user.name=Codex Task Worker", "-c", "user.email=codex-worker@localhost", "merge", "--no-edit", f"refs/remotes/origin/{ref}"],
                         repo, job / f"merge-{index}.log", cancelled)
        base_commit = self.checked(["git", "rev-parse", "HEAD"], repo, job / "base.log", cancelled)
        prompt = ("Leia AGENTS.md deste projeto, se existir. Trabalhe somente neste checkout e não publique nem faça merge na branch principal. "
                  "Não leia nem copie credenciais e não altere login. O executor fará os testes, commit e publicação solicitada.\n\n"
                  + project.get("instructions", "") + "\n\nObjetivo: " + task["prompt"])
        args = [self.codex, "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                "--ask-for-approval", "never", "exec", "--sandbox", "read-only" if task["kind"] == "plan" else "workspace-write",
                "--json", "-C", str(repo), "--output-last-message", str(job / "result.txt")]
        if task["kind"] == "plan":
            schema = job / "plan-schema.json"
            private_json(schema, PLAN_SCHEMA)
            args += ["--output-schema", str(schema)]
            prompt += ("\nRetorne um plano JSON com no máximo oito tarefas concretas. Cada tarefa tem title, prompt, depends_on. "
                       "As dependências são índices iniciados em zero e apontam só para tarefas anteriores. Não modifique arquivos.")
        args.append("-")
        code, events = self.command(args, repo, job / "codex.jsonl", cancelled, self.timeout, prompt)
        if code:
            indicators = events.lower()
            if any(s in indicators for s in ["rate limit", "usage limit", "quota", "rate_limit_exceeded", "limite de uso"]):
                raise RunError("Limite da assinatura atingido. Agente pausado; tarefa não será enviada automaticamente para outra conta.", "quota")
            raise RunError("Codex terminou sem sucesso. Consulte codex.jsonl no agente; nenhuma publicação foi feita.")
        if not (job / "result.txt").exists():
            raise RunError("Codex não produziu a mensagem final esperada.")
        if (job / "result.txt").stat().st_size > 256000:
            raise RunError("Resultado final excedeu o limite permitido.")
        result = (job / "result.txt").read_text(encoding="utf-8")
        if len(result) > 128000:
            raise RunError("Resultado do Codex excedeu o limite permitido.")
        if task["kind"] == "plan":
            try: plan = validate_plan(json.loads(result))
            except (ValueError, Problem): raise RunError("Codex retornou um plano inválido; verifique o resultado privado.")
            if not task["publish"] and any(item["depends_on"] for item in plan["tasks"]):
                raise RunError("Plano com dependências precisa publicar branches para os próximos agentes. Ative publicação ou peça tarefas independentes.")
            return {"plan": plan, "summary": f"Plano com {len(plan['tasks'])} tarefas."}
        verification = []
        for index, command in enumerate(project.get("verify", [])):
            authorize()
            status, _ = self.command(command, repo, job / f"verify-{index}.log", cancelled, self.timeout)
            verification.append({"command": command, "passed": status == 0})
            if status:
                raise RunError(f"Verificação {index + 1} falhou; checkout mantido no agente e nenhuma publicação feita.", "verification")
        current_branch = self.checked(["git", "branch", "--show-current"], repo, job / "current-branch.log", cancelled)
        if current_branch != task_branch:
            raise RunError("O agente mudou a branch da tentativa; publicação bloqueada.")
        self.checked(["git", "add", "--all"], repo, job / "stage.log", cancelled)
        staged = self.checked(["git", "diff", "--cached", "--name-only"], repo, job / "staged-files.log", cancelled)
        for filename in staged.splitlines():
            path = Path(filename)
            if path.name in ["auth.json", ".env", "worker-config.json"] or path.suffix in [".p12", ".token", ".mobileprovision"] or ".codex" in path.parts:
                raise RunError("A tentativa incluiu um arquivo de credenciais ou configuração privada; publicação bloqueada.")
        pending = self.checked(["git", "status", "--porcelain"], repo, job / "status.log", cancelled)
        if pending:
            self.checked(["git", "-c", "user.name=Codex Task Worker", "-c", "user.email=codex-worker@localhost", "commit", "-m", task["title"]],
                         repo, job / "commit.log", cancelled)
        commit = self.checked(["git", "rev-parse", "HEAD"], repo, job / "head.log", cancelled)
        changed = commit != base_commit
        answer = dict(summary=result[:MAX_SUMMARY], branch=task_branch, commit=commit, changed=changed,
                      published=False, pull_request=None, verification=verification)
        if changed and task["publish"]:
            authorize()
            self.checked(["git", "push", "origin", f"HEAD:refs/heads/{task_branch}"], repo, job / "push.log", cancelled)
            answer["published"] = True
            if project["remote"].startswith("https://github.com/"):
                pr_body = job / "pr-body.md"
                pr_body.write_text(f"Tarefa {task['id']} · agente {task['worker']}\n\n{result[:12000]}\n\nVerificações: {len(verification)} comandos configurados.\n", encoding="utf-8")
                authorize()
                output = self.checked(["gh", "pr", "create", "--draft", "--base", project["base"], "--head", task_branch,
                                       "--title", task["title"], "--body-file", str(pr_body)],
                                      repo, job / "pull-request.log", cancelled)
                urls = [line for line in output.splitlines() if line.startswith("https://github.com/")]
                answer["pull_request"] = urls[-1] if urls else None
        return answer
