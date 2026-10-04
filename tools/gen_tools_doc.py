"""Генерирует docs/TOOLS.md из ответа tools/list работающего MCP-сервера 1С.

    python tools/gen_tools_doc.py http://localhost/<публикация>/hs/mcp > docs/TOOLS.md

Описание параметров берется из inputSchema, поэтому документ совпадает с тем,
что видит MCP-клиент. Признак заглушки задается списком STUBS ниже.
"""
import json
import sys
import urllib.request

# Инструменты, которые возвращают фиксированные демонстрационные данные и не читают базу.
STUBS = {
    "get_stock_balances", "get_daily_revenue", "get_top_products", "analyze_receipts",
    "get_low_stock_items", "analyze_returns", "analyze_discounts", "calculate_stock_turnover",
    "analyze_plan_execution",
    "get_account_turnovers", "analyze_accounts_receivable", "calculate_vat_liability",
    "calculate_product_margin", "get_stock_turnover", "get_cash_flow",
}

# Условия работы реальных инструментов, которых нет в описании инструмента.
NOTES = {
    "list_sale_param": "Нужен один из отчетов 1С:Розницы: Продажи, ПродажиПоДисконтнымКартамЧека, "
                       "ПродажиПоПлатежнымКартам, ПродажиПоДисконтнымКартам.",
    "get_report_list": "Внешние отчеты читаются из справочника ДополнительныеОтчетыИОбработки (БСП), "
                       "если он есть в конфигурации.",
}


def fetch_tools(url: str) -> list:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8-sig"))["result"]["tools"]


def cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(tools: list) -> str:
    lines = [
        "# Инструменты MCP-сервера",
        "",
        "Документ сгенерирован скриптом `tools/gen_tools_doc.py` из ответа `tools/list`.",
        "",
        "| Статус | Значение |",
        "|---|---|",
        "| ✅ | Читает данные информационной базы |",
        "| ⚠️ | **Заглушка**: возвращает фиксированные демонстрационные данные, аргументы не влияют на результат |",
        "",
    ]
    containers = {}
    for tool in tools:
        containers.setdefault(tool.get("container", "(без контейнера)"), []).append(tool)

    total_stubs = sum(1 for t in tools if t["name"] in STUBS)
    lines += [f"Всего инструментов: {len(tools)}, из них заглушек: {total_stubs}.", ""]

    for container, items in containers.items():
        lines += [f"## {container}", ""]
        lines += ["| | Инструмент | Описание |", "|---|---|---|"]
        for tool in items:
            mark = "⚠️" if tool["name"] in STUBS else "✅"
            lines.append(f"| {mark} | [`{tool['name']}`](#{tool['name']}) | {cell(tool['description'])} |")
        lines.append("")
        for tool in items:
            lines += [f"### {tool['name']}", "", cell(tool["description"]), ""]
            if tool["name"] in STUBS:
                lines += ["> [!WARNING]",
                          "> Заглушка: результат не зависит от данных базы и аргументов.", ""]
            if tool["name"] in NOTES:
                lines += [NOTES[tool["name"]], ""]
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            if not props:
                lines += ["Параметров нет.", ""]
                continue
            lines += ["| Параметр | Тип | Обязательный | По умолчанию | Описание |", "|---|---|:---:|---|---|"]
            for name, prop in props.items():
                kind = prop.get("type", "")
                if kind == "array":
                    kind = f"array of {prop.get('items', {}).get('type', '?')}"
                description = prop.get("description", "")
                if prop.get("enum"):
                    description += " Допустимые значения: " + ", ".join(f"`{v}`" for v in prop["enum"]) + "."
                default = f"`{prop['default']}`" if "default" in prop else ""
                lines.append(f"| `{name}` | {kind} | {'да' if name in required else ''} | {default} | {cell(description)} |")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(fetch_tools(sys.argv[1])))
