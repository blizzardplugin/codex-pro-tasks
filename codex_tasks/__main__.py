import argparse
import os
from .common import Problem
from .runner import RunError
from .server import initialize, serve
from .worker import doctor, run


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Fila privada de tarefas para Codex CLI com login do ChatGPT.")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Cria estado e token administrativos locais.")
    init.add_argument("--state", default=".state")
    server = actions.add_parser("server", help="Abre o painel local e o coordenador.")
    server.add_argument("--config", default=".state/server.json")
    server.add_argument("--port", type=int, default=8765)
    worker = actions.add_parser("worker", help="Conecta um agente com seu login local do ChatGPT.")
    worker.add_argument("--config", required=True)
    worker.add_argument("--once", action="store_true")
    diagnostic = actions.add_parser("doctor", help="Verifica CLI e método de autenticação, sem consumir uma tarefa.")
    diagnostic.add_argument("--codex-command", default="codex")
    args = parser.parse_args()
    try:
        if args.action == "init":
            print("Configuração criada em", initialize(args.state))
        elif args.action == "server":
            serve(args.config, args.port)
        elif args.action == "worker":
            if not run(args.config, args.once): return 1
        else:
            doctor(args.codex_command)
        return 0
    except (Problem, RunError, OSError, ValueError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
