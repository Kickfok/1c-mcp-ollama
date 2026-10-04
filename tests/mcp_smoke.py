"""Smoke-тест MCP-сервера 1С: health, initialize, tools/list, resources, вызовы инструментов.

    python tests/mcp_smoke.py http://localhost/<публикация>/hs/mcp

Адрес можно задать и переменной окружения MCP_URL. Тест только читает данные:
вызываемые инструменты не изменяют информационную базу.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_URL", "")).rstrip("/")
if not BASE:
    sys.exit(__doc__)
sys.stdout.reconfigure(encoding="utf-8")
_id = 0


def rpc(method, params=None, notify=False):
    global _id
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        _id += 1
        body["id"] = _id
    req = urllib.request.Request(BASE + "/", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, {"error": {"code": f"HTTP {e.code}", "message": raw or e.reason}}


def short(text, n=160):
    text = str(text).replace("\n", " | ")
    return text if len(text) <= n else text[:n] + "..."


with urllib.request.urlopen(BASE + "/health", timeout=30) as r:
    print("health:", r.status, r.read().decode())

print("initialize:", short(rpc("initialize")[1]))
print("notification ->", rpc("notifications/initialized", notify=True)[0])

status, data = rpc("tools/list")
if "error" in data:
    print("tools/list ERROR:", short(data["error"]["message"], 400))
    tools = []
else:
    tools = data["result"]["tools"]
    by_container = {}
    for t in tools:
        by_container.setdefault(t.get("container", "?"), []).append(t["name"])
    print(f"tools/list: {len(tools)} tools")
    for c, names in by_container.items():
        print(f"  {c}: {', '.join(names)}")

status, data = rpc("resources/list")
print("resources/list:", short(data.get("result", data.get("error"))))
status, data = rpc("prompts/list")
print("prompts/list:", short(data.get("result", data.get("error"))))
status, data = rpc("resources/read", {"uri": "file://resource/syntax_1c.txt"})
if "result" in data:
    print("resources/read: chars =", len(data["result"]["contents"][0]["text"]))
else:
    print("resources/read ERROR:", short(data["error"]["message"]))

calls = [
    ("get_configuration_version", {"configurationProperty": "ВерсияПлатформы"}),
    ("list_metadata_objects", {"metaType": "Languages"}),
    ("list_metadata_objects", {}),
    ("get_metadata_structure", {"metaType": "Catalogs", "name": "НетТакого"}),
    ("list_object_dependencies", {"objectType": "Catalogs", "objectName": "НетТакого"}),
    ("get_report_list", {}),
    ("list_sale_param", {"periodStart": "2024-03-01", "periodEnd": "2024-03-31"}),
    ("no_such_tool", {}),
]
for name, args in calls:
    status, data = rpc("tools/call", {"name": name, "arguments": args})
    if "error" in data:
        print(f"call {name}: JSON-RPC ERROR {data['error']['code']}: {short(data['error']['message'], 220)}")
    else:
        res = data["result"]
        print(f"call {name}: isError={res['isError']} -> {short(res['content'][0]['text'], 220)}")

status, data = rpc("unknown/method")
print("unknown method:", data["error"]["code"], short(data["error"]["message"]))
