"""Local-only preparation for the official Secure MCP Tunnel client."""
import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".grok-subagent" / "chatgpt"
CONFIG = HOME / "config.json"


def download(url):
    if not url.startswith(("https://api.github.com/repos/openai/tunnel-client/",
                           "https://github.com/openai/tunnel-client/releases/download/")):
        raise ValueError("Only official OpenAI tunnel-client release URLs are accepted")
    request = urllib.request.Request(url, headers={"User-Agent": "grok-subagent-local-setup"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def install_client():
    architecture = platform.machine().lower()
    if os.name == "nt" and not architecture:
        system_info = ctypes.create_string_buffer(64)
        ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(system_info))
        architecture = {9: "amd64", 12: "arm64"}.get(int.from_bytes(system_info.raw[:2], "little"), "")
    if os.name != "nt" or architecture not in {"amd64", "x86_64"}:
        raise ValueError("This installer supports Windows x64 only; use the official client for your host")
    release = json.loads(download("https://api.github.com/repos/openai/tunnel-client/releases/latest"))
    tag = release["tag_name"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Unexpected release tag; inspect the official release before installing")
    name = f"tunnel-client-{tag}-windows-amd64.zip"
    assets = {entry["name"]: entry for entry in release["assets"]}
    manifest = download(assets["SHA256SUMS.txt"]["browser_download_url"]).decode("utf-8")
    checksums = {}
    for line in manifest.splitlines():
        fields = line.split()
        if len(fields) == 2:
            checksums[fields[1].lstrip("*")] = fields[0].lower()
    expected = checksums.get(name)
    if not expected or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Release checksum not found")
    payload = download(assets[name]["browser_download_url"])
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError("Official archive checksum verification failed")
    target = HOME / "tools" / tag
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        matches = [item for item in archive.infolist()
                   if Path(item.filename).name == "tunnel-client.exe" and not item.is_dir()]
        if len(matches) != 1 or matches[0].file_size > 250000000:
            raise ValueError("Unexpected executable in official archive")
        # Extract one named file; never trust archive paths or run embedded scripts.
        executable = target / "tunnel-client.exe"
        executable.write_bytes(archive.read(matches[0]))
    evidence = {"version": tag, "archive": name, "sha256": actual,
                "source": assets[name]["browser_download_url"],
                "verification": "SHA256 matched official release checksum; no Sigstore verification claimed"}
    (target / "download.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return {"executable": str(executable), **evidence}


def read_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def write_config(value):
    HOME.mkdir(parents=True, exist_ok=True)
    temporary = HOME / ("config-" + str(uuid.uuid4()) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, CONFIG)


def client_path():
    candidates = list((HOME / "tools").glob("v*/tunnel-client.exe"))
    if not candidates:
        raise ValueError("Run install-client first")
    return max(candidates, key=lambda path: tuple(int(part) for part in path.parent.name[1:].split(".")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("install-client")
    add = commands.add_parser("add-workspace")
    add.add_argument("alias")
    add.add_argument("path")
    add.add_argument("--allow-execution", action="store_true",
                     help="Explicitly allow Grok execution as the local account; not an OS sandbox")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--tunnel-id", required=True)
    args = parser.parse_args()
    if args.action == "init":
        if CONFIG.exists():
            result = {"config": str(CONFIG), "created": False}
        else:
            write_config({"schema_version": 1, "client_scope": "chatgpt:" + str(uuid.uuid4()),
                          "workspaces": {}})
            result = {"config": str(CONFIG), "created": True, "execution_enabled": False}
    elif args.action == "install-client":
        result = install_client()
    elif args.action == "add-workspace":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.alias):
            raise ValueError("Alias must contain only ASCII letters, digits, underscore or hyphen")
        path = Path(args.path).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Workspace must be an existing absolute directory")
        config = read_config()
        if args.alias in config["workspaces"]:
            raise ValueError("Alias already exists; review/edit the local policy file explicitly")
        config["workspaces"][args.alias] = {"path": str(path.resolve()),
                                          "allow_execution": args.allow_execution}
        write_config(config)
        result = {"alias": args.alias, "allow_execution": args.allow_execution,
                  "restart_required": True}
    elif args.action == "prepare":
        if not re.fullmatch(r"tunnel_[A-Za-z0-9]{16,80}", args.tunnel_id):
            raise ValueError("Supply the actual tunnel_id from OpenAI Platform, not a placeholder")
        read_config()
        client = client_path()
        # Forward slashes avoid backslash interpretation in the client's command parser.
        command = f'"{Path(sys.executable).as_posix()}" "{(ROOT / "scripts/chatgpt_server.py").as_posix()}"'
        result = subprocess.run([str(client), "init", "--sample", "sample_mcp_stdio_local",
                                 "--profile", "grok-subagent", "--tunnel-id", args.tunnel_id,
                                 "--health-listen-addr", "127.0.0.1:0",
                                 "--mcp-command", command], creationflags=0x08000000, timeout=30)
        raise SystemExit(result.returncode)
    else:
        config = read_config() if CONFIG.exists() else {"workspaces": {}}
        try:
            client = str(client_path())
        except ValueError:
            client = None
        result = {"config_exists": CONFIG.exists(), "client_executable": client,
                  "workspaces": [{"alias": alias, "allow_execution": entry.get("allow_execution", False)}
                                 for alias, entry in config["workspaces"].items()],
                  "runtime_key_in_environment": bool(os.environ.get("CONTROL_PLANE_API_KEY")),
                  "cloud_connection_verified": False,
                  "next_step": "Create/select a private owner-only tunnel in OpenAI Platform; no tunnel is started automatically."}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Setup refused: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
