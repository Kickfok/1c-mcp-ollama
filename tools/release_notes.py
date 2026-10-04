"""Формирует описание релиза из раздела CHANGELOG.md для указанной версии.

    python tools/release_notes.py 1.0.0 > release-notes.md

Строки-продолжения пунктов списка (с отступом в два пробела) склеиваются с пунктом:
в описании релиза на GitHub одиночный перенос строки отображается как разрыв.
Перед публикацией проверяет, что версия совпадает с версией расширения в Configuration.xml.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/Kickfok/1c-mcp-ollama"


def extension_version() -> str:
    text = (ROOT / "src" / "extension" / "Configuration.xml").read_text(encoding="utf-8-sig")
    match = re.search(r"<Version>([^<]*)</Version>", text)
    return match.group(1) if match else ""


def is_plain_text(line: str) -> bool:
    """Строка обычного абзаца: не пустая, не заголовок, не пункт списка, не цитата и не код."""
    stripped = line.strip()
    return bool(stripped) and not line.startswith((" ", "\t")) and not stripped.startswith(("#", "-", ">", "`", "|"))


def section(version: str) -> list:
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    header = f"## [{version}]"
    start = next((i for i, line in enumerate(lines) if line.startswith(header)), None)
    if start is None:
        sys.exit(f"В CHANGELOG.md нет раздела {header}")
    result = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        previous = result[-1] if result else ""
        if re.match(r"^  \S", line) and re.match(r"^\s*- ", previous):
            result[-1] += " " + line.strip()
        elif is_plain_text(line) and is_plain_text(previous):
            result[-1] += " " + line.strip()
        else:
            result.append(line)
    while result and not result[-1].strip():
        result.pop()
    return result


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    version = sys.argv[1].lstrip("v")
    if extension_version() != version:
        sys.exit(f"Версия расширения в Configuration.xml ({extension_version()}) не совпадает с {version}")
    body = [
        "## Установка",
        "",
        "1. Скачайте `MCP_Сервер.cfe` ниже и подключите в \"Администрирование - Расширения\".",
        "2. Опубликуйте HTTP-сервис `mcp_APIBackend`, явно перечислив его в `default.vrd`.",
        "3. Для LLM-оркестратора скачайте `LLM_Orchestrator.zip`, выполните `pip install -r requirements.txt` "
        "и задайте `MCP_URL`.",
        "",
        f"Подробно - [docs/INSTALL.md]({REPO}/blob/v{version}/docs/INSTALL.md).",
        "",
        "> [!WARNING]",
        "> 15 из 21 инструмента - заглушки с фиксированными демонстрационными данными, "
        f"см. [docs/TOOLS.md]({REPO}/blob/v{version}/docs/TOOLS.md).",
        "",
        "## Изменения",
    ] + section(version)
    print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
