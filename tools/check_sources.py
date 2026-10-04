"""Статические проверки исходников расширения и оркестратора (запускаются в CI).

    python tools/check_sources.py

Проверки:
  1. Каждый XML-файл выгрузки разбирается парсером.
  2. Нет представлений с несуществующим кодом языка (например, ru1): платформа их не показывает.
  3. Модули расширения не обращаются к общим модулям БСП и БТС: расширение должно
     работать в любой конфигурации.
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
# Общие модули БСП и БТС. Префикс mcp_ не совпадает с границей слова, поэтому
# собственный модуль mcp_ОбщегоНазначения под шаблон не попадает.
FOREIGN_MODULES = re.compile(
    r"\b(ОбщегоНазначения|ОбщегоНазначенияКлиентСервер|ОбщегоНазначенияКлиент|"
    r"ОбщегоНазначенияСервер|ОбщегоНазначенияБТС|СтандартныеПодсистемыСервер)\.")


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
            match = FOREIGN_MODULES.search(code)
            if match:
                errors.append(f"{path.relative_to(ROOT)}:{number}: обращение к модулю "
                              f"{match.group(1)} (БСП/БТС), расширение должно работать без них")
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
