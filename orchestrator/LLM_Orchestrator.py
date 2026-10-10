# LLM_Orchestrator.py - HTTP-сервер, связывающий локальную LLM (Ollama) с MCP-сервером 1С.
import argparse
import collections
import hmac
import ipaddress
import itertools
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hashlib import sha1
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

VERSION = "1.3.0"

# ================== НАСТРОЙКИ ==================
# Порядок: значение по умолчанию, затем файл конфигурации (--config), затем переменная окружения.
# Поле файла конфигурации -> (переменная окружения, значение по умолчанию).
SETTINGS = {
    "ollama_url": ("OLLAMA_URL", "http://localhost:11434/api/generate"),
    "ollama_model": ("OLLAMA_MODEL", "qwen2.5-coder:32b"),
    # Пусто - Ollama сама подбирает число слоев на GPU (см. OLLAMA_NUM_GPU ниже).
    "ollama_num_gpu": ("OLLAMA_NUM_GPU", None),
    # Адрес HTTP-сервиса mcp_APIBackend: http(s)://<сервер>/<имя публикации>/hs/mcp
    "mcp_url": ("MCP_URL", "https://localhost/yt_mcp_test/hs/mcp"),
    # Проверка TLS-сертификата публикации. По умолчанию выключена: локальные публикации
    # обычно используют самоподписанный сертификат.
    "mcp_verify_ssl": ("MCP_VERIFY_SSL", False),
    "host": ("ORCHESTRATOR_HOST", "127.0.0.1"),
    "port": ("ORCHESTRATOR_PORT", 9000),
    # Ключ клиента (1С задает вопросы) и ключ администратора (монитор, журнал всех запросов).
    "client_key": ("ORCHESTRATOR_CLIENT_KEY", ""),
    "admin_key": ("ORCHESTRATOR_ADMIN_KEY", ""),
    # Сертификат и закрытый ключ в формате PEM: заданы - оркестратор принимает только HTTPS.
    "tls_cert": ("ORCHESTRATOR_TLS_CERT", ""),
    "tls_key": ("ORCHESTRATOR_TLS_KEY", ""),
}
MIN_KEY_LENGTH = 32


def setting_value(name: str, raw: Any) -> Any:
    """Приводит значение из файла или окружения к типу настройки."""
    default = SETTINGS[name][1]
    if name == "ollama_num_gpu":
        text = "" if raw is None else str(raw).strip()
        return int(text) if text else None
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(raw)
    return "" if raw is None else str(raw).strip()


def read_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    values = {}
    for name, (env_name, default) in SETTINGS.items():
        if os.environ.get(env_name, "").strip():
            values[name] = setting_value(name, os.environ[env_name])
        elif name in config:
            values[name] = setting_value(name, config[name])
        else:
            values[name] = default
    return values


def apply_settings(values: Dict[str, Any]):
    global OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_GPU, MCP_URL, MCP_VERIFY_SSL
    global ORCHESTRATOR_HOST, ORCHESTRATOR_PORT, CLIENT_KEY, ADMIN_KEY, TLS_CERT, TLS_KEY
    OLLAMA_URL = values["ollama_url"]
    OLLAMA_MODEL = values["ollama_model"]
    OLLAMA_NUM_GPU = values["ollama_num_gpu"]
    MCP_URL = values["mcp_url"]
    MCP_VERIFY_SSL = values["mcp_verify_ssl"]
    ORCHESTRATOR_HOST = values["host"]
    ORCHESTRATOR_PORT = values["port"]
    CLIENT_KEY = values["client_key"]
    ADMIN_KEY = values["admin_key"]
    TLS_CERT = values["tls_cert"]
    TLS_KEY = values["tls_key"]
    if not MCP_VERIFY_SSL:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


apply_settings(read_settings({}))

MAX_STEPS = 15
# Сколько повторов одного и того же вызова подряд отклоняется, прежде чем вопрос завершается ошибкой
MAX_REJECTED_REPEATS = 3
LLM_TIMEOUT = 1800
MCP_TIMEOUT = 30
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY = 2

# Число слоев модели на GPU.
# None  -> не передавать параметр, Ollama сама подберет максимум под доступную
#          видеопамять. Так не будет падений CUDA OOM на другой видеокарте.
# Число -> задать явно. Если слоев больше, чем помещается в видеопамять,
#          runner падает с "CUDA error / out of memory".
#          На 12 ГБ (RTX 5070) помещается около 24 слоев.
# Задается полем ollama_num_gpu файла конфигурации или переменной окружения OLLAMA_NUM_GPU;
# пустое значение - автоподбор.

# Размер контекстного окна модели.
# 4096 стабильно грузится на 12 ГБ VRAM вместе с 32B-моделью.
# Большой контекст (8192) на этой карте вызывает CUDA out of memory при
# загрузке (не хватает pinned-памяти под KV-кэш + слои). Переполнение из-за
# больших ответов инструментов устранено обрезкой (см. MAX_TOOL_RESULT_CHARS
# здесь и maxItems в инструментах 1С), поэтому большой контекст не нужен.
OLLAMA_NUM_CTX = 4096

# Максимальная длина текста результата инструмента (в символах).
# Подобрано под окно 4096 токенов: системный промпт со списком инструментов
# занимает ~4000 токенов, поэтому ответ инструмента должен быть компактным,
# иначе промпт обрежется и модель потеряет формат ответа. ~2500 символов
# (≈800-1000 токенов) оставляет запас. При превышении текст обрезается с пометкой.
MAX_TOOL_RESULT_CHARS = 2500


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LLM-Orchestrator")

# Число событий, которые журнал хранит для монитора и формы 1С.
JOURNAL_SIZE = 3000
# Длина фрагмента ответа инструмента, который попадает в событие журнала.
TRACE_PREVIEW_CHARS = 1200


# ================== ЖУРНАЛ СОБЫТИЙ ==================
# Журнал запросов для монитора (GET /monitor) и формы 1С (GET /events).
class EventJournal:
    def __init__(self, size: int):
        self._events = collections.deque(maxlen=size)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, kind: str, text: str, request_id: Optional[str] = None,
            level: int = logging.INFO, data: Optional[Dict[str, Any]] = None,
            user: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": round(now, 3),
                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                "request": request_id,
                "user": user,
                "kind": kind,
                "level": logging.getLevelName(level),
                "text": text,
                "data": data or {},
            }
            self._events.append(event)
        return event

    @property
    def last_seq(self) -> int:
        return self._seq

    def since(self, seq: int, request_id: Optional[str] = None, limit: int = 500) -> list:
        with self._lock:
            events = [e for e in self._events
                      if e["seq"] > seq and (request_id is None or e["request"] == request_id)]
        return events[:limit]


JOURNAL = EventJournal(JOURNAL_SIZE)

# Запрос, который обрабатывает текущий поток. Нужен, чтобы записи лога из call_llm
# и mcp_call_tool попали в журнал своего запроса.
_context = threading.local()


def current_request_id() -> Optional[str]:
    return getattr(_context, "request_id", None)


def current_user() -> Optional[str]:
    return getattr(_context, "user", None)


class JournalLogHandler(logging.Handler):
    """Переносит записи лога в журнал событий. Записи, созданные функцией trace, пропускаются:
    они уже есть в журнале в виде событий с данными."""

    def emit(self, record):
        if getattr(record, "journaled", False):
            return
        try:
            JOURNAL.add("log", record.getMessage(), current_request_id(), record.levelno, user=current_user())
        except Exception:
            self.handleError(record)


logger.addHandler(JournalLogHandler())


def trace(kind: str, text: str, level: int = logging.INFO, **data) -> Dict[str, Any]:
    """Пишет строку в консоль и событие с данными в журнал текущего запроса."""
    logger.log(level, text, extra={"journaled": True})
    return JOURNAL.add(kind, text, current_request_id(), level, data, current_user())


def tool_result_preview(result: Any) -> str:
    """Текст ответа инструмента для журнала: первый текстовый блок content."""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return json.dumps(result, ensure_ascii=False)


def tool_result_length(result: Any) -> int:
    return len(tool_result_preview(result))

# ================== SYSTEM PROMPT ==================
SYSTEM_PROMPT = """
Ты — аналитический orchestrator.

ВАЖНЕЙШИЕ ПРАВИЛА:
1. Ты должен отвечать **ТОЛЬКО чистым JSON** без каких-либо дополнительных текстов
2. Не используй ```json ```, ` ``, или другие обрамляющие теги
3. Не добавляй текст перед JSON или после него
4. Твой ответ должен начинаться с '{' и заканчивается '}'
5. JSON должен быть полностью валидным и соответствовать одному из форматов ниже

ФОРМАТЫ ОТВЕТА:

1) Для вызова инструмента:
{
  "action": "call_tool",
  "name": "имя_инструмента",
  "arguments": {
    "ключ": "значение"
  }
}

2) Для финального ответа:
{
  "action": "final",
  "Text": "краткое текстовое описание результата",
  "Result": { ... }  // Здесь должны быть ПОЛНЫЕ данные из инструментов
}

ВАЖНО:
- НЕ ИСПОЛЬЗУЙ теги <think> и НЕ ВЫВОДИ никаких внутренних рассуждений.
- В поле "Result" помещай ПОЛНЫЕ данные, полученные от инструментов
- В поле "Text" пиши только краткое описание того, что в Result
- Не обрезай данные в поле Result
- Если данные большие, возвращай их полностью в Result
- Не добавляй никаких дополнительных полей кроме "action", "Text" и "Result"

ОБЩИЕ ПРИНЦИПЫ РАБОТЫ:
- Внимательно анализируй запрос пользователя
- Используй инструменты для получения информации
- Если инструмент возвращает ошибку, анализируй её и исправляй аргументы
- Если для выполнения запроса требуется сначала получить список объектов, сделай это
- Выбирай наиболее подходящие аргументы для инструментов на основе контекста
- Если данных достаточно — возвращай финальный ответ
- Используй точные названия инструментов из списка доступных
- Не повторяй одинаковые вызовы инструментов
- Если готового инструмента для вопроса нет, составь запрос на языке запросов 1С: узнай имена полей
  через get_metadata_structure, найди образец через find_report_queries, проверь текст через
  validate_query и выполни run_query. Пиши запрос только по-русски (ВЫБРАТЬ, ИЗ, ГДЕ)
- list_metadata_objects и get_metadata_structure описывают конфигурацию (какие есть справочники и их
  реквизиты), а не записи базы. Вопрос о записях (перечисли контрагентов, сколько документов, какая
  сумма) требует run_query
"""


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def find_valid_json(text: str) -> Optional[str]:
    """Находит валидный JSON объект в строке, игнорируя рассуждения и markdown"""
    # Удаляем блоки рассуждений
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL)
    # Очищаем от markdown-блоков
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    
    # JSON начинается с первой '{'
    start = text.find('{')
    if start == -1:
        return None
    
    # Отрезаем все до первой '{'
    text = text[start:]
    
    # Ищем соответствующую закрывающую скобку с учетом вложенности
    balance = 0
    end = 0
    for i, ch in enumerate(text):
        if ch == '{':
            balance += 1
        elif ch == '}':
            balance -= 1
            if balance == 0:
                end = i
                break
    if balance != 0:
        # Если баланс не сошелся, пробуем найти последнюю '}'
        last_close = text.rfind('}')
        if last_close > 0:
            end = last_close
        else:
            return None
    
    json_candidate = text[:end + 1]
    return json_candidate if json_candidate.startswith('{') and json_candidate.endswith('}') else None


def normalize_llm_response(data: Any) -> Any:
    """
    Приводит ответ LLM к каноническому формату ДО валидации.

    Модель (особенно qwen2.5-coder) периодически отклоняется от схемы:
      - кладет имя инструмента прямо в "action" вместо литерала "call_tool";
      - называет аргументы "parameters" вместо "arguments";
      - называет имя инструмента "tool"/"tool_name" вместо "name".
    Каноника: {"action":"call_tool","name":...,"arguments":{...}}
              {"action":"final","Text":...,"Result":...}

    Нормализуем по структуре, не завязываясь на список инструментов.
    Если привести не удается - возвращаем данные как есть (отбракует валидатор).
    """
    if not isinstance(data, dict):
        return data

    action = data.get('action')

    # Финальный ответ не трогаем.
    if action == 'final':
        return data

    name = None
    for key in ('name', 'tool', 'tool_name', 'toolName'):
        if key in data and isinstance(data[key], str):
            name = data[key]
            break

    args = normalized_arguments(data)

    # Случай: action содержит имя инструмента (не "call_tool"/"final"),
    # а рядом лежат параметры -> это вызов инструмента.
    if action not in (None, 'call_tool', 'final'):
        if name is None:
            name = action
        if args is None:
            args = {}
        return {"action": "call_tool", "name": name, "arguments": args}

    # Случай: action == 'call_tool', но аргументы/имя названы иначе.
    if action == 'call_tool':
        return {
            "action": "call_tool",
            "name": name if name is not None else data.get('name'),
            "arguments": args if args is not None else data.get('arguments', {})
        }

    # action отсутствует, но есть имя инструмента и параметры -> вызов.
    if action is None and name is not None:
        return {"action": "call_tool", "name": name, "arguments": args if args is not None else {}}

    return data


def normalized_arguments(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Аргументы вызова из ответа модели. Модель кладет их в "arguments", называет иначе ("parameters", "args",
    "params") или пишет прямо рядом с action: {"action": "run_query", "query": "ВЫБРАТЬ ..."}. Если рядом с
    action есть свои поля, "parameters" считается аргументом инструмента (у run_query есть такой параметр),
    а не контейнером аргументов."""
    if isinstance(data.get('arguments'), dict):
        return data['arguments']
    service_keys = {'action', 'name', 'tool', 'tool_name', 'toolName', 'arguments', 'args', 'params', 'parameters',
                    'Text', 'Result'}
    top_level = {k: v for k, v in data.items() if k not in service_keys}
    if top_level:
        if 'parameters' in data:
            top_level['parameters'] = data['parameters']
        return top_level
    for key in ('parameters', 'args', 'params'):
        if isinstance(data.get(key), dict):
            return data[key]
    return None


def validate_json_structure(data: Dict[str, Any]) -> bool:
    """Проверяет структуру JSON ответа от LLM"""
    if not isinstance(data, dict):
        return False
    
    action = data.get('action')
    if action not in ['call_tool', 'final']:
        return False
    
    if action == 'call_tool':
        required = ['name', 'arguments']
        return all(key in data for key in required)
    elif action == 'final':
        required = ['Text', 'Result']
        return all(key in data for key in required)
    
    return False


def analyze_ollama_error(error: Exception, response: Optional[requests.Response] = None) -> Dict[str, Any]:
    """Анализирует ошибку Ollama и возвращает структурированное описание"""
    error_info = {
        "type": "unknown",
        "message": str(error),
        "details": {}
    }
    
    try:
        error_str = str(error).lower()
        
        # Проверяем наличие информации в response
        if response is not None:
            try:
                response_data = response.json()
                if 'error' in response_data:
                    error_info['details']['ollama_error'] = response_data['error']
            except:
                pass
        
        # Определяем тип ошибки по ключевым словам
        if 'memory' in error_str or 'requires more system memory' in error_str:
            error_info['type'] = "memory_error"
            error_info['message'] = "Недостаточно памяти для обработки запроса"
            
            # Извлекаем информацию о памяти из сообщения
            memory_match = re.search(r'(\d+\.?\d*)\s*GiB', error_str)
            if memory_match:
                error_info['details']['required_memory'] = memory_match.group(1) + " GiB"
            
            memory_match = re.search(r'available\s*(\d+\.?\d*)\s*GiB', error_str)
            if memory_match:
                error_info['details']['available_memory'] = memory_match.group(1) + " GiB"
                
        elif 'timeout' in error_str or 'timed out' in error_str:
            error_info['type'] = "timeout_error"
            error_info['message'] = "Превышено время ожидания ответа от LLM"
            
        elif 'connection' in error_str or 'refused' in error_str:
            error_info['type'] = "connection_error"
            error_info['message'] = "Не удалось подключиться к серверу LLM"
            
        elif 'model' in error_str and 'not found' in error_str:
            error_info['type'] = "model_error"
            error_info['message'] = f"Модель '{OLLAMA_MODEL}' не найдена"
            
        elif 'context length' in error_str or 'num_ctx' in error_str:
            error_info['type'] = "context_error"
            error_info['message'] = "Запрос слишком длинный для контекста модели"
            
        elif '500' in error_str or 'internal server error' in error_str:
            error_info['type'] = "server_error"
            error_info['message'] = "Внутренняя ошибка сервера LLM"
            
    except Exception as e:
        logger.warning(f"Ошибка при анализе ошибки Ollama: {e}")
    
    return error_info


def get_error_suggestion(error_type: str) -> str:
    """Возвращает подсказку для пользователя в зависимости от типа ошибки"""
    suggestions = {
        "memory_error": "Недостаточно оперативной памяти. Попробуйте упростить запрос или увеличьте объем оперативной памяти.",
        "timeout_error": "Превышено время ожидания. Попробуйте упростить запрос или увеличьте таймаут.",
        "connection_error": "Проблема с подключением к серверу LLM. Проверьте, запущен ли сервер Ollama.",
        "model_error": f"Модель {OLLAMA_MODEL} не найдена. Убедитесь, что модель загружена в Ollama.",
        "context_error": "Запрос слишком длинный для модели. Попробуйте упростить запрос.",
        "server_error": "Внутренняя ошибка сервера LLM. Попробуйте позже или перезапустите сервер Ollama.",
        "invalid_response": "LLM вернула некорректный ответ. Попробуйте переформулировать запрос.",
        "unknown": "Неизвестная ошибка. Попробуйте еще раз."
    }
    return suggestions.get(error_type, suggestions["unknown"])


def create_success_response(text: str, result: Any) -> Dict[str, Any]:
    """Создает стандартный успешный ответ"""
    return {
        "Success": True,
        "Text": text,
        "Result": result
    }


def create_error_response(text: str, error_type: str = "unknown", details: Optional[Dict] = None, success: bool = False) -> Dict[str, Any]:
    """Создает стандартный ответ с ошибкой"""
    return {
        "Success": success,
        "Text": text,
        "Result": {
            "error_type": error_type,
            "details": details or {},
            "suggestion": get_error_suggestion(error_type)
        }
    }


def is_error_result(result: Dict[str, Any]) -> bool:
    """Проверяет, является ли результат ошибкой"""
    if not isinstance(result, dict):
        return False
    
    # Если есть поле error_type, это ошибка
    if "error_type" in result:
        return True
    
    # Если это результат от MCP инструмента с isError = True
    if result.get("isError", False):
        return True
    
    # Если в content есть сообщение об ошибке
    if isinstance(result.get("content"), list) and len(result["content"]) > 0:
        first_item = result["content"][0]
        if isinstance(first_item, dict) and "text" in first_item:
            error_text = first_item["text"].lower()
            if any(word in error_text for word in ["ошибка", "error", "exception", "failed"]):
                return True
    
    return False


def normalize_mcp_result(result: Any) -> Dict[str, Any]:
    """Нормализует результат MCP в единый формат"""
    # Если результат - None или пустой
    if not result:
        return {"content": [], "isError": True}
    
    # Если результат - строка (текстовый ответ от 1С)
    if isinstance(result, str):
        return {
            "content": [{"type": "text", "text": result}],
            "isError": False
        }
    
    # Если результат - словарь
    if isinstance(result, dict):
        # Если уже есть content и isError - возвращаем как есть
        if "content" in result and "isError" in result:
            return result
        
        # Если есть только content (без isError)
        if "content" in result:
            result["isError"] = result.get("isError", False)
            return result
        
        # Если есть только isError
        if "isError" in result:
            result["content"] = result.get("content", [])
            return result
        
        # Если это структура с результатом (например, от get_metadata_structure в 1С)
        # Оборачиваем в content
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
            "isError": False
        }
    
    # Для любых других типов
    return {
        "content": [{"type": "text", "text": str(result)}],
        "isError": False
    }


def truncate_tool_result(result: Dict[str, Any], max_chars: int = MAX_TOOL_RESULT_CHARS) -> Dict[str, Any]:
    """
    Обрезает текстовое содержимое результата инструмента до max_chars символов.
    Защищает контекст модели от переполнения большими ответами (например,
    list_object_dependencies по всей конфигурации). К обрезанному тексту
    добавляется явная пометка, чтобы модель понимала, что данные неполные
    и не пыталась додумывать недостающее.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result

    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str) and len(text) > max_chars:
                item["text"] = (
                    text[:max_chars]
                    + f"\n\n[...РЕЗУЛЬТАТ ОБРЕЗАН: показано {max_chars} из {len(text)} символов. "
                    + "Данные неполные. Используй параметры фильтрации/лимита инструмента, "
                    + "чтобы сузить выборку, либо опиши только полученную часть.]"
                )
    return result


# ================== MCP ==================
# Идентификаторы JSON-RPC запросов: уникальные в пределах запуска оркестратора.
_mcp_request_ids = itertools.count(1)


def mcp_payload(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": next(_mcp_request_ids), "method": method, "params": params}


# Последняя ошибка обращения к MCP-серверу 1С: показывается в /health и диагностике.
MCP_STATE = {"error": None, "checked": None}


def mcp_request(payload, mcp_token: Optional[str] = None):
    """Отправляет JSON-RPC в HTTP-сервис 1С. Токен пользователя 1С уходит заголовком X-MCP-Token:
    по нему 1С проверяет права пользователя, задавшего вопрос."""
    headers = {"X-MCP-Token": mcp_token} if mcp_token else None
    try:
        r = requests.post(MCP_URL, json=payload, headers=headers, timeout=MCP_TIMEOUT, verify=MCP_VERIFY_SSL)
        MCP_STATE["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if r.status_code == 204 or not r.text.strip():
            MCP_STATE["error"] = None
            return None
        r.raise_for_status()
        MCP_STATE["error"] = None
        return r.json()
    except Exception as e:
        MCP_STATE["error"] = str(e)[:300]
        logger.warning("Ошибка MCP запроса: %s", e)
        return {"result": {"content": [], "isError": True}}


def mcp_initialize():
    logger.info("MCP initialize")
    mcp_request(mcp_payload("initialize", {}))


def mcp_list_tools():
    data = mcp_request(mcp_payload("tools/list", {}))
    if not data or "result" not in data:
        return []
    return data["result"].get("tools", [])


def load_tools() -> int:
    """Загружает список инструментов MCP. Вызывается при запуске и повторно, если при запуске
    1С была недоступна."""
    tools = mcp_list_tools()
    if tools:
        set_tools(tools)
        logger.info(format_tools_by_container(tools))
    else:
        logger.warning("Не загружены инструменты MCP")
    return len(tools)


def format_tools_by_container(tools: list) -> str:
    """
    Группирует инструменты по контейнерам (поле 'container' из tools/list)
    и форматирует столбиками для вывода в лог. Если контейнер не указан,
    инструмент попадает в группу "(без контейнера)".
    """
    groups = {}
    for t in tools:
        container = t.get("container") or "(без контейнера)"
        groups.setdefault(container, []).append(t.get("name", "?"))

    lines = [f"Загружены инструменты ({len(tools)}) по контейнерам:"]
    for container in sorted(groups):
        names = groups[container]
        lines.append(f"  • {container} ({len(names)}):")
        for name in names:
            lines.append(f"      - {name}")
    return "\n".join(lines)


def mcp_call_tool(name, arguments):
    payload = mcp_payload("tools/call", {"name": name, "arguments": arguments})
    logger.info(f"MCP CALL: {name} {arguments}", extra={"journaled": True})
    data = mcp_request(payload, getattr(_context, "mcp_token", None))

    if data and isinstance(data.get("error"), dict):
        # Отказ HTTP-сервиса (например, токен пользователя истек): текст нужен модели и пользователю
        result = {"content": [{"type": "text", "text": str(data["error"].get("message", ""))}], "isError": True}
    elif data:
        result = data.get("result", {"content": [], "isError": True})
    else:
        result = {"content": [], "isError": True}

    # Нормализуем результат в единый формат
    normalized_result = normalize_mcp_result(result)
    
    logger.info(f"MCP RESPONSE (normalized): {json.dumps(normalized_result, ensure_ascii=False)[:200]}...",
                extra={"journaled": True})
    
    return normalized_result


# ================== LLM ==================
def warmup_llm_async():
    """Асинхронный прогрев LLM в фоновом потоке"""
    def _warmup():
        try:
            logger.info("🔄 Прогрев LLM запущен в фоновом режиме...")
            logger.info(f"⏳ Загрузка модели {OLLAMA_MODEL} может занять 30-60 секунд")
            requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": "OK", "stream": False},
                timeout=LLM_TIMEOUT
            )
            logger.info("✅ LLM прогрета и готова к работе")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка прогрева LLM: {e}")
    
    thread = threading.Thread(target=_warmup, daemon=True)
    thread.start()


def call_llm(prompt: str) -> Dict[str, Any]:
    """Вызывает LLM с анализом ошибок и возвратом структурированной информации"""
    for attempt in range(LLM_MAX_RETRIES):
        try:
            logger.debug(f"Попытка {attempt + 1}/{LLM_MAX_RETRIES} запроса к LLM")
            
            # Опции генерации. num_gpu добавляем только если задан явно,
            # иначе Ollama сама подберет число слоев под доступную видеопамять.
            ollama_options = {
                "temperature": 0.0,
                "num_ctx": OLLAMA_NUM_CTX,
                "num_predict": 2048,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
            }
            if OLLAMA_NUM_GPU is not None:
                ollama_options["num_gpu"] = OLLAMA_NUM_GPU

            r = requests.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": ollama_options
                },
                timeout=LLM_TIMEOUT
            )
            
            # Проверяем статус ответа
            if r.status_code != 200:
                error_info = analyze_ollama_error(
                    Exception(f"HTTP {r.status_code}: {r.text[:200]}"),
                    r
                )

                logger.warning(f"Ошибка HTTP {r.status_code} от LLM: {error_info}")

                # Ошибка 5xx бывает разовой: runner Ollama падает при загрузке модели с
                # "CUDA error" (failed to allocate pinned memory), а следующая загрузка проходит.
                if r.status_code >= 500 and attempt < LLM_MAX_RETRIES - 1:
                    time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                    continue

                return {
                    "action": "final",
                    "Text": error_info['message'],
                    "Result": {
                        "error_type": error_info['type'],
                        "details": error_info['details'],
                        "suggestion": get_error_suggestion(error_info['type'])
                    }
                }
            
            r.raise_for_status()
            
            response_data = r.json()
            raw_text = response_data.get("response", "")
            # Сколько токенов занял промпт и ответ - для монитора и формы
            _context.llm_stats = {
                "prompt_tokens": response_data.get("prompt_eval_count"),
                "output_tokens": response_data.get("eval_count"),
                "load_seconds": round((response_data.get("load_duration") or 0) / 1e9, 1),
                "attempt": attempt + 1,
            }
            
            # Извлекаем JSON из ответа
            json_str = find_valid_json(raw_text)
            
            if not json_str:
                logger.warning(f"JSON не найден в ответе LLM (попытка {attempt + 1})")
                if attempt < LLM_MAX_RETRIES - 1:
                    time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                    continue
                raise ValueError("LLM вернула не JSON")
            
            # Парсим JSON
            try:
                result = json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.warning(f"Ошибка парсинга JSON (попытка {attempt + 1}): {e}")
                if attempt < LLM_MAX_RETRIES - 1:
                    time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                    continue
                raise
            
            # Нормализуем формат ответа (модель иногда отклоняется от схемы:
            # action=имя_инструмента, parameters вместо arguments и т.п.)
            result = normalize_llm_response(result)

            # Проверяем структуру
            if not validate_json_structure(result):
                logger.warning(f"Неверная структура JSON (попытка {attempt + 1}): {result}")
                if attempt < LLM_MAX_RETRIES - 1:
                    time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                    continue
                raise ValueError("Неверная структура JSON ответа")
            
            return result
            
        except requests.RequestException as e:
            error_info = analyze_ollama_error(e)
            logger.warning(f"Ошибка соединения с LLM (попытка {attempt + 1}): {error_info}")
            
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                continue
            else:
                return {
                    "action": "final",
                    "Text": error_info['message'],
                    "Result": {
                        "error_type": error_info['type'],
                        "details": error_info['details'],
                        "suggestion": get_error_suggestion(error_info['type'])
                    }
                }
                
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Ошибка данных от LLM (попытка {attempt + 1}): {e}")
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                continue
            else:
                return {
                    "action": "final",
                    "Text": "LLM вернула некорректный ответ",
                    "Result": {
                        "error_type": "invalid_response",
                        "details": {"error": str(e)},
                        "suggestion": "Попробуйте переформулировать запрос"
                    }
                }
                
        except Exception as e:
            error_info = analyze_ollama_error(e)
            logger.error(f"Неожиданная ошибка в call_llm: {error_info}")
            
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(LLM_RETRY_DELAY * (attempt + 1))
                continue
            else:
                return {
                    "action": "final",
                    "Text": error_info['message'],
                    "Result": {
                        "error_type": error_info['type'],
                        "details": error_info['details'],
                        "suggestion": get_error_suggestion(error_info['type'])
                    }
                }


# ================== PROMPT ==================
def build_prompt(messages, tools, include_tools, instructions: Optional[str] = None):
    parts = [SYSTEM_PROMPT.strip()]
    if instructions:
        parts.append("ЗАМЕТКИ АДМИНИСТРАТОРА ОБ ЭТОЙ БАЗЕ (учитывай при выборе объектов и инструментов):\n"
                     + instructions)
    
    if include_tools and tools:
        parts.append("ДОСТУПНЫЕ ИНСТРУМЕНТЫ:")
        parts.extend(tool_prompt_line(t) for t in tools.values())
    elif tools:
        # После первого шага описания не повторяются ради контекста, но имена и параметры нужны: без них
        # модель вызывает инструменты с пустыми или выдуманными аргументами
        parts.append("ИНСТРУМЕНТЫ (имя и параметры, * - обязательный):\n"
                     + "\n".join(tool_prompt_line(t, with_description=False) for t in tools.values()))

    for m in messages:
        if m["role"] == "user":
            parts.append(f"ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{m['content']}")
        elif m["role"] == "tool":
            parts.extend(tool_result_prompt(m['content'], m.get('tool')))

    parts.append("\n" + "="*50 + "\nТВОЙ ОТВЕТ (ТОЛЬКО JSON, помни про формат с Text и Result):")

    return "\n\n".join(parts)


def tool_prompt_line(tool: Dict[str, Any], with_description: bool = True) -> str:
    """Описание инструмента для промпта: имя, описание и компактный список параметров. Без описания -
    одна строка "имя(параметры)" для шагов после первого.

    Полная JSON-схема раздувала промпт больше чем до 4000 токенов, и он обрезался при контексте 4096.
    Поэтому выводятся только имя, тип, обязательность (*) и enum, если он есть: этого модели достаточно
    для правильного вызова."""
    input_schema = tool.get('inputSchema', {})
    props = input_schema.get('properties') if isinstance(input_schema, dict) else None
    param_strs = []
    if isinstance(props, dict):
        required = set(input_schema.get('required', []) or [])
        param_strs = [tool_param_prompt(pname, pinfo, pname in required) for pname, pinfo in props.items()]
    if not with_description:
        return f"{tool['name']}({', '.join(param_strs)})"
    tool_desc = f"{tool['name']}: {tool.get('description', 'Без описания')}"
    if not param_strs:
        return tool_desc
    return tool_desc + "\n  Параметры: " + ", ".join(param_strs)


def tool_param_prompt(pname: str, pinfo: Any, required: bool) -> str:
    """Параметр инструмента для промпта: имя*(тип) или имя*(тип:значения enum), длинный enum сокращается."""
    pinfo = pinfo if isinstance(pinfo, dict) else {}
    ptype = pinfo.get('type', '')
    mark = '*' if required else ''
    enum = pinfo.get('enum')
    if not enum:
        return f"{pname}{mark}({ptype})"
    enum_preview = ','.join(str(e) for e in enum[:8])
    if len(enum) > 8:
        enum_preview += ',...'
    return f"{pname}{mark}({ptype}:{enum_preview})"


# Инструменты, которые описывают конфигурацию, а не записи базы. После их ответа модель склонна переписать
# описание в Result даже на вопрос о данных, поэтому к результату добавляется подсказка про run_query.
METADATA_TOOLS = {"list_metadata_objects", "get_metadata_structure"}
METADATA_HINT = ("ПОДСКАЗКА: это описание конфигурации, а не записи базы. Если вопрос о записях (список, "
                 "количество, суммы), вызови run_query с полями из этого описания. Если вопрос о составе "
                 "объекта, дай финальный ответ.")


def tool_result_prompt(content: Any, tool: Optional[str] = None) -> List[str]:
    """Результат инструмента для промпта: ошибка выделяется подсказкой, текст 1С выводится как есть, список
    объектов - первыми 10 именами. После описания конфигурации добавляется подсказка про run_query."""
    if not isinstance(content, dict):
        return [f"РЕЗУЛЬТАТ ИНСТРУМЕНТА:\n{json.dumps(content, ensure_ascii=False, indent=2)}"]
    items = content.get('content')
    if content.get('isError', False):
        return tool_error_prompt(content, items)
    if not isinstance(items, list):
        return [f"РЕЗУЛЬТАТ ИНСТРУМЕНТА:\n{json.dumps(content, ensure_ascii=False, indent=2)}"]
    if items and isinstance(items[0], dict) and 'text' in items[0]:
        lines = [f"РЕЗУЛЬТАТ ИНСТРУМЕНТА:\n{items[0]['text']}"]
        if tool in METADATA_TOOLS and "run_query" in AVAILABLE_TOOLS:
            lines.append(METADATA_HINT)
        return lines
    lines = [f"РЕЗУЛЬТАТ ИНСТРУМЕНТА (найдено {len(items)} объектов):"]
    for i, item in enumerate(items[:10], 1):
        name = item.get('name', item.get('title', 'Без названия')) if isinstance(item, dict) else item
        lines.append(f"{i}. {name}")
    if len(items) > 10:
        lines.append(f"... и еще {len(items) - 10} объектов")
    return lines


def tool_error_prompt(content: Dict[str, Any], items: Any) -> List[str]:
    """Ошибка инструмента для промпта: текст ошибки и указание исправить аргументы, а не выдумывать данные."""
    error_text = ""
    if isinstance(items, list) and items and isinstance(items[0], dict) and 'text' in items[0]:
        error_text = items[0]['text']
    detail = f"Сообщение об ошибке: {error_text[:500]}" if error_text \
        else json.dumps(content, ensure_ascii=False, indent=2)
    return ["РЕЗУЛЬТАТ ИНСТРУМЕНТА (ОШИБКА):", detail,
            "ВНИМАНИЕ: Инструмент вернул ошибку. Исправь аргументы и повтори вызов. Не выдумывай данные: "
            "если получить их не удалось, так и напиши в Text."]


def hash_tool_call(name, args):
    """Создает хэш для уникальной идентификации вызова инструмента"""
    return sha1(json.dumps({"name": name, "args": args}, sort_keys=True).encode()).hexdigest()


# ================== МОНИТОР ==================
# Страница монитора. Хранится здесь же, потому что форма 1С выгружает оркестратор из макета одним файлом.
LOGO_SVG = r"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256" role="img" aria-labelledby="title desc">
  <title id="title">1C MCP Ollama Bridge</title>
  <desc id="desc">Мост MCP между базой 1С и локальной языковой моделью</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1B1F3B"/>
      <stop offset="1" stop-color="#0D1024"/>
    </linearGradient>
    <linearGradient id="arc" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#FFC21A"/>
      <stop offset="1" stop-color="#8B7CFF"/>
    </linearGradient>
    <linearGradient id="db" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FFD54A"/>
      <stop offset="1" stop-color="#F5A800"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#8B7CFF" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#8B7CFF" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <rect width="256" height="256" rx="56" fill="url(#bg)"/>
  <g transform="translate(0 22)">

  <!-- База 1С: цилиндр -->
  <g transform="translate(26 112)">
    <path d="M0 14 v52 a34 12 0 0 0 68 0 v-52" fill="url(#db)"/>
    <ellipse cx="34" cy="14" rx="34" ry="12" fill="#FFE27A"/>
    <path d="M0 33 a34 12 0 0 0 68 0" fill="none" stroke="#C98500" stroke-width="2.5" opacity="0.55"/>
    <path d="M0 50 a34 12 0 0 0 68 0" fill="none" stroke="#C98500" stroke-width="2.5" opacity="0.55"/>
    <text x="34" y="54" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="26" font-weight="800" fill="#1B1F3B">1C</text>
  </g>

  <!-- Языковая модель: узлы нейросети -->
  <circle cx="196" cy="140" r="46" fill="url(#glow)"/>
  <g stroke="#B9B0FF" stroke-width="2.5" opacity="0.9">
    <line x1="196" y1="112" x2="172" y2="140"/>
    <line x1="196" y1="112" x2="220" y2="140"/>
    <line x1="172" y1="140" x2="196" y2="168"/>
    <line x1="220" y1="140" x2="196" y2="168"/>
    <line x1="172" y1="140" x2="220" y2="140"/>
    <line x1="196" y1="112" x2="196" y2="168"/>
  </g>
  <g fill="#8B7CFF" stroke="#E4E0FF" stroke-width="2.5">
    <circle cx="196" cy="112" r="8"/>
    <circle cx="172" cy="140" r="8"/>
    <circle cx="220" cy="140" r="8"/>
    <circle cx="196" cy="168" r="8"/>
  </g>

  <!-- Мост MCP -->
  <path d="M60 116 C 92 40, 164 40, 196 98" fill="none" stroke="url(#arc)" stroke-width="9" stroke-linecap="round"/>
  <g stroke-width="3" stroke-linecap="round">
    <line x1="92" y1="77" x2="92" y2="118" stroke="#E8B53A"/>
    <line x1="128" y1="64" x2="128" y2="118" stroke="#C3A06A"/>
    <line x1="164" y1="72" x2="164" y2="118" stroke="#A493E8"/>
  </g>
  <line x1="66" y1="120" x2="184" y2="120" stroke="#5B5F86" stroke-width="5" stroke-linecap="round"/>
  <rect x="98" y="38" width="60" height="24" rx="12" fill="#0D1024" stroke="url(#arc)" stroke-width="2.5"/>
  <text x="128" y="55" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="14" font-weight="700" fill="#FFFFFF" letter-spacing="1">MCP</text>

  <!-- Поток данных по мосту -->
  <circle cx="92" cy="72" r="4" fill="#FFC21A"/>
  <circle cx="164" cy="68" r="4" fill="#A99CFF"/>

  </g>
</svg>"""

MONITOR_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Монитор оркестратора</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<style>
  :root {
    --bg: #0B0D1F; --panel: #12152E; --panel2: #171B3A; --term: #0E1124; --termbar: #191D38;
    --border: #2C3366; --text: #D7DAF5; --dim: #7D84B2; --strong: #FFFFFF;
    --y: #FFC94D; --v: #B3A6FF; --c: #7DD3FC; --ok: #6EE7A8; --err: #FF6B6B; --warn: #FFB454;
    --chip: #1E2350; --chipb: #39407A; --hl: rgba(255, 201, 77, 0.08); --box: #151A36;
    --shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
  }
  body.light {
    --bg: #F3F4FA; --panel: #FFFFFF; --panel2: #F7F8FC; --term: #FFFFFF; --termbar: #EEF0F8;
    --border: #DDE1EF; --text: #1F2440; --dim: #6B7194; --strong: #0D1024;
    --y: #A86A00; --v: #5B49D6; --c: #0B6FA0; --ok: #12864A; --err: #D23C3C; --warn: #A65F00;
    --chip: #F1F2FA; --chipb: #DCE0F0; --hl: rgba(255, 194, 26, 0.12); --box: #F7F8FD;
    --shadow: 0 12px 34px rgba(27, 31, 59, 0.10);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.45 "Segoe UI", system-ui, Arial, sans-serif;
         display: flex; flex-direction: column; }
  .mono { font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 13px; }

  header { display: flex; align-items: center; gap: 16px; padding: 14px 20px; border-bottom: 1px solid var(--border);
           background: var(--panel); }
  header img { width: 44px; height: 44px; border-radius: 10px; }
  .brand h1 { margin: 0; font-size: 19px; font-weight: 800; letter-spacing: -0.2px; color: var(--strong); }
  .brand h1 .y { color: #F5A800; } .brand h1 .v { color: #8B7CFF; }
  .brand div { font-size: 12.5px; color: var(--dim); }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; align-items: center; }
  .chip { display: inline-flex; align-items: center; gap: 7px; padding: 5px 11px; border-radius: 999px;
          background: var(--chip); border: 1px solid var(--chipb); font-size: 12.5px; white-space: nowrap; }
  .chip b { font-weight: 600; color: var(--strong); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); }
  .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px rgba(110, 231, 168, 0.18); }
  .dot.busy { background: var(--y); animation: pulse 1.1s ease-in-out infinite; }
  .dot.err { background: var(--err); }
  @keyframes pulse { 50% { opacity: 0.35; } }
  button { font: inherit; color: var(--text); background: var(--chip); border: 1px solid var(--chipb);
           border-radius: 8px; padding: 5px 11px; cursor: pointer; }
  button:hover { border-color: var(--v); }
  button.on { background: var(--v); color: #fff; border-color: var(--v); }
  .seg { display: inline-flex; }
  .seg button { border-radius: 0; margin-left: -1px; }
  .seg button:first-child { border-radius: 8px 0 0 8px; } .seg button:last-child { border-radius: 0 8px 8px 0; }

  main { flex: 1; display: grid; grid-template-columns: 300px 1fr; gap: 16px; padding: 16px 20px; min-height: 0; }
  aside { display: flex; flex-direction: column; gap: 16px; min-height: 0; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); }
  .card h2 { margin: 0; padding: 12px 14px 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px;
             color: var(--dim); font-weight: 700; }
  .scroll { overflow: auto; min-height: 0; }
  #history { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  #historyList { padding: 0 8px 10px; }
  .req { padding: 9px 10px; border-radius: 9px; cursor: pointer; border: 1px solid transparent; }
  .req:hover { background: var(--panel2); border-color: var(--border); }
  .req .q { color: var(--strong); font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .req .m { color: var(--dim); font-size: 12px; margin-top: 3px; display: flex; gap: 8px; align-items: center; }
  .tools { padding: 0 14px 12px; max-height: 38vh; }
  .tools .grp { margin-top: 8px; font-weight: 700; font-size: 12.5px; color: var(--strong); }
  .tools .grp span { color: var(--dim); font-weight: 400; }
  .tools .t { font-size: 12.5px; padding: 2px 0 2px 12px; color: var(--c); cursor: default; }
  .empty { color: var(--dim); padding: 6px 14px 14px; font-size: 13px; }

  .term { display: flex; flex-direction: column; min-height: 0; background: var(--term); border: 1px solid var(--border);
          border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }
  .termbar { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 16px; background: var(--termbar);
             border-bottom: 1px solid var(--border); }
  .termbar i { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
  .termbar .title { flex: 1; text-align: center; color: var(--dim); }
  #log { flex: 1; padding: 14px 18px 22px; overflow: auto; }
  .line { display: flex; gap: 10px; padding: 2px 0; white-space: pre-wrap; word-break: break-word; animation: show 0.25s ease-out both; }
  @keyframes show { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
  .line .t { color: var(--dim); flex: none; }
  .line .k { flex: none; font-weight: 700; }
  .line .x { flex: 1; min-width: 0; }
  .k.req { color: var(--y); } .k.llm { color: var(--v); } .k.mcp { color: var(--v); } .k.ok { color: var(--ok); }
  .k.err { color: var(--err); } .k.warn { color: var(--warn); } .k.log { color: var(--dim); font-weight: 400; }
  .y { color: var(--y); } .v { color: var(--v); } .c { color: var(--c); } .ok { color: var(--ok); } .err { color: var(--err); }
  .dim { color: var(--dim); } .warn { color: var(--warn); }
  .sep { margin: 16px 0 8px; padding: 10px 14px; border-radius: 10px; background: var(--hl); border: 1px solid var(--border); }
  .sep .q { color: var(--y); font-weight: 700; font-size: 14px; white-space: pre-wrap; }
  .sep .m { color: var(--dim); font-size: 12px; margin-top: 2px; }
  .box { margin: 8px 0 6px 74px; padding: 12px 16px; border-radius: 10px; background: var(--box); border: 1px solid var(--border); }
  .box.err { border-color: var(--err); }
  .meta { color: var(--dim); }
  .bar { display: inline-block; width: 90px; height: 6px; border-radius: 3px; background: var(--chipb); vertical-align: middle; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--v); }
  .bar.full i { background: var(--err); }
  .more { color: var(--c); cursor: pointer; text-decoration: underline dotted; }
  .preview { display: none; margin: 4px 0 6px 74px; padding: 10px 12px; border-radius: 8px; background: var(--box);
             border: 1px dashed var(--border); color: var(--c); max-height: 340px; overflow: auto; }
  .preview.open { display: block; }
  .spin { display: inline-block; width: 10px; height: 10px; border: 2px solid var(--v); border-right-color: transparent;
          border-radius: 50%; animation: rot 0.8s linear infinite; vertical-align: -1px; margin-left: 6px; }
  @keyframes rot { to { transform: rotate(360deg); } }
  footer { display: flex; gap: 18px; padding: 8px 20px; border-top: 1px solid var(--border); background: var(--panel);
           color: var(--dim); font-size: 12px; }
  footer span b { color: var(--text); font-weight: 600; }
  .cursor { animation: blink 1s steps(1) infinite; color: var(--text); }
  @keyframes blink { 50% { opacity: 0; } }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } aside { display: none; } }
  #login { position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
    background: var(--bg); z-index: 10; padding: 16px; }
  #login.show { display: flex; }
  #login form { width: 100%; max-width: 420px; padding: 22px; display: flex; flex-direction: column; gap: 12px; }
  #login h2 { margin: 0; font-size: 18px; color: var(--strong); }
  #login p { margin: 0; color: var(--dim); }
  #login input { font: inherit; padding: 9px 11px; border-radius: 8px; border: 1px solid var(--chipb);
    background: var(--panel2); color: var(--text); }
  #login .err { color: var(--err); min-height: 1.4em; }
</style>
</head>
<body>
<header>
  <img src="/logo.svg" alt="">
  <div class="brand">
    <h1><span class="y">1C</span> MCP <span class="v">Ollama</span> Bridge</h1>
    <div>Монитор оркестратора: запросы, шаги модели, вызовы инструментов 1С</div>
  </div>
  <div class="chips">
    <span class="chip"><span class="dot" id="stateDot"></span><b id="stateText">Подключение...</b></span>
    <span class="chip" title="Модель Ollama">🧠 <b id="model">-</b></span>
    <span class="chip" title="Инструменты MCP из 1С">🧰 <b id="toolsCount">-</b></span>
    <span class="chip" title="HTTP-сервис 1С" id="mcpChip">🔌 <b id="mcpUrl">-</b></span>
    <span class="seg"><button id="fReq" class="on">Запросы</button><button id="fAll">Весь журнал</button></span>
    <button id="autoBtn" class="on" title="Прокручивать к новым событиям">⇣ Автопрокрутка</button>
    <button id="clearBtn" title="Очистить экран (журнал на сервере не меняется)">Очистить</button>
    <button id="themeBtn" title="Светлая или темная тема">☀</button>
  </div>
</header>
<main>
  <aside>
    <section class="card" id="history">
      <h2>Запросы</h2>
      <div class="scroll" id="historyList"><div class="empty">Запросов пока не было</div></div>
    </section>
    <section class="card">
      <h2>Инструменты 1С</h2>
      <div class="scroll tools" id="tools"><div class="empty">Загрузка...</div></div>
    </section>
  </aside>
  <section class="term">
    <div class="termbar">
      <i style="background:#FF6B6B"></i><i style="background:#FFC94D"></i><i style="background:#6EE7A8"></i>
      <span class="title mono" id="termTitle">LLM_Orchestrator.py</span>
    </div>
    <div id="log" class="mono"></div>
  </section>
</main>
<div id="login">
  <form class="card" id="loginForm">
    <h2>Вход в монитор</h2>
    <p>Монитор показывает вопросы всех пользователей, поэтому нужен ключ администратора
      оркестратора (поле admin_key файла конфигурации).</p>
    <input type="password" id="loginKey" autocomplete="off" placeholder="Ключ администратора">
    <div class="err" id="loginError"></div>
    <button type="submit" class="on">Войти</button>
  </form>
</div>
<footer>
  <span>Событий: <b id="evCount">0</b></span>
  <span>Обновлено: <b id="updated">-</b></span>
  <span>Запущен: <b id="started">-</b></span>
  <span>Адрес: <b id="addr">-</b></span>
</footer>
<script>
(function () {
  var seq = 0, showAll = false, autoScroll = true, total = 0, online = null;
  var logEl = document.getElementById("log");
  var requests = {}, order = [], pending = {};
  var KIND = {
    request: ["ЗАПРОС", "req"], llm_start: ["LLM", "llm"], llm_done: ["LLM", "llm"],
    tool_call: ["MCP CALL", "mcp"], tool_result: ["MCP RESPONSE", "mcp"], final: ["ГОТОВО", "ok"],
    error: ["ОШИБКА", "err"], warning: ["ВНИМАНИЕ", "warn"], log: ["", "log"]
  };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function span(cls, text) { return el("span", cls, text); }
  function json(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }
  function num(n) { return n === null || n === undefined ? "?" : String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }
  function sec(s) {
    if (s === undefined || s === null) return "";
    if (s < 10) return s.toFixed(1) + " с";
    if (s < 60) return Math.round(s) + " с";
    var t = Math.round(s);
    return Math.floor(t / 60) + " мин " + (t % 60) + " с";
  }

  // Выбранная тема запоминается в браузере
  function setTheme(light) {
    document.body.className = light ? "light" : "";
    $("themeBtn").textContent = light ? "☾" : "☀";
    try { localStorage.setItem("mcpMonitorTheme", light ? "light" : "dark"); } catch (e) {}
  }
  var savedTheme = null;
  try { savedTheme = localStorage.getItem("mcpMonitorTheme"); } catch (e) {}
  setTheme(savedTheme === "light" || /[?&]theme=light/.test(location.search));
  $("themeBtn").onclick = function () { setTheme(document.body.className !== "light"); };

  $("fReq").onclick = function () { showAll = false; this.className = "on"; $("fAll").className = ""; rerender(); };
  $("fAll").onclick = function () { showAll = true; this.className = "on"; $("fReq").className = ""; rerender(); };
  $("autoBtn").onclick = function () { autoScroll = !autoScroll; this.className = autoScroll ? "on" : ""; };
  $("clearBtn").onclick = function () { logEl.innerHTML = ""; allEvents = []; };

  var allEvents = [];
  function rerender() { logEl.innerHTML = ""; for (var i = 0; i < allEvents.length; i++) render(allEvents[i]); scrollDown(true); }
  function scrollDown(force) { if (autoScroll || force) logEl.scrollTop = logEl.scrollHeight; }

  function line(ev, label, cls) {
    var row = el("div", "line");
    row.appendChild(span("t", ev.time));
    if (label) row.appendChild(span("k " + cls, label));
    var x = span("x");
    row.appendChild(x);
    if (ev.request) row.setAttribute("data-req", ev.request);
    logEl.appendChild(row);
    return x;
  }

  function render(ev) {
    var d = ev.data || {}, k = KIND[ev.kind] || ["", "log"], x;
    if (ev.kind === "log") {
      if (!showAll && ev.level !== "WARNING" && ev.level !== "ERROR") return;
      var cls = ev.level === "ERROR" ? "err" : (ev.level === "WARNING" ? "warn" : "log");
      x = line(ev, ev.level === "INFO" ? "" : ev.level, cls);
      x.appendChild(span(cls === "log" ? "" : cls, ev.text));
      return;
    }
    if (ev.kind === "request") {
      var box = el("div", "sep");
      box.id = "req-" + ev.request;
      box.appendChild(el("div", "q", "▶ " + (d.question || ev.text)));
      box.appendChild(el("div", "m mono", ev.time + "  ·  запрос " + ev.request + (ev.user ? "  ·  " + ev.user : "")));
      logEl.appendChild(box);
      return;
    }
    if (ev.kind === "llm_start") {
      x = line(ev, k[0], k[1]);
      x.appendChild(span("", "Шаг " + d.step + ": модель формирует ответ"));
      var sp = span("spin");
      x.appendChild(sp);
      x.appendChild(span("dim", "  промпт " + num(d.prompt_chars) + " симв."));
      pending[ev.request + ":" + d.step] = sp;
      return;
    }
    if (ev.kind === "llm_done") {
      var key = ev.request + ":" + d.step;
      if (pending[key] && pending[key].parentNode) pending[key].parentNode.removeChild(pending[key]);
      delete pending[key];
      x = line(ev, k[0], k[1]);
      x.appendChild(span("", "Шаг " + d.step + ": " + sec(d.seconds) + "  "));
      if (d.prompt_tokens) {
        var ratio = d.num_ctx ? Math.min(1, d.prompt_tokens / d.num_ctx) : 0;
        var bar = el("span", "bar" + (ratio >= 0.98 ? " full" : ""));
        var fill = el("i"); fill.style.width = Math.round(ratio * 100) + "%"; bar.appendChild(fill);
        x.appendChild(bar);
        x.appendChild(span(ratio >= 0.98 ? "err" : "dim", "  контекст " + num(d.prompt_tokens) + " / " + num(d.num_ctx) +
          " ток." + (ratio >= 0.98 ? " (заполнен)" : "") + "  ·  ответ " + num(d.output_tokens) + " ток."));
      }
      if (d.load_seconds && d.load_seconds >= 1) x.appendChild(span("dim", "  ·  загрузка модели " + sec(d.load_seconds)));
      x.appendChild(span("", "  →  "));
      x.appendChild(span(d.action === "call_tool" ? "v" : (d.action === "final" ? "ok" : "warn"),
        d.action === "call_tool" ? "вызов " + d.tool : (d.action === "final" ? "итоговый ответ" : String(d.action))));
      return;
    }
    if (ev.kind === "tool_call") {
      x = line(ev, k[0], k[1]);
      x.appendChild(span("", d.tool + " "));
      x.appendChild(span("c", json(d.arguments || {})));
      if (d.container) x.appendChild(span("dim", "   " + d.container));
      return;
    }
    if (ev.kind === "tool_result") {
      x = line(ev, k[0], k[1]);
      x.appendChild(span(d.is_error ? "err" : "", d.tool + "  "));
      x.appendChild(span("meta", num(d.chars) + " симв.  ·  " + sec(d.seconds) + (d.truncated ? "  ·  обрезано для контекста модели" : "") + "  "));
      var pre = el("div", "preview mono", d.preview || "");
      var more = span("more", "показать ответ");
      more.onclick = function () { pre.className = pre.className.indexOf("open") < 0 ? "preview mono open" : "preview mono"; };
      x.appendChild(more);
      logEl.appendChild(pre);
      return;
    }
    if (ev.kind === "final" || ev.kind === "error") {
      var b = el("div", "box" + (ev.kind === "error" ? " err" : ""));
      var r1 = el("div");
      r1.appendChild(span("dim", "{ ")); r1.appendChild(span("c", "\"Success\"")); r1.appendChild(span("dim", ": "));
      r1.appendChild(span(d.success ? "ok" : "err", d.success ? "true" : "false")); r1.appendChild(span("dim", ","));
      var r2 = el("div"); r2.style.paddingLeft = "14px";
      r2.appendChild(span("c", "\"Text\"")); r2.appendChild(span("dim", ": ")); r2.appendChild(span("y", json(ev.text)));
      r2.appendChild(span("dim", " }"));
      var r3 = el("div", "meta"); r3.style.marginTop = "6px";
      r3.textContent = "шагов: " + d.steps + "  ·  время: " + sec(d.seconds) + "  ·  ";
      var res = el("div", "preview mono", JSON.stringify(d.result, null, 2));
      var m2 = span("more", "показать Result");
      m2.onclick = function () { res.className = res.className.indexOf("open") < 0 ? "preview mono open" : "preview mono"; };
      r3.appendChild(m2);
      b.appendChild(r1); b.appendChild(r2); b.appendChild(r3);
      logEl.appendChild(b);
      logEl.appendChild(res);
      return;
    }
    x = line(ev, k[0], k[1]);
    x.appendChild(span(k[1], ev.text));
  }

  function track(ev) {
    if (!ev.request) return;
    var r = requests[ev.request];
    if (!r) { r = requests[ev.request] = { id: ev.request, q: "", user: ev.user, state: "busy", time: ev.time, steps: 0, sec: null }; order.unshift(r); }
    if (ev.kind === "request") r.q = (ev.data && ev.data.question) || ev.text;
    if (ev.kind === "llm_done") r.steps = ev.data.step;
    if (ev.kind === "final") { r.state = "ok"; r.sec = ev.data.seconds; }
    if (ev.kind === "error") { r.state = "err"; r.sec = ev.data.seconds; }
  }

  function renderHistory() {
    var list = $("historyList");
    if (!order.length) return;
    list.innerHTML = "";
    for (var i = 0; i < order.length && i < 50; i++) {
      (function (r) {
        var item = el("div", "req");
        item.appendChild(el("div", "q", r.q || r.id));
        var m = el("div", "m");
        m.appendChild(span("dot " + r.state));
        m.appendChild(span("", r.time));
        if (r.user) m.appendChild(span("", r.user));
        m.appendChild(span("", r.state === "busy" ? "выполняется, шаг " + (r.steps + 1) : sec(r.sec) + ", шагов: " + r.steps));
        item.appendChild(m);
        item.onclick = function () { var t = $("req-" + r.id); if (t) { autoScroll = false; $("autoBtn").className = ""; t.scrollIntoView({ behavior: "smooth", block: "start" }); } };
        list.appendChild(item);
      })(order[i]);
    }
  }

  // Сессия монитора живет до закрытия вкладки; ключ администратора в браузере не хранится
  var session = null, started = false;
  try { session = sessionStorage.getItem("mcpMonitorSession"); } catch (e) {}

  function send(method, url, body, ok, fail) {
    var x = new XMLHttpRequest();
    x.open(method, url, true);
    x.timeout = 5000;
    if (session) x.setRequestHeader("Authorization", "Bearer " + session);
    if (body) x.setRequestHeader("Content-Type", "application/json");
    x.onload = function () {
      if (x.status === 401 && url !== "/monitor/login") { showLogin(""); return; }
      var data = null;
      try { data = JSON.parse(x.responseText); } catch (e) {}
      if (x.status === 200 && data) ok(data); else fail(data);
    };
    x.onerror = function () { fail(null); }; x.ontimeout = function () { fail(null); };
    x.send(body ? JSON.stringify(body) : null);
  }
  function get(url, ok, fail) { send("GET", url, null, ok, fail); }

  function showLogin(message) {
    session = null;
    try { sessionStorage.removeItem("mcpMonitorSession"); } catch (e) {}
    $("loginError").textContent = message || "";
    $("login").className = "show";
    $("loginKey").focus();
  }
  function login(body) {
    send("POST", "/monitor/login", body, function (r) {
      session = r.session;
      try { sessionStorage.setItem("mcpMonitorSession", session); } catch (e) {}
      $("login").className = ""; $("loginKey").value = "";
      start();
    }, function (r) { showLogin((r && r.message) || "Оркестратор недоступен"); });
  }
  $("loginForm").onsubmit = function (e) {
    e.preventDefault();
    var key = $("loginKey").value.trim();
    if (key) login({ key: key });
  };

  function setOnline(v, active) {
    online = v;
    $("stateDot").className = "dot " + (!v ? "err" : (active ? "busy" : "ok"));
    $("stateText").textContent = !v ? "Нет связи с оркестратором" : (active ? "Выполняется запрос" + (active > 1 ? " (" + active + ")" : "") : "Ожидает запросов");
  }

  function poll() {
    get("/events?since=" + seq, function (r) {
      setOnline(true, r.active);
      if (r.seq < seq) { seq = 0; }
      for (var i = 0; i < r.events.length; i++) {
        var ev = r.events[i];
        allEvents.push(ev); track(ev); render(ev); total++;
        seq = ev.seq;
      }
      if (allEvents.length > 4000) allEvents = allEvents.slice(-3000);
      if (r.events.length) { renderHistory(); scrollDown(false); }
      $("evCount").textContent = num(total);
      $("updated").textContent = new Date().toLocaleTimeString();
      setTimeout(poll, r.active ? 700 : 1500);
    }, function () { setOnline(false); setTimeout(poll, 3000); });
  }

  function loadStatus() {
    get("/health", function (h) {
      $("model").textContent = h.model + "  ·  ctx " + h.num_ctx + (h.num_gpu !== null ? "  ·  GPU " + h.num_gpu + " сл." : "");
      $("toolsCount").textContent = h.tools + " инструм.";
      $("mcpUrl").textContent = h.mcp_url.replace(/^https?:\/\//, "");
      $("started").textContent = h.started;
      $("addr").textContent = h.address;
      $("termTitle").textContent = "LLM_Orchestrator.py  ·  " + h.model + "  ·  " + h.address;
    }, function () {});
    get("/tools", function (t) {
      var groups = {}, names = [], box = $("tools");
      for (var i = 0; i < t.tools.length; i++) {
        var g = t.tools[i].container || "Без контейнера";
        if (!groups[g]) { groups[g] = []; names.push(g); }
        groups[g].push(t.tools[i]);
      }
      box.innerHTML = "";
      if (!names.length) { box.appendChild(el("div", "empty", "Инструменты не загружены")); return; }
      names.sort();
      for (var j = 0; j < names.length; j++) {
        var h = el("div", "grp", names[j] + " ");
        h.appendChild(span("", "(" + groups[names[j]].length + ")"));
        box.appendChild(h);
        for (var n = 0; n < groups[names[j]].length; n++) {
          var tl = el("div", "t mono", groups[names[j]][n].name);
          tl.title = groups[names[j]][n].description;
          box.appendChild(tl);
        }
      }
    }, function () {});
  }

  // Ответ 401 останавливает опрос журнала; после входа он запускается заново
  function start() {
    if (!started) { started = true; setInterval(function () { if (session) loadStatus(); }, 30000); }
    loadStatus();
    poll();
  }

  var ticket = /[#&]ticket=([^&]+)/.exec(location.hash);
  if (ticket) {
    history.replaceState(null, "", location.pathname + location.search);
    login({ ticket: decodeURIComponent(ticket[1]) });
  } else if (session) {
    start();
  } else {
    showLogin("");
  }
})();
</script>
</body>
</html>"""


# ================== HTTP SERVER ==================
# ================== ЗАПРОСЫ ==================
MAX_BODY_BYTES = 1_000_000
MAX_QUESTION_CHARS = 20000
# Заметки администратора о базе входят в промпт каждого шага: контекст модели 4096 токенов.
MAX_INSTRUCTIONS_CHARS = 1500
# Сколько вопросов обрабатывается одновременно. Ollama отвечает по очереди, поэтому больше
# не нужно, а лимит защищает от переполнения потоками.
MAX_ACTIVE_REQUESTS = 4
# Сколько завершенных запросов хранится для GET /requests/{id}.
KEEP_FINISHED_REQUESTS = 200


REQUESTS_PATH = "/requests/"
REQUEST_NOT_FOUND = "Запрос не найден"


class RequestRejected(Exception):
    """Запрос нельзя принять: код HTTP и тело ответа."""

    def __init__(self, code: int, error: str, message: str):
        super().__init__(message)
        self.code = code
        self.body = {"error": error, "message": message}


class QuestionRequest:
    """Вопрос пользователя 1С и состояние его обработки."""

    def __init__(self, request_id: str, text: str, user: Optional[str], mcp_token: Optional[str],
                 instructions: Optional[str] = None):
        self.id = request_id
        self.text = text
        self.user = user
        self.instructions = instructions
        # Токен пользователя 1С нужен только на время обработки и наружу не отдается.
        self.mcp_token = mcp_token
        self.status = "running"
        self.created = time.time()
        self.finished: Optional[float] = None
        self.steps = 0
        self.response: Optional[Dict[str, Any]] = None
        self.cancel = threading.Event()

    def public(self, with_response: bool) -> Dict[str, Any]:
        state = {
            "request_id": self.id,
            "user": self.user,
            "status": self.status,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created)),
            "seconds": round((self.finished or time.time()) - self.created, 1),
            "steps": self.steps,
            "question": self.text[:300],
        }
        if with_response:
            state["response"] = self.response
        return state


class RequestRegistry:
    def __init__(self):
        self._items: "collections.OrderedDict[str, QuestionRequest]" = collections.OrderedDict()
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return sum(1 for r in self._items.values() if r.status == "running")

    def create(self, text: str, user: Optional[str], mcp_token: Optional[str],
               request_id: Optional[str] = None, instructions: Optional[str] = None) -> QuestionRequest:
        with self._lock:
            if sum(1 for r in self._items.values() if r.status == "running") >= MAX_ACTIVE_REQUESTS:
                raise RequestRejected(429, "busy", f"Оркестратор уже обрабатывает {MAX_ACTIVE_REQUESTS} вопроса, "
                                                   "повторите позже")
            request_id = request_id or uuid.uuid4().hex
            if request_id in self._items:
                raise RequestRejected(409, "duplicate_request_id", "Запрос с таким идентификатором уже есть")
            rec = QuestionRequest(request_id, text, user, mcp_token, instructions)
            self._items[request_id] = rec
            finished = [k for k, r in self._items.items() if r.status != "running"]
            for k in finished[:max(0, len(finished) - KEEP_FINISHED_REQUESTS)]:
                del self._items[k]
            return rec

    def get(self, request_id: Optional[str]) -> Optional[QuestionRequest]:
        with self._lock:
            return self._items.get(request_id) if request_id else None

    def snapshot(self) -> list:
        with self._lock:
            return list(self._items.values())


REQUESTS = RequestRegistry()
AVAILABLE_TOOLS: Dict[str, Any] = {}
STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")


def clean_request_id(value: Any) -> Optional[str]:
    """Идентификатор запроса от клиента: латиница, цифры и дефис, от 16 до 64 символов.
    Короткий идентификатор можно подобрать и прочитать чужой журнал, поэтому он не принимается."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9-]{16,64}", value) else None


def clean_user(value: Any) -> Optional[str]:
    """Имя пользователя 1С для журнала: без управляющих символов, до 100 символов."""
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return value[:100] or None


def clean_mcp_token(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", value.strip()):
        raise RequestRejected(400, "bad_request", "Поле mcp_token имеет неверный формат")
    return value.strip()


def clean_instructions(value: Any) -> Optional[str]:
    """Заметки администратора о базе: без управляющих символов, кроме переводов строк и табуляции."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RequestRejected(400, "bad_request", "Поле instructions должно быть строкой")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    if len(value) > MAX_INSTRUCTIONS_CHARS:
        raise RequestRejected(400, "bad_request", f"Заметки для модели длиннее {MAX_INSTRUCTIONS_CHARS} символов")
    return value or None


def new_request(data: Dict[str, Any]) -> QuestionRequest:
    """Проверяет тело вопроса и регистрирует запрос."""
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RequestRejected(400, "bad_request", "Поле 'text' обязательно")
    if len(text) > MAX_QUESTION_CHARS:
        raise RequestRejected(400, "bad_request", f"Вопрос длиннее {MAX_QUESTION_CHARS} символов")
    return REQUESTS.create(text, clean_user(data.get("user")), clean_mcp_token(data.get("mcp_token")),
                           clean_request_id(data.get("request_id")), clean_instructions(data.get("instructions")))


class ToolDialog:
    """История сообщений модели по одному вопросу и защита от повторных вызовов инструментов."""

    def __init__(self, text: str):
        self.messages = [{"role": "user", "content": text}]
        self.used_calls = set()
        self.rejected_repeats = 0           # отклоненные повторы подряд

    def reply(self, content: Any, tool: Optional[str] = None):
        self.messages.append({"role": "tool", "content": content, "tool": tool})

    def call_tool(self, answer: Dict[str, Any], step: int) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Вызывает инструмент, который запросила модель, и кладет результат в историю.
        Возвращает ответ, если обработку вопроса нужно прервать (модель зациклилась)."""
        tool = answer.get("name")
        args = answer.get("arguments", {})

        if not tool or tool not in AVAILABLE_TOOLS:
            trace("warning", f"Модель запросила неизвестный инструмент: {tool}", logging.WARNING, tool=tool)
            self.reply({"error": "НЕИЗВЕСТНЫЙ_ИНСТРУМЕНТ", "доступные_инструменты": list(AVAILABLE_TOOLS.keys())})
            return None

        # Повтором считаем только одинаковый вызов (то же имя и те же аргументы).
        # Разные аргументы при одном инструменте - нормальный последовательный опрос.
        call_hash = hash_tool_call(tool, args)
        if call_hash in self.used_calls:
            self.rejected_repeats += 1
            if self.rejected_repeats >= MAX_REJECTED_REPEATS:
                logger.warning(f"Зацикливание на инструменте: {tool}")
                return 200, create_error_response("LLM зациклился на инструменте", "loop_detected",
                                                  {"tool": tool, "steps": step}, False)
            trace("warning", f"Повторный вызов {tool} с теми же аргументами отклонен",
                  logging.WARNING, tool=tool, arguments=args)
            self.reply({"error": "ПОВТОРНЫЙ_ВЫЗОВ", "инструмент": tool,
                        "подсказка": "Этот вызов с этими аргументами уже выполнен, его результат выше. "
                                     "Измени аргументы (например, исправь запрос) или дай финальный ответ"})
            return None

        self.rejected_repeats = 0
        self.used_calls.add(call_hash)

        trace("tool_call", f"Вызов инструмента: {tool} с аргументами: {args}",
              tool=tool, arguments=args,
              arguments_text=json.dumps(args, ensure_ascii=False),
              container=AVAILABLE_TOOLS[tool].get("container") or "")
        tool_started = time.time()
        result = mcp_call_tool(tool, args)
        full_length = tool_result_length(result)

        # Обрезаем слишком большие ответы, чтобы не переполнить контекст
        result = truncate_tool_result(result)
        trace("tool_result", f"Ответ инструмента {tool}: {full_length} симв.",
              tool=tool, seconds=round(time.time() - tool_started, 2),
              chars=full_length, truncated=full_length > MAX_TOOL_RESULT_CHARS,
              is_error=bool(result.get("isError")) if isinstance(result, dict) else False,
              preview=tool_result_preview(result)[:TRACE_PREVIEW_CHARS])
        self.reply(result, tool)
        return None


def final_answer(answer: Dict[str, Any]) -> Dict[str, Any]:
    """Итоговый ответ модели в формате Success/Text/Result."""
    result = answer.get("Result", {})
    if is_error_result(result):
        return create_error_response(answer.get("Text", "Произошла ошибка"), result.get("error_type", "unknown"),
                                     result.get("details", {}), False)
    text = answer.get("Text", "Запрос успешно обработан")
    if text in ["", "OK", "Готово"]:
        text = "Запрос успешно обработан"
    return create_success_response(text, result)


def answer_question(rec: QuestionRequest) -> Tuple[int, Dict[str, Any]]:
    """Цикл модель - инструменты по одному вопросу. Возвращает код HTTP и ответ."""
    dialog = ToolDialog(rec.text)

    for step in range(1, MAX_STEPS + 1):
        if rec.cancel.is_set():
            return 200, create_error_response("Запрос отменен", "cancelled", {"steps": step - 1}, False)
        rec.steps = step

        prompt = build_prompt(dialog.messages, AVAILABLE_TOOLS, include_tools=(step == 1),
                              instructions=rec.instructions)
        trace("llm_start", f"Шаг {step}: модель формирует ответ", step=step, prompt_chars=len(prompt))
        _context.llm_stats = None
        llm_started = time.time()
        answer = call_llm(prompt)
        stats = getattr(_context, "llm_stats", None) or {}

        action = answer.get("action")
        trace("llm_done", f"Шаг {step}: ответ модели за {time.time() - llm_started:.1f} с ({action})",
              step=step, seconds=round(time.time() - llm_started, 1), action=action,
              tool=answer.get("name") if action == "call_tool" else None,
              num_ctx=OLLAMA_NUM_CTX, **stats)

        if action == "final":
            logger.info(f"Запрос завершен за {step} шагов")
            return 200, final_answer(answer)
        if action == "call_tool":
            stop = dialog.call_tool(answer, step)
            if stop:
                return stop
            continue

        trace("warning", f"Модель вернула неизвестное действие: {action}", logging.WARNING, action=action)
        dialog.reply({"error": "НЕИЗВЕСТНОЕ_ДЕЙСТВИЕ", "action": action})

    logger.warning(f"Превышено максимальное число шагов ({MAX_STEPS})")
    return 200, create_error_response("Превышено число шагов LLM", "max_steps_exceeded",
                                      {"max_steps": MAX_STEPS, "completed_steps": MAX_STEPS}, False)


def finish_request(rec: QuestionRequest, response_obj: Any) -> Dict[str, Any]:
    """Приводит ответ к формату Success/Text/Result, пишет итоговое событие и закрывает запрос."""
    if not isinstance(response_obj, dict):
        response_obj = create_error_response("Неверный формат ответа", "invalid_response_format", None, False)
    if "Success" not in response_obj:
        result = response_obj.get("Result", {})
        response_obj["Success"] = not (isinstance(result, dict) and "error_type" in result)
    response_obj.setdefault("Text", "")
    response_obj.setdefault("Result", {})

    elapsed = round(time.time() - rec.created, 1)
    trace("final" if response_obj["Success"] else "error", response_obj["Text"],
          logging.INFO if response_obj["Success"] else logging.WARNING,
          success=response_obj["Success"], seconds=elapsed, steps=rec.steps, result=response_obj["Result"])
    response_obj["RequestId"] = rec.id

    result = response_obj["Result"]
    cancelled = isinstance(result, dict) and result.get("error_type") == "cancelled"
    rec.response = response_obj
    if response_obj["Success"]:
        rec.status = "done"
    elif cancelled:
        rec.status = "cancelled"
    else:
        rec.status = "error"
    rec.finished = time.time()
    rec.mcp_token = None
    return response_obj


def process_request(rec: QuestionRequest) -> Tuple[int, Dict[str, Any]]:
    """Обрабатывает вопрос в текущем потоке: журнал, токен 1С и пользователь берутся из запроса."""
    _context.request_id = rec.id
    _context.user = rec.user
    _context.mcp_token = rec.mcp_token
    code = 200
    try:
        try:
            trace("request", f"Новый запрос: {rec.text[:100]}...", question=rec.text)
            if not AVAILABLE_TOOLS:
                load_tools()
            code, response_obj = answer_question(rec)
        except Exception as e:
            logger.exception(f"Неожиданная ошибка при обработке запроса: {e}")
            code, response_obj = 500, create_error_response(f"Внутренняя ошибка сервера: {str(e)[:100]}",
                                                            "server_error", {"error": str(e)}, False)
        response_obj = finish_request(rec, response_obj)
        logger.info(f"Запрос обработан за {time.time() - rec.created:.2f} секунд")
        return code, response_obj
    finally:
        _context.request_id = None
        _context.user = None
        _context.mcp_token = None


def start_request(rec: QuestionRequest):
    threading.Thread(target=process_request, args=(rec,), name=f"request-{rec.id[:8]}", daemon=True).start()


# ================== ДОСТУП ==================
def key_matches(value: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


class AuthGuard:
    """Ограничивает подбор ключа: после LIMIT неудачных попыток за WINDOW секунд адрес получает 429."""
    WINDOW = 300
    LIMIT = 30

    def __init__(self):
        self._fails: Dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str) -> collections.deque:
        fails = self._fails.setdefault(ip, collections.deque())
        while fails and fails[0] < time.time() - self.WINDOW:
            fails.popleft()
        return fails

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip)) >= self.LIMIT

    def failed(self, ip: str):
        with self._lock:
            self._recent(ip).append(time.time())


class MonitorSessions:
    """Сессии монитора. Вход по ключу администратора или по одноразовому билету, который
    оркестратор передает окну монитора при запуске с --monitor. Ключ администратора
    в браузере не хранится: страница держит только сессию."""
    TTL = 12 * 3600
    TICKET_TTL = 120

    def __init__(self):
        self._sessions: Dict[str, float] = {}
        self._tickets: Dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        token = "mon_" + secrets.token_hex(32)
        with self._lock:
            now = time.time()
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            self._sessions[token] = now + self.TTL
        return token

    def valid(self, token: str) -> bool:
        with self._lock:
            expires = self._sessions.get(token)
        return expires is not None and expires > time.time()

    def issue_ticket(self) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            self._tickets[ticket] = time.time() + self.TICKET_TTL
        return ticket

    def redeem_ticket(self, ticket: str) -> bool:
        with self._lock:
            expires = self._tickets.pop(ticket, None)
        return expires is not None and expires > time.time()


AUTH_GUARD = AuthGuard()
MONITOR_SESSIONS = MonitorSessions()


# ================== СОСТОЯНИЕ ==================
def model_listed(names: set) -> bool:
    return OLLAMA_MODEL in names or (":" not in OLLAMA_MODEL and f"{OLLAMA_MODEL}:latest" in names)


def ollama_status() -> Dict[str, Any]:
    """Доступна ли Ollama, скачана ли модель и загружена ли она в память."""
    parsed = urlparse(OLLAMA_URL)
    base = f"{parsed.scheme}://{parsed.netloc}"
    status = {"url": base, "model": OLLAMA_MODEL, "available": False,
              "model_present": False, "model_loaded": False, "error": None}
    try:
        tags = requests.get(base + "/api/tags", timeout=3)
        tags.raise_for_status()
        status["available"] = True
        status["model_present"] = model_listed({m.get("name") for m in tags.json().get("models", [])})
        ps = requests.get(base + "/api/ps", timeout=3)
        ps.raise_for_status()
        status["model_loaded"] = model_listed({m.get("name") for m in ps.json().get("models", [])})
    except Exception as e:
        status["error"] = str(e)[:200]
    return status


def server_status(role: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": VERSION,
        "role": role,
        "model": OLLAMA_MODEL,
        "num_ctx": OLLAMA_NUM_CTX,
        "num_gpu": OLLAMA_NUM_GPU,
        "mcp_url": MCP_URL,
        "mcp_error": MCP_STATE["error"],
        "tools": len(AVAILABLE_TOOLS),
        "active": REQUESTS.active,
        "started": STARTED_AT,
        "address": f"{ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}",
        "tls": bool(TLS_CERT),
        "ollama": ollama_status(),
    }


def run_diagnostics(mcp_token: Optional[str]) -> Dict[str, Any]:
    """Проверки для мастера настройки 1С: Ollama, связь с MCP и сквозной вызов инструмента
    с токеном пользователя."""
    report: Dict[str, Any] = {"version": VERSION, "ollama": ollama_status()}

    started = time.time()
    tools = mcp_list_tools()
    if tools:
        set_tools(tools)
    report["mcp"] = {"ok": bool(tools), "tools": len(tools), "error": MCP_STATE["error"],
                     "seconds": round(time.time() - started, 2)}

    if mcp_token:
        check = {"tool": "get_configuration_version", "ok": False, "text": None}
        if check["tool"] in AVAILABLE_TOOLS:
            data = mcp_request(mcp_payload("tools/call", {"name": check["tool"],
                                                          "arguments": {"configurationProperty": "КраткаяИнформация"}}),
                               mcp_token)
            if data and isinstance(data.get("error"), dict):
                check["text"] = str(data["error"].get("message", ""))
            elif data and isinstance(data.get("result"), dict):
                result = data["result"]
                check["ok"] = not result.get("isError")
                check["text"] = tool_result_preview(result)[:300]
            else:
                check["text"] = MCP_STATE["error"] or "HTTP-сервис 1С не ответил"
        else:
            check["text"] = "У MCP-сервера нет инструмента get_configuration_version"
        report["tool_call"] = check
    return report


# ================== HTTP ==================
class Handler(BaseHTTPRequestHandler):
    server_version = "LLM-Orchestrator"
    sys_version = ""
    # Таймаут операций с сокетом: медленный клиент не держит поток бесконечно.
    timeout = 60

    def log_message(self, format, *args):
        """Отключаем стандартное логирование запросов от BaseHTTPRequestHandler"""
        pass

    def setup(self):
        super().setup()
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    # ---------- ответы ----------
    def send_json(self, code: int, obj: Any, headers: Optional[Dict[str, str]] = None):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", headers)

    def send_body(self, code: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # ---------- доступ ----------
    def presented_key(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth[:7].lower() == "bearer " else ""

    def role(self) -> Optional[str]:
        key = self.presented_key()
        if not key:
            return None
        if key_matches(key, ADMIN_KEY) or MONITOR_SESSIONS.valid(key):
            return "admin"
        if key_matches(key, CLIENT_KEY):
            return "client"
        return None

    def authorize(self, admin: bool = False) -> Optional[str]:
        """Роль по ключу из заголовка Authorization: Bearer. Нет доступа - отправляет ответ и
        возвращает None."""
        ip = self.client_address[0]
        if AUTH_GUARD.blocked(ip):
            self.send_json(429, {"error": "too_many_attempts", "message": "Слишком много неверных ключей, "
                                                                          "повторите через несколько минут"})
            return None
        role = self.role()
        if role is None:
            AUTH_GUARD.failed(ip)
            self.send_json(401, {"error": "unauthorized",
                                 "message": "Нужен ключ оркестратора: заголовок Authorization: Bearer <ключ>"},
                           {"WWW-Authenticate": 'Bearer realm="LLM-Orchestrator"'})
            return None
        if admin and role != "admin":
            self.send_json(403, {"error": "forbidden", "message": "Нужен ключ администратора оркестратора"})
            return None
        return role

    def read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise RequestRejected(400, "bad_request", "Неверный Content-Length")
        if length <= 0:
            raise RequestRejected(400, "bad_request", "Пустое тело запроса")
        if length > MAX_BODY_BYTES:
            raise RequestRejected(413, "too_large", f"Тело запроса больше {MAX_BODY_BYTES} байт")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # UnicodeDecodeError: тело запроса не в UTF-8 (например, отправлено в CP1251)
            raise RequestRejected(400, "invalid_json", f"Невалидный JSON в запросе (ожидается UTF-8): {e}")
        if not isinstance(data, dict):
            raise RequestRejected(400, "invalid_json", "Ожидается JSON-объект")
        return data

    # ---------- маршруты ----------
    def do_GET(self):
        url = urlparse(self.path)
        routes = {
            "/": self.get_monitor,
            "/monitor": self.get_monitor,
            "/logo.svg": self.get_logo,
            "/health": self.get_health,
            "/events": self.get_events,
            "/tools": self.get_tools,
            "/requests": self.get_requests,
        }
        try:
            handler = routes.get(url.path)
            if handler:
                handler(parse_qs(url.query))
            elif url.path.startswith(REQUESTS_PATH):
                self.get_request(url.path[len(REQUESTS_PATH):])
            else:
                self.send_json(404, {"error": "not_found", "path": url.path})
        except ValueError:
            self.send_json(400, {"error": "bad_request", "path": url.path})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        url = urlparse(self.path)
        routes = {
            "/": self.post_question,
            "/requests": self.post_request,
            "/monitor/login": self.monitor_login,
            "/diagnostics": self.post_diagnostics,
        }
        try:
            handler = routes.get(url.path)
            if handler:
                handler()
            elif url.path.startswith(REQUESTS_PATH) and url.path.endswith("/cancel"):
                self.post_cancel(url.path[len(REQUESTS_PATH):-len("/cancel")])
            else:
                self.send_json(404, {"error": "not_found", "path": url.path})
        except RequestRejected as e:
            self.send_json(e.code, e.body)
        except ValueError:
            self.send_json(400, {"error": "bad_request", "path": url.path})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_request_not_found(self):
        self.send_json(404, {"error": "not_found", "message": REQUEST_NOT_FOUND})

    def get_monitor(self, params):
        self.send_body(200, MONITOR_HTML.encode("utf-8"), "text/html; charset=utf-8", {"X-Frame-Options": "DENY"})

    def get_logo(self, params):
        self.send_body(200, LOGO_SVG.encode("utf-8"), "image/svg+xml")

    def get_health(self, params):
        # Без ключа - только признак, что оркестратор запущен
        role = None
        if self.presented_key():
            role = self.role()
            if role is None:
                AUTH_GUARD.failed(self.client_address[0])
        self.send_json(200, server_status(role) if role else {"status": "ok"})

    def get_tools(self, params):
        if self.authorize():
            self.send_json(200, {"tools": [
                {"name": t.get("name"), "container": t.get("container") or "",
                 "description": t.get("description") or ""}
                for t in AVAILABLE_TOOLS.values()
            ]})

    def get_requests(self, params):
        if self.authorize(admin=True):
            self.send_json(200, {"requests": [r.public(False) for r in reversed(REQUESTS.snapshot())]})

    def get_request(self, request_id: str):
        if not self.authorize():
            return
        rec = REQUESTS.get(request_id)
        if rec is None:
            self.send_request_not_found()
        else:
            self.send_json(200, rec.public(True))

    def get_events(self, params: Dict[str, list]):
        """Журнал событий. Ключ клиента читает только события своего запроса (идентификатор
        обязателен), ключ администратора - весь журнал."""
        role = self.authorize()
        if not role:
            return
        since = int((params.get("since") or ["0"])[0] or 0)
        request_id = (params.get("request") or [None])[0]
        limit = min(int((params.get("limit") or ["500"])[0] or 500), 2000)
        if role != "admin" and not request_id:
            self.send_json(403, {"error": "forbidden",
                                 "message": "Ключ клиента читает только журнал своего запроса: укажите request"})
            return
        if role != "admin" and REQUESTS.get(request_id) is None:
            self.send_request_not_found()
            return
        self.send_json(200, {
            "seq": JOURNAL.last_seq,
            "active": REQUESTS.active,
            "events": JOURNAL.since(since, request_id, limit),
        })

    def post_request(self):
        if self.authorize():
            rec = new_request(self.read_json())
            start_request(rec)
            self.send_json(202, rec.public(False), {"Location": REQUESTS_PATH + rec.id})

    def post_cancel(self, request_id: str):
        if not self.authorize():
            return
        rec = REQUESTS.get(request_id)
        if rec is None:
            self.send_request_not_found()
            return
        rec.cancel.set()
        self.send_json(200, rec.public(False))

    def post_diagnostics(self):
        if self.authorize():
            data = self.read_json() if int(self.headers.get("Content-Length", 0) or 0) else {}
            self.send_json(200, run_diagnostics(clean_mcp_token(data.get("mcp_token"))))

    def post_question(self):
        """Синхронный вариант: ответ приходит, когда модель закончила. Ход выполнения клиент читает
        через /events?request=<свой request_id>."""
        if not self.authorize():
            return
        rec = new_request(self.read_json())
        code, response_obj = process_request(rec)
        response_obj = dict(response_obj)
        response_obj["Trace"] = JOURNAL.since(0, rec.id, JOURNAL_SIZE)
        self.send_body(code, json.dumps(response_obj, ensure_ascii=False, indent=2).encode("utf-8"),
                       "application/json; charset=utf-8")

    def monitor_login(self):
        ip = self.client_address[0]
        if AUTH_GUARD.blocked(ip):
            self.send_json(429, {"error": "too_many_attempts", "message": "Слишком много неверных попыток входа"})
            return
        data = self.read_json()
        key, ticket = data.get("key"), data.get("ticket")
        allowed = ((isinstance(key, str) and key_matches(key.strip(), ADMIN_KEY))
                   or (isinstance(ticket, str) and MONITOR_SESSIONS.redeem_ticket(ticket)))
        if not allowed:
            AUTH_GUARD.failed(ip)
            self.send_json(401, {"error": "unauthorized", "message": "Неверный ключ администратора"})
            return
        self.send_json(200, {"session": MONITOR_SESSIONS.issue(), "ttl": MonitorSessions.TTL})


class OrchestratorServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Обрыв соединения и неудачное TLS-рукопожатие - не ошибка оркестратора
        error = sys.exc_info()[1]
        # OSError покрывает ssl.SSLError, ConnectionError и TimeoutError
        if isinstance(error, OSError):
            logger.debug("Соединение %s прервано: %s", client_address, error)
            return
        super().handle_error(request, client_address)


def set_tools(tools: list):
    """Подменяет список инструментов целиком: идущие запросы дочитывают прежний словарь."""
    global AVAILABLE_TOOLS
    AVAILABLE_TOOLS = {t["name"]: t for t in tools}


def open_monitor_window(url: str):
    """Открывает монитор отдельным окном без панелей браузера (режим приложения Edge или
    Chrome), а если их нет - в браузере по умолчанию."""
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        shutil.which("chrome"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1360,860"])
            return
    webbrowser.open(url)


# ================== ЗАПУСК ==================
class ConfigError(Exception):
    pass


def default_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "LLM_Orchestrator.config.json")


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        # utf-8-sig: файл, записанный из 1С, может начинаться с метки порядка байтов
        with open(path, encoding="utf-8-sig") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"Не удалось прочитать файл конфигурации {path}: {e}")
    if not isinstance(config, dict):
        raise ConfigError(f"Файл конфигурации {path} должен содержать JSON-объект")
    unknown = sorted(set(config) - set(SETTINGS))
    if unknown:
        logger.warning("Неизвестные поля файла конфигурации %s: %s", path, ", ".join(unknown))
    return config


def save_config(path: str, config: Dict[str, Any]):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    if os.name != "nt":
        os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def generate_missing_keys(path: str, config: Dict[str, Any]) -> list:
    """Создает недостающие ключи и сохраняет их в файл конфигурации. Ключи печатаются в консоль
    один раз и в журнал событий не попадают."""
    values = read_settings(config)
    missing = [name for name in ("client_key", "admin_key") if not values[name]]
    if not missing:
        return []
    for name in missing:
        config[name] = secrets.token_hex(32)
    try:
        save_config(path, config)
        where = f"записаны в файл конфигурации {path}"
    except OSError as e:
        where = f"НЕ сохранены ({e}): действуют до остановки оркестратора"
    print(f"Созданы ключи оркестратора, {where}:")
    for name in missing:
        title = "ключ клиента (1С)" if name == "client_key" else "ключ администратора (монитор)"
        print(f"  {title}: {config[name]}")
    return missing


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def configure(config_path: str):
    config = load_config(config_path)
    generate_missing_keys(config_path, config)
    try:
        apply_settings(read_settings(config))
    except ValueError as e:
        raise ConfigError(f"Неверное значение настройки: {e}")
    if not CLIENT_KEY or not ADMIN_KEY:
        raise ConfigError("Не заданы ключи клиента и администратора: без них оркестратор не запускается")
    for name, value in (("client_key", CLIENT_KEY), ("admin_key", ADMIN_KEY)):
        if len(value) < MIN_KEY_LENGTH:
            raise ConfigError(f"Ключ {name} короче {MIN_KEY_LENGTH} символов")
    if TLS_KEY and not TLS_CERT:
        raise ConfigError("Задан закрытый ключ TLS без сертификата (tls_cert / ORCHESTRATOR_TLS_CERT)")


def tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(TLS_CERT, TLS_KEY or None)
    except OSError as e:  # ssl.SSLError - подкласс OSError
        raise ConfigError(f"Не удалось загрузить сертификат TLS {TLS_CERT}: {e}")
    return context


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="LLM-оркестратор: Ollama и MCP-сервер 1С")
    parser.add_argument("--config", default=os.environ.get("ORCHESTRATOR_CONFIG") or default_config_path(),
                        help="файл конфигурации JSON; недостающие ключи создаются и записываются в него")
    # Ключ --monitor (или ORCHESTRATOR_MONITOR=1) открывает монитор отдельным окном после запуска.
    parser.add_argument("--monitor", action="store_true", help="открыть монитор после запуска")
    args = parser.parse_args(argv)
    open_monitor = args.monitor or os.environ.get("ORCHESTRATOR_MONITOR", "") in ("1", "true", "yes")

    try:
        configure(args.config)
        context = tls_context() if TLS_CERT else None
    except ConfigError as e:
        print(f"Оркестратор не запущен: {e}", file=sys.stderr)
        return 2

    scheme = "https" if context else "http"
    monitor_host = "127.0.0.1" if ORCHESTRATOR_HOST in ("0.0.0.0", "", "::") else ORCHESTRATOR_HOST
    monitor_url = f"{scheme}://{monitor_host}:{ORCHESTRATOR_PORT}/monitor"

    print(f"🚀 LLM-Orchestrator {VERSION} запущен: {scheme}://{ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}")
    print("=" * 50)

    try:
        server = OrchestratorServer((ORCHESTRATOR_HOST, ORCHESTRATOR_PORT), Handler)
        if context:
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        elif not is_loopback(ORCHESTRATOR_HOST):
            logger.warning("Оркестратор слушает сетевой адрес без TLS: ключ и вопросы идут открытым текстом. "
                           "Задайте tls_cert и tls_key.")

        logger.info(f"Файл конфигурации: {args.config}")
        mcp_initialize()
        tools = mcp_list_tools()
        if tools:
            set_tools(tools)
            logger.info(format_tools_by_container(tools))
        else:
            logger.warning("Не загружены инструменты MCP: повторная попытка будет при первом вопросе")

        warmup_llm_async()

        logger.info(f"MCP-сервер 1С: {MCP_URL}")
        logger.info(f"✅ Сервер готов к приему запросов: {ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}")
        logger.info(f"📺 Монитор: {monitor_url} (вход по ключу администратора)")
        logger.info("⏳ LLM прогревается в фоновом режиме, первые запросы могут быть медленнее")
        if open_monitor:
            # Одноразовый билет входа во фрагменте адреса: на сервер фрагмент не уходит,
            # страница обменивает его на сессию и убирает из адреса.
            open_monitor_window(f"{monitor_url}#ticket={MONITOR_SESSIONS.issue_ticket()}")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("🛑 Сервер остановлен пользователем")
    except OSError as e:
        logger.error(f"❌ Не удалось запустить сервер на {ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
