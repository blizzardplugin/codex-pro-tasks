import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .common import (Problem, LEASE_SECONDS, MAX_PROMPT, MAX_SUMMARY, branch,
                     commands, digest, ident, matches, remote, text, token, validate_plan)


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, label TEXT NOT NULL,
              secret TEXT NOT NULL, projects TEXT NOT NULL, state TEXT NOT NULL,
              seen REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL,
              title TEXT NOT NULL, prompt TEXT NOT NULL, kind TEXT NOT NULL,
              dependencies TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
              worker TEXT, attempt TEXT, lease REAL, result TEXT, error TEXT,
              affinity TEXT, publish INTEGER NOT NULL DEFAULT 0,
              auto_apply INTEGER NOT NULL DEFAULT 0, applied INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(state, created);
            ''')
        Path(path).chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def add_project(self, data):
        name = ident(data.get("id"))
        item = dict(id=name, remote=remote(data.get("remote")), base=branch(data.get("base", "main")),
                    verify=commands(data.get("verify", [])),
                    instructions=str(data.get("instructions", ""))[:16000])
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM projects WHERE id=?", (name,)).fetchone():
                raise Problem("Projeto já cadastrado.", 409)
            db.execute("INSERT INTO projects VALUES (?,?)", (name, json.dumps(item)))
        return item

    def add_worker(self, data):
        name, label = ident(data.get("id")), text(data.get("label", data.get("id")), 120, "Nome do agente")
        projects = data.get("projects")
        if not isinstance(projects, list) or not 1 <= len(projects) <= 32 or any(not isinstance(p, str) for p in projects):
            raise Problem("Selecione os projetos autorizados para o agente.")
        key = token()
        with self.transaction() as db:
            known = {r[0] for r in db.execute("SELECT id FROM projects")}
            if not set(projects) <= known:
                raise Problem("Projeto desconhecido.")
            if db.execute("SELECT 1 FROM workers WHERE id=?", (name,)).fetchone():
                raise Problem("Agente já cadastrado.", 409)
            db.execute("INSERT INTO workers(id,label,secret,projects,state,seen) VALUES(?,?,?,?,?,?)",
                       (name, label, digest(key), json.dumps(projects), "ready", 0))
        return dict(id=name, token=key, projects=projects)

    def authenticate_worker(self, name, key):
        with self.connection() as db:
            row = db.execute("SELECT secret FROM workers WHERE id=?", (name,)).fetchone()
            if not row or not matches(key, row[0]):
                raise Problem("Credencial do agente inválida.", 401)

    def create_task(self, data):
        project = ident(data.get("project"))
        kind = data.get("kind", "code")
        if kind not in ["code", "plan"]:
            raise Problem("Tipo de tarefa inválido.")
        title, prompt = text(data.get("title"), 160, "Título"), text(data.get("prompt"), MAX_PROMPT, "Objetivo")
        deps = data.get("dependencies", [])
        if not isinstance(deps, list) or len(deps) > 8 or any(not isinstance(d, str) for d in deps):
            raise Problem("Dependências inválidas.")
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                raise Problem("Projeto desconhecido.")
            for dep in deps:
                row = db.execute("SELECT project, kind FROM tasks WHERE id=?", (dep,)).fetchone()
                if not row or row[0] != project or row[1] != "code":
                    raise Problem("Dependência precisa ser uma tarefa de código do mesmo projeto.")
            return self._insert(db, project, title, prompt, kind, deps,
                                bool(data.get("publish", False)), bool(data.get("auto_apply", True)))

    def _insert(self, db, project, title, prompt, kind, deps, publish=False, auto_apply=False):
        if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= 10000:
            raise Problem("A fila atingiu o limite de dez mil tarefas.", 409)
        task_id = secrets.token_hex(8)
        db.execute('''INSERT INTO tasks(id,project,title,prompt,kind,dependencies,state,created,publish,auto_apply)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (task_id, project, title, prompt, kind, json.dumps(deps), "queued", self.clock(), publish, auto_apply))
        return task_id

    def _expire(self, db):
        expired = db.execute("SELECT id,worker FROM tasks WHERE state='running' AND lease<?", (self.clock(),)).fetchall()
        for row in expired:
            db.execute("UPDATE tasks SET state='stalled',error='Agente sem heartbeat; retomada manual.',affinity=worker WHERE id=?", (row[0],))
            db.execute("UPDATE workers SET state='paused',note='Heartbeat interrompido.' WHERE id=?", (row[1],))

    def claim(self, worker):
        with self.transaction() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM workers WHERE id=?", (worker,)).fetchone()
            if not row:
                raise Problem("Agente desconhecido.", 404)
            db.execute("UPDATE workers SET seen=? WHERE id=?", (self.clock(), worker))
            if row["state"] != "ready":
                return {"task": None, "worker_state": row["state"]}
            allowed = set(json.loads(row["projects"]))
            queued = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created,id").fetchall()
            for task in queued:
                if task["project"] not in allowed or task["affinity"] not in [None, worker]:
                    continue
                deps = []
                ready = True
                for dep in json.loads(task["dependencies"]):
                    parent = db.execute("SELECT state,result FROM tasks WHERE id=?", (dep,)).fetchone()
                    if not parent or parent["state"] != "done":
                        ready = False; break
                    result = json.loads(parent["result"] or "{}")
                    if result.get("changed") and not result.get("published"):
                        ready = False; break
                    deps.append(dict(id=dep, **result))
                if not ready:
                    continue
                attempt = secrets.token_hex(12)
                db.execute("UPDATE tasks SET state='running',worker=?,attempt=?,lease=?,error=NULL WHERE id=?",
                           (worker, attempt, self.clock() + LEASE_SECONDS, task["id"]))
                db.execute("UPDATE workers SET state='busy',note='' WHERE id=?", (worker,))
                item = dict(task)
                item.update(attempt=attempt, worker=worker, state="running")
                item["dependencies"] = deps
                item["project_config"] = json.loads(db.execute("SELECT data FROM projects WHERE id=?", (task["project"],)).fetchone()[0])
                return {"task": item, "worker_state": "busy"}
            return {"task": None, "worker_state": "ready"}

    def _owned(self, db, worker, task_id, attempt):
        self._expire(db)
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["worker"] != worker or task["attempt"] != attempt or task["state"] != "running":
            raise Problem("Tentativa cancelada, expirada ou pertencente a outro agente.", 409)
        return task

    def heartbeat(self, worker, task_id, attempt):
        with self.transaction() as db:
            self._owned(db, worker, task_id, attempt)
            db.execute("UPDATE tasks SET lease=? WHERE id=?", (self.clock() + LEASE_SECONDS, task_id))
            db.execute("UPDATE workers SET seen=? WHERE id=?", (self.clock(), worker))
        return {"ok": True}

    def complete(self, worker, task_id, data):
        result = data.get("result", {})
        if not isinstance(result, dict) or len(json.dumps(result)) > 128000:
            raise Problem("Resultado inválido ou muito grande.")
        with self.transaction() as db:
            task = self._owned(db, worker, task_id, data.get("attempt"))
            ok = data.get("ok") is True
            error_kind = data.get("error_kind", "execution")
            if ok and task["kind"] == "plan":
                result = {"plan": validate_plan(result.get("plan")), "summary": str(result.get("summary", ""))[:MAX_SUMMARY]}
            if ok and task["kind"] == "code":
                import re
                expected = f"codex-tasks/{task_id}-{task['attempt'][:8]}"
                if result.get("changed") and (result.get("branch") != expected or not isinstance(result.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", result["commit"])):
                    raise Problem("Resultado com alterações precisa identificar a branch e commit desta tentativa.")
                if result.get("published") and not result.get("changed"):
                    raise Problem("Resultado de publicação inconsistente.")
                result["summary"] = str(result.get("summary", ""))[:MAX_SUMMARY]
            state = "done" if ok else "blocked_quota" if error_kind == "quota" else "failed"
            db.execute("UPDATE tasks SET state=?,result=?,error=?,affinity=? WHERE id=?",
                       (state, json.dumps(result), None if ok else str(data.get("error", "Execução falhou."))[:1500],
                        worker if error_kind == "quota" and not ok else task["affinity"], task_id))
            paused = not ok and error_kind in ["quota", "auth"]
            db.execute("UPDATE workers SET state=?,seen=?,note=? WHERE id=?",
                       ("paused" if paused else "ready", self.clock(),
                        "Limite da assinatura ou autenticação; retomada explícita." if paused else "", worker))
            if ok and task["kind"] == "plan" and task["auto_apply"]:
                self._apply_plan(db, task_id)
        return {"ok": True}

    def _apply_plan(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["kind"] != "plan" or task["state"] != "done" or task["applied"]:
            raise Problem("Plano indisponível ou já aplicado.", 409)
        plan = validate_plan(json.loads(task["result"])["plan"])
        created = []
        for item in plan["tasks"]:
            deps = [created[index] for index in item["depends_on"]]
            created.append(self._insert(db, task["project"], item["title"], item["prompt"], "code", deps, bool(task["publish"])))
        db.execute("UPDATE tasks SET applied=1 WHERE id=?", (task_id,))
        return created

    def action(self, task_id, action):
        with self.transaction() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise Problem("Tarefa não encontrada.", 404)
            if action == "apply-plan":
                return {"tasks": self._apply_plan(db, task_id)}
            if action == "cancel":
                if row["state"] in ["done", "cancelled"]:
                    raise Problem("Tarefa já encerrada.", 409)
                db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task_id,))
                if row["worker"]:
                    db.execute("UPDATE workers SET state='paused',note='Tarefa cancelada; confirme parada antes de retomar.' WHERE id=?", (row["worker"],))
            elif action == "retry":
                if row["state"] not in ["failed", "stalled", "blocked_quota"]:
                    raise Problem("Somente tarefas interrompidas podem ser retomadas.", 409)
                db.execute("UPDATE tasks SET state='queued',attempt=NULL,lease=NULL,affinity=coalesce(affinity,worker) WHERE id=?", (task_id,))
            else:
                raise Problem("Ação desconhecida.", 404)
        return {"ok": True}

    def resume_worker(self, worker):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM tasks WHERE worker=? AND state='running'", (worker,)).fetchone():
                raise Problem("Agente ainda tem uma tarefa ativa.", 409)
            if not db.execute("SELECT 1 FROM workers WHERE id=?", (worker,)).fetchone():
                raise Problem("Agente desconhecido.", 404)
            db.execute("UPDATE workers SET state='ready',note='' WHERE id=?", (worker,))
        return {"ok": True}

    def snapshot(self):
        with self.transaction() as db:
            self._expire(db)
            projects = [json.loads(r[0]) for r in db.execute("SELECT data FROM projects ORDER BY id")]
            workers = [dict(r) for r in db.execute("SELECT id,label,projects,state,seen,note FROM workers ORDER BY id")]
            tasks = [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC,id DESC LIMIT 250")]
            counts = {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM tasks GROUP BY state")}
            for w in workers:
                w["projects"] = json.loads(w["projects"])
                w["online"] = self.clock() - w["seen"] < LEASE_SECONDS
            for task in tasks:
                task["dependencies"] = json.loads(task["dependencies"])
                task["result"] = json.loads(task["result"]) if task["result"] else None
                task.pop("attempt", None)
        return dict(projects=projects, workers=workers, tasks=tasks, counts=counts, time=self.clock())
