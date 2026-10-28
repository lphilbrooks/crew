"""Stand-in for the codex, claude, agy and opencode CLIs (and a plain command-line agent), so
tests exercise crew end to end without a vendor.

Usage: fake_agent.py codex|claude|agy|opencode|plain <real CLI arguments...>
Behaviour switches (environment): FAKE_MODE = ok | fail | touch | deny | sleep | silent | unpriced | limited | build
"""
import json
import os
import sys
import time

kind, args = sys.argv[1], sys.argv[2:]
mode = os.environ.get("FAKE_MODE", "ok")
# FAKE_LIMITED=codex,claude makes those backends report a usage limit, so retries can be tested.
limited = os.environ.get("FAKE_LIMITED", "").split(",")
# claude-direct: only Claude Code on Anthropic's own service, not one crew routed to a test provider.
routed = "127.0.0.1" in os.environ.get("ANTHROPIC_BASE_URL", "")
if kind in limited or (kind == "claude" and "claude-direct" in limited and not routed):
    mode = "limited"

if "--version" in args:
    version = os.environ.get("FAKE_OPENCODE_VERSION") if kind == "opencode" else None
    print(f"fake-{kind} {version or '1.0.0'}")
    sys.exit(0)
if kind == "claude" and "--help" in args:
    print(os.environ.get("FAKE_CLAUDE_HELP", "claude [options]"))
    sys.exit(0)
if kind == "codex" and args[:3] == ["mcp", "list", "--json"]:
    listing = os.environ.get("FAKE_CODEX_MCP", '[{"name": "docs", "enabled": true}, {"name": "off", "enabled": false}]')
    if listing == "fail":
        print("error: unknown subcommand", file=sys.stderr)
        sys.exit(2)
    print(listing)
    sys.exit(0)

if mode == "sleep":
    time.sleep(600)
if mode == "build" and os.environ.get("CREW_SCRATCH"):
    target = os.path.join(os.environ["CREW_SCRATCH"], "target")
    os.makedirs(os.path.join(target, "debug", "deps"), exist_ok=True)
    with open(os.path.join(target, "debug", "deps", "big.rlib"), "wb") as f:
        f.write(b"x" * 300000)
    with open(os.path.join(target, "evidence.log"), "w") as f:
        f.write("test result: ok")
if mode == "touch":
    with open("touched-by-agent.txt", "w") as f:
        f.write("x")

if kind == "codex" and mode == "limited":
    print(json.dumps({"type": "thread.started", "thread_id": "00000000-0000-0000-0000-00000000000f"}))
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
    sys.exit(1)

if kind == "claude" and mode == "limited":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": f"Claude AI usage limit reached|{int(time.time()) + 7200}", "session_id": "l"}))
    sys.exit(1)

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
    env_seen = {k: bool(os.environ.get(k)) for k in ("CLAUDECODE", "CREW_TASK_DIR", "ANTHROPIC_AUTH_TOKEN",
                                                     "ANTHROPIC_API_KEY")}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env_seen["base_url"] = os.environ["ANTHROPIC_BASE_URL"]
    answer = os.environ.get("FAKE_ANSWER") or f"fake claude answer; argv={json.dumps(args)}; prompt_chars={len(prompt)}; env={json.dumps(env_seen)}"
    denials = [{"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}] if mode == "deny" else []
    events = [
        {"type": "system", "subtype": "init", "session_id": "11111111-1111-1111-1111-111111111111",
         "model": "claude-fake-9-9", "tools": ["Read"]},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25, "resetsAt": 4102444800},
            "seven_day": {"utilization": 0.5, "resetsAt": 4102444800}}}},
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

if kind == "opencode":
    if args[:2] == ["run", "--help"]:
        print("opencode run [message..]\n  -m, --model\n  -s, --session\n  --format\n  --variant\n  -f, --file")
        sys.exit(0)
    if args[:1] == ["models"]:
        print("openrouter/deepseek/deepseek-v4\nanthropic/claude-sonnet-5-5")
        sys.exit(0)
    prompt = sys.stdin.read()
    model = args[args.index("-m") + 1]
    session = args[args.index("-s") + 1] if "-s" in args else "ses_fake1"
    perm = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}").get("permission") or {}

    def ev(kind_, **part):
        print(json.dumps({"type": kind_, "sessionID": session, "part": part}), flush=True)

    if mode == "limited":
        print(json.dumps({"type": "error", "sessionID": session,
                          "error": {"name": "APIError", "data": {"message": "429 Too Many Requests: rate limit"}}}))
        sys.exit(1)
    ev("step_start")
    ev("tool_use", tool="read", state={"status": "completed", "input": {"filePath": "a.txt"}, "output": "one"})
    if mode == "deny":
        ev("tool_use", tool="bash", state={"status": "error", "input": {"command": "rm -rf x"},
                                           "error": "The user has specified a rule which prevents you from using "
                                                    "this specific tool call (permission denied)"})
    if mode == "fail":
        print(json.dumps({"type": "error", "sessionID": session, "error": {"name": "UnknownError",
                                                                         "data": {"message": "boom"}}}))
        sys.exit(1)
    if mode not in ("silent", "deny"):
        ev("text", text=f"fake opencode answer; model={model}; argv={json.dumps(args)}; "
                        f"prompt_chars={len(prompt)}; edit={perm.get('edit')}; bash={perm.get('bash')}; "
                        f"webfetch={perm.get('webfetch')}; pwd={os.environ.get('PWD')}; "
                        f"db={bool(os.environ.get('OPENCODE_DB'))}")
    ev("step_finish", tokens={"input": 30, "output": 12, "reasoning": 3, "cache": {"read": 5, "write": 2}},
       cost=0.0042)
    sys.exit(0)

if kind == "plain":
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    if mode == "stall":
        print("started", flush=True)
        time.sleep(600)
    if mode == "touch":
        sys.exit(0)
    print(f"fake plain answer; argv={json.dumps(args)}; stdin_chars={len(prompt)}")
    sys.exit(3 if mode == "fail" else 0)
