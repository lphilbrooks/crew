"""Stand-in for the codex and agy CLIs, so tests exercise crew end to end without a vendor.

Usage: fake_agent.py codex|agy <real CLI arguments...>
Behaviour switches (environment): FAKE_MODE = ok | fail | touch | deny | sleep | silent | unpriced
"""
import json
import os
import sys
import time

kind, args = sys.argv[1], sys.argv[2:]
mode = os.environ.get("FAKE_MODE", "ok")

if "--version" in args:
    print(f"fake-{kind} 1.0.0")
    sys.exit(0)

if mode == "sleep":
    time.sleep(600)
if mode == "touch":
    with open("touched-by-agent.txt", "w") as f:
        f.write("x")

if kind == "codex":
    prompt = sys.stdin.read() if args and args[-1] == "-" else ""
    out = args[args.index("-o") + 1] if "-o" in args else None
    events = [
        {"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000001"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "git status"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "git status",
                                            "aggregated_output": "a\nb", "exit_code": 0}},
    ]
    answer = f"fake codex answer; argv={json.dumps(args)}; prompt_chars={len(prompt)}"
    if mode != "silent":
        events.append({"type": "item.completed", "item": {"type": "agent_message", "text": answer}})
    events.append({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                       "output_tokens": 10, "reasoning_output_tokens": 5}})
    for e in events:
        print(json.dumps(e), flush=True)
    if out and mode != "silent":
        with open(out, "w", encoding="utf-8") as f:
            f.write(answer)
    sys.exit(3 if mode == "fail" else 0)

if kind == "claude":
    prompt = sys.stdin.read()
    env_seen = {k: bool(os.environ.get(k)) for k in ("CLAUDECODE", "CREW_TASK_DIR")}
    answer = f"fake claude answer; argv={json.dumps(args)}; prompt_chars={len(prompt)}; env={json.dumps(env_seen)}"
    denials = [{"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}] if mode == "deny" else []
    events = [
        {"type": "system", "subtype": "init", "session_id": "11111111-1111-1111-1111-111111111111",
         "model": "claude-fake-9-9", "tools": ["Read"]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                                                       "input": {"file_path": "a.txt"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "one"}]}},
    ]
    if mode not in ("silent", "deny"):
        events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
    events.append({"type": "result", "subtype": "success", "is_error": mode == "fail",
                   "result": "" if mode in ("silent", "deny") else answer, "session_id": "11111111-1111-1111-1111-111111111111",
                   # Like the real CLI, a resumed session reports its whole cost so far.
                   "total_cost_usd": 0.0246 if "--resume" in args else 0.0123,
                   "usage": {"input_tokens": 50, "output_tokens": 20, "cache_creation_input_tokens": 5},
                   "modelUsage": {
                       "claude-fake-9-9": {"inputTokens": 50, "outputTokens": 20, "cacheReadInputTokens": 7,
                                           "cacheCreationInputTokens": 5, "webSearchRequests": 0, "costUSD": 0.0023,
                                           "costBasis": "unknown" if mode == "unpriced" else "list"},
                       "claude-helper-1-0": {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": 0,
                                             "cacheCreationInputTokens": 0, "webSearchRequests": 2, "costUSD": 0.01,
                                             "costBasis": "list"}},
                   "permission_denials": denials})
    for e in events:
        print(json.dumps(e), flush=True)
    sys.exit(1 if mode == "fail" else 0)

if kind == "agy":
    prompt = args[args.index("-p") + 1] if "-p" in args else ""
    if mode == "deny":
        print('jetski: no output produced - a tool required the "read_url" permission that headless mode '
              'cannot prompt for, so it was auto-denied.')
        sys.exit(0)
    if mode != "silent":
        print(f"fake agy answer; argv={json.dumps(args[:-1])}; prompt_chars={len(prompt)}")
    sys.exit(3 if mode == "fail" else 0)
