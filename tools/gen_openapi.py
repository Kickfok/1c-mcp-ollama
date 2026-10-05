"""Собирает OpenAPI-описание HTTP-сервиса mcp_APIBackend для docs/api/openapi.json.

Источник - описание, которое расширение отдает через swagger-1c (модуль mcp_APIBackendОписание), и
список инструментов MCP (tools/list). Из полного swagger.json базы берутся только пути и схемы
нашего сервиса; к ним добавляются адрес сервера с переменными публикации, схема авторизации
X-MCP-Token, схемы аргументов всех инструментов и примеры запросов.

    python tools/gen_openapi.py --base http://127.0.0.1:8089/kamcp

--base - адрес публикации базы, в которой установлены расширение MCP_Сервер и swagger-1c.
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "api" / "openapi.json"
SERVICE_ROOT = "/mcp"


def fetch_json(url: str, body: dict = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def extension_version() -> str:
    text = (ROOT / "src" / "extension" / "Configuration.xml").read_text(encoding="utf-8-sig")
    return re.search(r"<Version>([^<]+)</Version>", text).group(1)


def referenced_schemas(node, found: set):
    """Имена схем, на которые ссылается узел описания, рекурсивно."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            referenced_schemas(value, found)
    elif isinstance(node, list):
        for value in node:
            referenced_schemas(value, found)


def service_paths(swagger: dict) -> dict:
    """Пути сервиса mcp_APIBackend. Шаблон /* сервиса отвечает и на корень /mcp: в описании он
    показывается как /mcp - этот адрес указывают MCP-клиенты."""
    paths = {}
    for path, operations in swagger["paths"].items():
        if path != SERVICE_ROOT and not path.startswith(SERVICE_ROOT + "/"):
            continue
        public_path = SERVICE_ROOT if path == SERVICE_ROOT + "/*" else path
        paths[public_path] = operations
    if not paths:
        sys.exit("В swagger.json нет путей /mcp: расширение MCP_Сервер не опубликовано в этой базе")
    return paths


def prepare_operations(paths: dict):
    """Заголовок X-MCP-Token описывается схемой авторизации, а не параметром каждого метода;
    у POST токен необязателен (вызов без токена может разрешить администратор), health - без авторизации."""
    for path, operations in paths.items():
        for method, operation in operations.items():
            operation["parameters"] = [p for p in operation.get("parameters", []) if p.get("name") != "X-MCP-Token"]
            if not operation["parameters"]:
                del operation["parameters"]
            operation["operationId"] = f"{method}_{path.strip('/').replace('/', '_')}"
            if method == "post":
                operation["security"] = [{"mcpToken": []}, {}]
                operation["requestBody"]["content"]["application/json"]["examples"] = request_examples()
            else:
                operation["security"] = []


def request_examples() -> dict:
    def rpc(method, params=None, request_id=1):
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}

    return {
        "initialize": {"summary": "Рукопожатие MCP", "value": rpc("initialize")},
        "tools_list": {"summary": "Список инструментов", "value": rpc("tools/list", request_id=2)},
        "tools_call": {"summary": "Вызов инструмента", "value": rpc(
            "tools/call", {"name": "get_metadata_structure",
                           "arguments": {"metaType": "Documents", "name": "ПеремещениеТоваров"}}, 3)},
    }


def add_tools(components: dict, tools: list):
    """Схемы аргументов инструментов: ToolArgs_<имя> из inputSchema, имя инструмента - перечисление."""
    names = []
    for tool in sorted(tools, key=lambda t: t["name"]):
        schema = dict(tool.get("inputSchema") or {"type": "object"})
        schema["description"] = (tool.get("description") or "").strip()
        if tool.get("container"):
            schema["description"] += f"\n\nКонтейнер: {tool['container']}"
        components["schemas"][f"ToolArgs_{tool['name']}"] = schema
        names.append(tool["name"])

    params = components["schemas"].get("ToolCallParams")
    if params:
        params["properties"]["name"]["enum"] = names
        params["properties"]["arguments"]["description"] = (
            "Аргументы инструмента: схема ToolArgs_<имя инструмента> в разделе Schemas")


def build(base: str) -> dict:
    swagger = fetch_json(f"{base}/hs/swagger/swagger.json")
    tools = fetch_json(f"{base}/hs/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]

    paths = service_paths(swagger)
    prepare_operations(paths)

    needed = set()
    referenced_schemas(paths, needed)
    all_schemas = swagger.get("components", {}).get("schemas", {})
    pending = list(needed)
    while pending:
        name = pending.pop()
        found = set()
        referenced_schemas(all_schemas.get(name, {}), found)
        for extra in found - needed:
            needed.add(extra)
            pending.append(extra)

    tag = next(iter(next(iter(paths.values())).values()))["tags"][0]
    tag_info = next((t for t in swagger.get("tags", []) if t.get("name") == tag), {"name": tag})

    components = {
        "schemas": {name: all_schemas[name] for name in sorted(needed) if name in all_schemas},
        "securitySchemes": {
            "mcpToken": {
                "type": "apiKey", "in": "header", "name": "X-MCP-Token",
                "description": "Токен пользователя 1С: mcpt_ и 64 шестнадцатеричных символа. Токен вопроса "
                               "выпускает форма 1С, персональный - администратор в настройках подключения к LLM.",
            },
        },
    }
    add_tools(components, tools)

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "1C MCP Server - HTTP-сервис mcp_APIBackend",
            "version": extension_version(),
            "description": tag_info.get("description", "") + "\n\nИсходник описания - общий модуль "
                           "mcp_APIBackendОписание расширения MCP_Сервер; файл собран tools/gen_openapi.py.",
            "license": {"name": "MIT", "url": "https://github.com/Kickfok/1c-mcp-ollama/blob/main/LICENSE"},
        },
        "externalDocs": {"description": "Репозиторий проекта", "url": "https://github.com/Kickfok/1c-mcp-ollama"},
        "servers": [{
            "url": "{scheme}://{host}/{publication}/hs",
            "description": "Публикация информационной базы на веб-сервере",
            "variables": {
                "scheme": {"enum": ["https", "http"], "default": "https"},
                "host": {"default": "localhost", "description": "Сервер публикации"},
                "publication": {"default": "base", "description": "Имя публикации базы"},
            },
        }],
        "tags": [tag_info],
        "paths": paths,
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="адрес публикации базы, например http://127.0.0.1:8089/kamcp")
    args = parser.parse_args()

    spec = build(args.base.rstrip("/"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: путей {len(spec['paths'])}, схем {len(spec['components']['schemas'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
