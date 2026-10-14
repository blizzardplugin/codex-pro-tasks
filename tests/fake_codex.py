#!/usr/bin/env python3
"""Offline test double. Does not contact OpenAI or use a subscription."""
import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT" if os.environ.get("TASK_TEST_AUTH", "chatgpt") == "chatgpt" else "Logged in using an API key")
    raise SystemExit(0)
if "--help" in args:
    print("--json --sandbox --output-last-message --output-schema")
    raise SystemExit(0)
assert 'forced_login_method="chatgpt"' in args
assert 'model_provider="openai"' in args
assert args[args.index("--ask-for-approval") + 1] == "never"
prompt = sys.stdin.read()
output = Path(args[args.index("--output-last-message") + 1])
mode = os.environ.get("TASK_TEST_MODE", "code")
if mode == "quota":
    print(json.dumps({"type": "error", "message": "Usage limit reached"}))
    raise SystemExit(1)
if mode == "slow":
    import time
    time.sleep(60)
if "--output-schema" in args:
    assert args[args.index("--sandbox") + 1] == "read-only"
    result = {"tasks": [{"title": "Escrever parte A", "prompt": "TASK_FILE=part_a.txt", "depends_on": []},
                        {"title": "Completar parte B", "prompt": "TASK_FILE=part_b.txt", "depends_on": [0]}]}
    output.write_text(json.dumps(result))
else:
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    repository = Path(args[args.index("-C") + 1])
    match = re.search(r"TASK_FILE=([a-zA-Z0-9_.-]+)", prompt)
    name = match[1] if match else "implemented.txt"
    (repository / name).write_text("implemented by offline test double\n")
    output.write_text("Implementação de teste concluída.\n")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}))
