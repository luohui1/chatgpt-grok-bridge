"""Diagnostic CLI using the same functions as MCP. Args live in a UTF-8 JSON file."""
import argparse
import json
from pathlib import Path

import server

parser = argparse.ArgumentParser()
parser.add_argument("tool", choices=[t["name"] for t in server.TOOLS])
parser.add_argument("--args-file")
options = parser.parse_args()
args = json.loads(Path(options.args_file).read_text(encoding="utf-8-sig")) if options.args_file else {}
result = server.invoke(options.tool, args)
print(json.dumps(result, ensure_ascii=True, indent=2))
