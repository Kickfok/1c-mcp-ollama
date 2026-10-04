"""Синхронизирует оркестратор с макетом LLM_Orchestrator обработки АнализКонфигурацииMCP.

Форма обработки выгружает макет на диск и запускает его, поэтому макет должен совпадать
с orchestrator/LLM_Orchestrator.py. Сравнение выполняется без учета концов строк:
в репозитории файл хранится с LF, макет записывается с CRLF, как его выгружает платформа.

    python tools/sync_orchestrator.py          # скопировать оркестратор в макет
    python tools/sync_orchestrator.py --check  # только проверить, код возврата 1 при расхождении
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "orchestrator" / "LLM_Orchestrator.py"
TEMPLATE = (ROOT / "src" / "extension" / "DataProcessors" / "АнализКонфигурацииMCP"
            / "Templates" / "LLM_Orchestrator" / "Ext" / "Template.bin")


def normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def main() -> int:
    source = normalized(SOURCE.read_bytes())
    template = normalized(TEMPLATE.read_bytes())

    if "--check" in sys.argv[1:]:
        if source == template:
            print("OK: макет LLM_Orchestrator совпадает с orchestrator/LLM_Orchestrator.py")
            return 0
        print("ОШИБКА: макет LLM_Orchestrator отличается от orchestrator/LLM_Orchestrator.py.\n"
              "Выполните: python tools/sync_orchestrator.py")
        return 1

    TEMPLATE.write_bytes(source.replace(b"\n", b"\r\n"))
    print(f"Макет обновлен: {TEMPLATE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
