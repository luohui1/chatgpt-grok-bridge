"""Deterministic ACP peer for cancellation, permission and conversation tests."""
import json
import sys
import threading

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

lock = threading.Lock()
active = None
permission_prompt = None
session_id = "fixture-session-1"
remembered = ""


def send(message):
    with lock:
        print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def result(request_id, value):
    send({"id": request_id, "result": value})


def update(text):
    send({"method": "session/update", "params": {"sessionId": session_id,
          "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


for line in sys.stdin:
    message = json.loads(line)
    method, params, rid = message.get("method"), message.get("params", {}), message.get("id")
    if method == "initialize":
        result(rid, {"protocolVersion": 1, "agentCapabilities": {
            "loadSession": True, "sessionCapabilities": {"resume": {}, "close": {}}},
            "authMethods": [{"id": "cached_token"}], "_meta": {"agentVersion": "fixture"}})
    elif method == "authenticate":
        result(rid, {})
    elif method in {"session/new", "session/resume", "session/load"}:
        result(rid, {"sessionId": session_id})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        if text == "crash":
            sys.exit(9)
        if text == "wait":
            active = rid
            update("Working")
        elif text == "permission":
            active = permission_prompt = rid
            send({"id": 900, "method": "session/request_permission", "params": {
                "sessionId": session_id, "toolCall": {"toolCallId": "write", "kind": "edit", "title": "Write fixture"},
                "options": [{"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                            {"optionId": "reject-once", "kind": "reject_once", "name": "Reject"}]}})
        else:
            if text.startswith("remember:"):
                remembered = text.partition(":")[2]
            update(remembered if text == "recall" else text)
            result(rid, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        if active is not None:
            result(active, {"stopReason": "cancelled"})
        active = None
        permission_prompt = None
    elif method == "session/close":
        result(rid, {})
    elif method is None and rid == 900:
        if permission_prompt is not None:
            update("Permission handled")
            result(permission_prompt, {"stopReason": "end_turn"})
            permission_prompt = active = None
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": "Unknown method"}})
