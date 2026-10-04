"""Статические проверки исходников расширения и оркестратора (запускаются в CI).

    python tools/check_sources.py

Проверки:
  1. Каждый XML-файл выгрузки разбирается парсером.
  2. Нет представлений с несуществующим кодом языка (например, ru1): платформа их не показывает.
  3. Модули расширения обращаются только к программному интерфейсу БСП: служебные модули БСП
     (*Служебный*) не гарантируют совместимость между версиями, модулей БТС в конфигурации
     на БСП может не быть.
  4. Макет LLM_Orchestrator совпадает с orchestrator/LLM_Orchestrator.py.
  5. Python-файлы компилируются.
"""
import py_compile
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "src" / "extension"
KNOWN_LANGS = {"ru", "en"}
# Служебные модули БСП и модули БТС. Собственные модули расширения начинаются с mcp_
# и под шаблон не попадают.
MODULE_CALL = re.compile(r"\b([А-Яа-яЁё]+)\.")
FOREIGN_PARTS = ("Служебный", "БТС")


def check_xml(errors: list) -> int:
    files = sorted(EXTENSION.rglob("*.xml"))
    for path in files:
        rel = path.relative_to(ROOT)
        try:
            ET.parse(path)
        except ET.ParseError as e:
            errors.append(f"{rel}: XML не разбирается: {e}")
            continue
        text = path.read_text(encoding="utf-8-sig")
        for lang in set(re.findall(r"<v8:lang>([^<]+)</v8:lang>", text)) - KNOWN_LANGS:
            errors.append(f"{rel}: неизвестный код языка '{lang}'")
    return len(files)


def check_bsl(errors: list) -> int:
    files = sorted(EXTENSION.rglob("*.bsl"))
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            code = line.split("//", 1)[0]
            for match in MODULE_CALL.finditer(code):
                if any(part in match.group(1) for part in FOREIGN_PARTS):
                    errors.append(f"{path.relative_to(ROOT)}:{number}: обращение к модулю "
                                  f"{match.group(1)}: допустим только программный интерфейс БСП")
    return len(files)


def check_python(errors: list) -> int:
    files = sorted(list((ROOT / "orchestrator").glob("*.py")) + list((ROOT / "tools").glob("*.py"))
                   + list((ROOT / "tests").glob("*.py")))
    for path in files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{path.relative_to(ROOT)}: {e.msg}")
    return len(files)


def check_template(errors: list) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import sync_orchestrator
    source = sync_orchestrator.normalized(sync_orchestrator.SOURCE.read_bytes())
    template = sync_orchestrator.normalized(sync_orchestrator.TEMPLATE.read_bytes())
    if source != template:
        errors.append("Макет LLM_Orchestrator отличается от orchestrator/LLM_Orchestrator.py: "
                      "выполните python tools/sync_orchestrator.py")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    errors: list = []
    xml_count = check_xml(errors)
    bsl_count = check_bsl(errors)
    py_count = check_python(errors)
    check_template(errors)
    print(f"Проверено: XML {xml_count}, BSL {bsl_count}, Python {py_count}, макет оркестратора 1")
    for error in errors:
        print("ОШИБКА:", error)
    print("Итог:", "ошибок нет" if not errors else f"ошибок {len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
