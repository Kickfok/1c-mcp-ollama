"""Тесты оркестратора: ключи, изоляция журнала, асинхронный API, токен пользователя 1С, TLS.

Ollama и MCP-сервер 1С заменены фейковыми HTTP-серверами, оркестратор запускается отдельным
процессом с файлом конфигурации во временном каталоге.

    python -m unittest tests.test_orchestrator -v
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCHESTRATOR = os.path.join(ROOT, "orchestrator", "LLM_Orchestrator.py")
MODEL = "test-model:1b"
GOOD_TOKEN = "mcpt_" + "a" * 64
REVOKED_TOKEN = "mcpt_" + "b" * 64
CLIENT_KEY = "c" * 64
ADMIN_KEY = "d" * 64


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """HTTP-сервер в потоке с обработчиком handle(method, path, headers, body) -> (код, объект)."""

    def __init__(self, handle):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, method):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                code, obj = handle(method, self.path, self.headers, body)
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        outer.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def fake_ollama(method, path, headers, body):
    if path in ("/api/tags", "/api/ps"):
        return 200, {"models": [{"name": MODEL}]}
    prompt = body["prompt"]
    question = prompt.split("ВОПРОС ПОЛЬЗОВАТЕЛЯ:")[-1]
    if "SLOW" in question:
        time.sleep(1.5)
    if "ВОПРОС ПОЛЬЗОВАТЕЛЯ:" in prompt and "РЕЗУЛЬТАТ ИНСТРУМЕНТА" not in question:
        answer = {"action": "call_tool", "name": "get_configuration_version",
                  "arguments": {"configurationProperty": "КраткаяИнформация"}}
    else:
        answer = {"action": "final", "Text": "Конфигурация Тест 1.0", "Result": {"version": "1.0"}}
    return 200, {"response": json.dumps(answer, ensure_ascii=False), "done": True,
                 "prompt_eval_count": 100, "eval_count": 20}


class FakeMcp:
    def __init__(self):
        self.tokens = []

    def __call__(self, method, path, headers, body):
        rpc_id = body.get("id")
        if body["method"] == "initialize":
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
        if body["method"] == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{
                "name": "get_configuration_version", "description": "Версия конфигурации",
                "container": "Конфигурация",
                "inputSchema": {"type": "object", "properties": {"configurationProperty": {"type": "string"}}}}]}}
        token = headers.get("X-MCP-Token")
        self.tokens.append(token)
        if token == REVOKED_TOKEN:
            return 200, {"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": -32001, "message": "Доступ запрещен: токен не найден или отозван"}}
        return 200, {"jsonrpc": "2.0", "id": rpc_id,
                     "result": {"content": [{"type": "text", "text": "Конфигурация Тест 1.0"}], "isError": False}}


class Orchestrator:
    """Процесс оркестратора с файлом конфигурации во временном каталоге."""

    def __init__(self, config: dict, workdir: str, scheme: str = "http"):
        self.port = free_port()
        self.config_path = os.path.join(workdir, f"config-{self.port}.json")
        config = dict(config, host="127.0.0.1", port=self.port)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ORCHESTRATOR_", "OLLAMA_", "MCP_"))}
        env["PYTHONIOENCODING"] = "utf-8"
        self.log_path = os.path.join(workdir, f"out-{self.port}.log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, ORCHESTRATOR, "--config", self.config_path],
                                        stdout=self.log, stderr=subprocess.STDOUT, env=env)
        self.url = f"{scheme}://127.0.0.1:{self.port}"

    def wait_ready(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("оркестратор завершился: " + self.output())
            try:
                requests.get(self.url + "/health", timeout=1, verify=False)
                return self
            except requests.RequestException:
                time.sleep(0.2)
        raise AssertionError("оркестратор не запустился: " + self.output())

    def output(self) -> str:
        self.log.flush()
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
        self.log.close()

    def call(self, method, path, key=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if key:
            headers["Authorization"] = "Bearer " + key
        return requests.request(method, self.url + path, headers=headers, timeout=30, verify=False, **kwargs)


def wait_done(orch: Orchestrator, request_id: str, key: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = orch.call("GET", f"/requests/{request_id}", key).json()
        if state["status"] != "running":
            return state
        time.sleep(0.2)
    raise AssertionError(f"запрос {request_id} не завершился")


class OrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="orch-test-")
        cls.ollama = FakeServer(fake_ollama)
        cls.mcp_handler = FakeMcp()
        cls.mcp = FakeServer(cls.mcp_handler)
        cls.base_config = {"ollama_url": cls.ollama.url + "/api/generate", "ollama_model": MODEL,
                           "mcp_url": cls.mcp.url + "/hs/mcp"}
        cls.orch = Orchestrator(dict(cls.base_config, client_key=CLIENT_KEY, admin_key=ADMIN_KEY),
                                cls.workdir).wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.orch.stop()
        cls.ollama.stop()
        cls.mcp.stop()
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def ask(self, text="Какая версия конфигурации?", token=GOOD_TOKEN, user="Иванов", key=CLIENT_KEY):
        r = self.orch.call("POST", "/requests", key, json={"text": text, "user": user, "mcp_token": token})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["request_id"]

    def test_health_without_key_is_minimal(self):
        r = self.orch.call("GET", "/health")
        self.assertEqual(r.json(), {"status": "ok"})

    def test_health_with_key_reports_ollama(self):
        h = self.orch.call("GET", "/health", CLIENT_KEY).json()
        self.assertEqual(h["role"], "client")
        self.assertTrue(h["ollama"]["available"])
        self.assertTrue(h["ollama"]["model_present"])
        self.assertTrue(h["ollama"]["model_loaded"])
        self.assertEqual(h["tools"], 1)
        self.assertFalse(h["tls"])

    def test_protected_paths_need_key(self):
        for method, path in (("GET", "/events"), ("GET", "/tools"), ("GET", "/requests"),
                             ("POST", "/requests"), ("POST", "/"), ("POST", "/diagnostics")):
            r = self.orch.call(method, path, json={"text": "x"} if method == "POST" else None)
            self.assertEqual(r.status_code, 401, f"{method} {path}")
            r = self.orch.call(method, path, "e" * 64, json={"text": "x"} if method == "POST" else None)
            self.assertEqual(r.status_code, 401, f"{method} {path} с неверным ключом")

    def test_monitor_page_is_public_but_data_is_not(self):
        r = self.orch.call("GET", "/monitor")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(ADMIN_KEY, r.text)

    def test_async_request_forwards_token_and_tags_user(self):
        request_id = self.ask()
        state = wait_done(self.orch, request_id, CLIENT_KEY)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(state["user"], "Иванов")
        self.assertTrue(state["response"]["Success"])
        self.assertEqual(state["response"]["Text"], "Конфигурация Тест 1.0")
        self.assertIn(GOOD_TOKEN, self.mcp_handler.tokens)

        events = self.orch.call("GET", f"/events?request={request_id}", CLIENT_KEY).json()["events"]
        kinds = [e["kind"] for e in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("final", kinds)
        self.assertTrue(all(e["user"] == "Иванов" for e in events if e["kind"] != "log"))

        everything = self.orch.call("GET", "/events", ADMIN_KEY).text + json.dumps(state)
        self.assertNotIn(GOOD_TOKEN, everything, "токен 1С попал в журнал или ответ")

    def test_revoked_token_error_reaches_model(self):
        request_id = self.ask(token=REVOKED_TOKEN)
        wait_done(self.orch, request_id, CLIENT_KEY)
        events = self.orch.call("GET", f"/events?request={request_id}", CLIENT_KEY).json()["events"]
        result = [e for e in events if e["kind"] == "tool_result"][0]["data"]
        self.assertTrue(result["is_error"])
        self.assertIn("токен не найден", result["preview"])

    def test_client_key_sees_only_given_request(self):
        request_id = self.ask()
        wait_done(self.orch, request_id, CLIENT_KEY)
        self.assertEqual(self.orch.call("GET", "/events", CLIENT_KEY).status_code, 403)
        self.assertEqual(self.orch.call("GET", "/events?request=" + "f" * 32, CLIENT_KEY).status_code, 404)
        self.assertEqual(self.orch.call("GET", "/requests", CLIENT_KEY).status_code, 403)
        self.assertEqual(self.orch.call("GET", "/requests/" + "f" * 32, CLIENT_KEY).status_code, 404)

    def test_admin_sees_all_requests(self):
        request_id = self.ask(user="Петров")
        wait_done(self.orch, request_id, CLIENT_KEY)
        listed = self.orch.call("GET", "/requests", ADMIN_KEY).json()["requests"]
        self.assertIn(request_id, [r["request_id"] for r in listed])
        self.assertTrue(all("response" not in r for r in listed))
        self.assertEqual(self.orch.call("GET", "/events", ADMIN_KEY).status_code, 200)

    def test_request_id_rules(self):
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "x", "request_id": "short"})
        self.assertNotEqual(r.json()["request_id"], "short", "короткий идентификатор клиента принят")
        own = "form-" + "1" * 27
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "x", "request_id": own})
        self.assertEqual(r.json()["request_id"], own)
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "x", "request_id": own})
        self.assertEqual(r.status_code, 409)
        wait_done(self.orch, own, CLIENT_KEY)

    def test_bad_input(self):
        self.assertEqual(self.orch.call("POST", "/requests", CLIENT_KEY, json={"user": "x"}).status_code, 400)
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "x", "mcp_token": "bad token\r\nX: y"})
        self.assertEqual(r.status_code, 400)
        r = self.orch.call("POST", "/requests", CLIENT_KEY, data=b"[1]", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "x" * 20001})
        self.assertEqual(r.status_code, 400)

    def test_sync_request_returns_trace(self):
        r = self.orch.call("POST", "/", CLIENT_KEY, json={"text": "Версия?", "mcp_token": GOOD_TOKEN})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["Success"])
        self.assertTrue(body["RequestId"])
        self.assertTrue(any(e["kind"] == "final" for e in body["Trace"]))

    def test_cancel(self):
        request_id = self.ask(text="SLOW вопрос")
        r = self.orch.call("POST", f"/requests/{request_id}/cancel", CLIENT_KEY)
        self.assertEqual(r.status_code, 200)
        state = wait_done(self.orch, request_id, CLIENT_KEY)
        self.assertEqual(state["status"], "cancelled", state)

    def test_concurrency_limit(self):
        ids = [self.ask(text="SLOW очередь") for _ in range(4)]
        r = self.orch.call("POST", "/requests", CLIENT_KEY, json={"text": "пятый"})
        self.assertEqual(r.status_code, 429)
        for request_id in ids:
            wait_done(self.orch, request_id, CLIENT_KEY)

    def test_monitor_login(self):
        self.assertEqual(self.orch.call("POST", "/monitor/login", json={"key": CLIENT_KEY}).status_code, 401)
        self.assertEqual(self.orch.call("POST", "/monitor/login", json={"ticket": "x" * 43}).status_code, 401)
        session = self.orch.call("POST", "/monitor/login", json={"key": ADMIN_KEY}).json()["session"]
        self.assertEqual(self.orch.call("GET", "/requests", session).status_code, 200)
        self.assertEqual(self.orch.call("GET", "/health", session).json()["role"], "admin")

    def test_diagnostics(self):
        report = self.orch.call("POST", "/diagnostics", CLIENT_KEY, json={"mcp_token": GOOD_TOKEN}).json()
        self.assertTrue(report["mcp"]["ok"])
        self.assertTrue(report["ollama"]["model_loaded"])
        self.assertTrue(report["tool_call"]["ok"], report)
        report = self.orch.call("POST", "/diagnostics", CLIENT_KEY, json={"mcp_token": REVOKED_TOKEN}).json()
        self.assertFalse(report["tool_call"]["ok"])
        self.assertIn("токен не найден", report["tool_call"]["text"])

    def test_key_brute_force_is_throttled(self):
        orch = Orchestrator(dict(self.base_config, client_key=CLIENT_KEY, admin_key=ADMIN_KEY),
                            self.workdir).wait_ready()
        try:
            codes = [orch.call("GET", "/tools", "e" * 64).status_code for _ in range(31)]
            self.assertEqual(codes[0], 401)
            self.assertEqual(codes[-1], 429)
            self.assertEqual(orch.call("GET", "/tools", CLIENT_KEY).status_code, 429)
        finally:
            orch.stop()


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="orch-start-")
        cls.ollama = FakeServer(fake_ollama)
        cls.mcp = FakeServer(FakeMcp())
        cls.base_config = {"ollama_url": cls.ollama.url + "/api/generate", "ollama_model": MODEL,
                           "mcp_url": cls.mcp.url + "/hs/mcp"}

    @classmethod
    def tearDownClass(cls):
        cls.ollama.stop()
        cls.mcp.stop()
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_generates_keys_and_keeps_them_out_of_journal(self):
        orch = Orchestrator(self.base_config, self.workdir).wait_ready()
        try:
            with open(orch.config_path, encoding="utf-8") as f:
                config = json.load(f)
            self.assertRegex(config["client_key"], r"^[0-9a-f]{64}$")
            self.assertRegex(config["admin_key"], r"^[0-9a-f]{64}$")
            self.assertNotEqual(config["client_key"], config["admin_key"])
            journal = orch.call("GET", "/events", config["admin_key"]).text
            self.assertNotIn(config["client_key"], journal)
            self.assertNotIn(config["admin_key"], journal)
            self.assertEqual(orch.call("GET", "/tools", config["client_key"]).status_code, 200)
        finally:
            orch.stop()

    def test_short_key_refused(self):
        orch = Orchestrator(dict(self.base_config, client_key="short", admin_key=ADMIN_KEY), self.workdir)
        try:
            self.assertEqual(orch.process.wait(20), 2)
            self.assertIn("короче", orch.output())
        finally:
            orch.stop()

    @unittest.skipUnless(shutil.which("openssl"), "нет openssl для сертификата")
    def test_tls(self):
        cert = os.path.join(self.workdir, "cert.pem")
        key = os.path.join(self.workdir, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
        orch = Orchestrator(dict(self.base_config, client_key=CLIENT_KEY, admin_key=ADMIN_KEY,
                                 tls_cert=cert, tls_key=key), self.workdir, "https").wait_ready()
        try:
            self.assertTrue(orch.call("GET", "/health", CLIENT_KEY).json()["tls"])
            plain_url = orch.url.replace("https://", "http://") + "/health"
            with self.assertRaises(requests.RequestException):
                requests.get(plain_url, timeout=3)
        finally:
            orch.stop()


if __name__ == "__main__":
    unittest.main()
