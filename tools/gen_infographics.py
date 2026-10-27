"""Генерирует анимированную инфографику README в docs/assets: обложку, "Зачем", схему работы, "Для кого",
три шага установки и историю версий с планом. Все картинки в одном стиле: темный фон с сеткой, желтый
цвет 1С, фиолетовый цвет модели. Анимация - SMIL внутри SVG: GitHub показывает ее в <img>, первый кадр
читается без анимации.

    python tools/gen_infographics.py
"""
import pathlib
from xml.sax.saxutils import escape

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "docs" / "assets"

SANS = "Inter, 'Segoe UI', Arial, Helvetica, sans-serif"
MONO = "'JetBrains Mono', Consolas, 'DejaVu Sans Mono', monospace"
BG = "#0B0E1C"
CARD = "#141831"
LINE = "#262B4E"
TEXT = "#E9ECF8"
DIM = "#9AA1C9"
MUTED = "#6B7199"
YELLOW = "#FFC21A"
VIOLET = "#8B7CFF"
GREEN = "#5EE0A0"
CYAN = "#6CCFF6"
RED = "#FF6B7A"
PINK = "#F28CB1"
W = 1200

# Знак логотипа на сетке 32 x 32 (тот же, что docs/assets/logo.svg)
LOGO_MARK = """
  <rect width="32" height="32" rx="7" fill="#FFC21A"/>
  <g transform="translate(-0.7 0.6)">
    <g fill="none" stroke="#1B1F3B" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="10" cy="10" rx="4.5" ry="1.8"/>
      <path d="M5.5 10 V20 C5.5 21 7.5 21.8 10 21.8 C12.5 21.8 14.5 21 14.5 20 V10"/>
      <path d="M5.5 15 C5.5 16 7.5 16.8 10 16.8 C12.5 16.8 14.5 16 14.5 15"/>
      <path d="M14.5 15 H19.5"/>
      <path d="M22 15 L26 9 M22 15 L27 15 M22 15 L26 21"/>
    </g>
    <g fill="#1B1F3B">
      <circle cx="22" cy="15" r="2.5"/><circle cx="26.3" cy="8.6" r="1.4"/>
      <circle cx="27.4" cy="15" r="1.4"/><circle cx="26.3" cy="21.4" r="1.4"/>
    </g>
  </g>"""


def t(x, y, text, size=16, color=TEXT, weight=400, anchor="start", family=SANS, spacing=None, extra=""):
    attrs = f'x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{color}"'
    if weight != 400:
        attrs += f' font-weight="{weight}"'
    if anchor != "start":
        attrs += f' text-anchor="{anchor}"'
    if spacing:
        attrs += f' letter-spacing="{spacing}"'
    return f'<text {attrs}{extra}>{text}</text>'


def hl(text, color, weight=None):
    """Выделенный фрагмент строки (tspan)."""
    w = f' font-weight="{weight}"' if weight else ""
    return f'<tspan fill="{color}"{w}>{escape(text)}</tspan>'


def svg(height, title, desc, body, glow=(0.22, 0.18)):
    yellow_glow, violet_glow = glow
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" role="img" aria-labelledby="t d">
  <title id="t">{escape(title)}</title>
  <desc id="d">{escape(desc)}</desc>
  <defs>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
      <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#FFFFFF" stroke-opacity="0.045" stroke-width="1"/>
    </pattern>
    <radialGradient id="glowY" cx="12%" cy="0%" r="60%">
      <stop offset="0" stop-color="{YELLOW}" stop-opacity="{yellow_glow}"/>
      <stop offset="1" stop-color="{YELLOW}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowV" cx="92%" cy="100%" r="60%">
      <stop offset="0" stop-color="{VIOLET}" stop-opacity="{violet_glow}"/>
      <stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="brand" x1="0" y1="0" x2="{W}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{YELLOW}"/>
      <stop offset="1" stop-color="{VIOLET}"/>
    </linearGradient>
  </defs>
  <rect width="{W}" height="{height}" rx="18" fill="{BG}"/>
  <rect width="{W}" height="{height}" rx="18" fill="url(#grid)"/>
  <rect width="{W}" height="{height}" rx="18" fill="url(#glowY)"/>
  <rect width="{W}" height="{height}" rx="18" fill="url(#glowV)"/>
{body}
</svg>
"""


def kicker(x, y, text, color=YELLOW, rule_to=None):
    out = t(x, y, escape(text), 13, color, family=MONO, spacing="2.5")
    if rule_to:
        start = x + len(text) * 10.2 + 18
        out += f'\n  <path d="M{start:.0f} {y - 4} H{rule_to}" stroke="{LINE}" stroke-width="1.5"/>'
    return out


def pulse_on_path(path_id, color, dur, begin, r=4):
    return (f'<circle r="{r}" fill="{color}" opacity="0">'
            f'<animateMotion dur="{dur}s" begin="{begin}s" repeatCount="indefinite"><mpath href="#{path_id}"/></animateMotion>'
            f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.1;0.85;1" dur="{dur}s" begin="{begin}s" repeatCount="indefinite"/>'
            f'</circle>')


def stat_chip(x, y, w, value, label, color):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="64" rx="12" fill="{CARD}" stroke="{color}" stroke-opacity="0.55" stroke-width="1.4"/>\n'
            f'  {t(x + w / 2, y + 29, escape(value), 22, color, 800, "middle")}\n'
            f'  {t(x + w / 2, y + 50, escape(label), 12, DIM, 500, "middle", spacing="0.5")}')


# --------------------------------------------------------------------------------------------- обложка
def hero():
    body = []
    body.append(f'<svg x="48" y="34" width="40" height="40" viewBox="0 0 32 32">{LOGO_MARK}</svg>')
    body.append(t(102, 52, "1C MCP OLLAMA BRIDGE", 15, TEXT, 700, family=MONO, spacing="2.5"))
    body.append(t(102, 71, "ЛОКАЛЬНЫЙ ИИ ДЛЯ 1С:ПРЕДПРИЯТИЯ", 12, YELLOW, family=MONO, spacing="2.5"))
    body.append(t(1152, 60, "MCP · OLLAMA · MIT", 12, MUTED, anchor="end", family=MONO, spacing="2"))

    body.append(t(48, 146, "Спросите базу 1С " + hl("обычным языком", YELLOW) + hl(".", VIOLET), 54, TEXT, 800))
    body.append(t(48, 190, "Модель работает на вашей видеокарте и отвечает данными базы "
                  + hl("с правами того, кто спросил", GREEN, 700) + ".", 22, "#CDD3EE", 500))
    body.append(t(48, 226, "qwen2.5-coder:32b в Ollama · 13 инструментов 1С · форма прямо в базе · "
                  "Cursor и Claude Desktop по MCP", 16, DIM))

    # разделитель с бегущим импульсом
    body.append(f'<path id="heroLine" d="M48 256 H1152" stroke="{LINE}" stroke-width="1.5"/>')
    body.append(pulse_on_path("heroLine", YELLOW, 4.5, 0, 3))

    # карточка реального прогона
    y0 = 284
    body.append(f'<rect x="48" y="{y0}" width="1104" height="150" rx="16" fill="{CARD}" stroke="{LINE}" stroke-width="1.5"/>')
    body.append(t(76, y0 + 32, "РЕАЛЬНЫЙ ПРОГОН · 11.10.2026 · ФОРМА 1С", 12, VIOLET, family=MONO, spacing="2"))
    body.append(t(76, y0 + 66, "«Какие ошибки были в журнале регистрации за последние сутки?»", 21, TEXT, 700))
    steps = [("модель", "выбирает инструмент", VIOLET), ("read_event_log", "отбор: уровень «Ошибка»", YELLOW),
             ("1С CALL", "сеанс пользователя", GREEN), ("ответ за 46 с", "вывод и таблица", CYAN)]
    sx, sw, gap, sy = 76, 236, 24, y0 + 88
    body.append(f'<path id="heroSteps" d="M{sx + 20} {sy + 22} H{sx + 4 * sw + 3 * gap - 20}" stroke="none"/>')
    for i, (head, sub, color) in enumerate(steps):
        x = sx + i * (sw + gap)
        body.append(f'<rect x="{x}" y="{sy}" width="{sw}" height="44" rx="10" fill="#0F1328" stroke="{color}" stroke-opacity="0.35" stroke-width="1.4">'
                    f'<animate attributeName="stroke-opacity" values="0.35;1;0.35;0.35" keyTimes="0;0.12;0.3;1" dur="6s" begin="{i * 1.2}s" repeatCount="indefinite"/></rect>')
        body.append(t(x + 16, sy + 19, escape(head), 14, color, 700, family=MONO))
        body.append(t(x + 16, sy + 36, escape(sub), 12, DIM))
        if i < 3:
            ax = x + sw + 4
            body.append(f'<path d="M{ax} {sy + 22} h{gap - 8} m-5 -4 l5 4 -5 4" fill="none" stroke="{MUTED}" stroke-width="1.6" stroke-linecap="round"/>')
    body.append(pulse_on_path("heroSteps", YELLOW, 6, 0, 3.5))

    chips = [("13", "ИНСТРУМЕНТОВ", YELLOW), ("0", "ИЗМЕНЕНИЙ ДАННЫХ", GREEN), ("4 096", "ТОКЕНОВ КОНТЕКСТА", VIOLET),
             ("12 ГБ", "ВИДЕОПАМЯТИ", CYAN), ("0", "ОБЛАЧНЫХ СЕРВИСОВ", PINK), ("MIT", "ОТКРЫТЫЙ КОД", YELLOW)]
    cw, cg = 172, 14.4
    for i, (value, label, color) in enumerate(chips):
        body.append(stat_chip(48 + i * (cw + cg), 458, cw, value, label, color))

    desc = ("1C MCP Ollama Bridge: спросите базу 1С обычным языком. Модель qwen2.5-coder:32b работает в Ollama на вашей "
            "видеокарте и отвечает данными базы с правами пользователя. Пример реального прогона: вопрос об ошибках журнала "
            "регистрации, вызов read_event_log в сеансе пользователя, ответ за 46 секунд. 13 инструментов, ни один не меняет "
            "данные, контекст 4096 токенов, хватает 12 ГБ видеопамяти, облачные сервисы не нужны, лицензия MIT.")
    return svg(546, "1C MCP Ollama Bridge", desc, "\n  ".join(body))


# --------------------------------------------------------------------------------------------- зачем
WHY = [
    ("Цифру из базы просят у программиста или ждут отчет", "Вопрос обычным языком", "в форме 1С, ответ за минуту-две"),
    ("Облачный ИИ: данные базы уходят во внешний сервис", "Модель в Ollama", "на вашем компьютере или сервере"),
    ("Нейросеть видит больше, чем положено сотруднику", "Сеанс пользователя", "роли и ограничения RLS как в 1С"),
    ("ИИ уверенно пересказывает и путает цифры", "Таблица из базы целиком", "модель пишет только вывод"),
    ("Непонятно, в каких таблицах данные за период", "data_profile", "проверяет таблицы пачками за 15 секунд"),
    ("Нужный отчет есть, но его надо найти и настроить", "Готовые отчеты БСП", "модель находит вариант и задает период"),
    ("Журнал регистрации разбирают вручную", "read_event_log", "отбор по уровню, событию, пользователю"),
    ("Доработка мешает обновлять типовую конфигурацию", "Отдельное расширение", "ни одного заимствованного объекта"),
]


def why():
    body = [kicker(40, 52, "ЗАЧЕМ", YELLOW, 1160)]
    body.append(t(40, 98, "Чтобы узнать цифру из 1С, " + hl("не нужен программист", YELLOW) + " и выгрузка в Excel.",
                  30, TEXT, 700))
    top = 132
    body.append(f'<path d="M48 {top + 10} l8 8 m0 -8 l-8 8" stroke="{RED}" stroke-width="2" stroke-linecap="round"/>')
    body.append(t(70, top + 20, "КАК СЕЙЧАС", 13, RED, family=MONO, spacing="2.5"))
    body.append(f'<path d="M628 {top + 15} l4 4 8 -9" fill="none" stroke="{GREEN}" stroke-width="2" stroke-linecap="round"/>')
    body.append(t(652, top + 20, "КАК С 1C MCP OLLAMA BRIDGE", 13, GREEN, family=MONO, spacing="2.5"))
    body.append(f'<path d="M40 {top + 36} H1160" stroke="{LINE}" stroke-width="1.5"/>')
    body.append(f'<path d="M610 {top + 44} V{top + 44 + 46 * len(WHY)}" stroke="{LINE}" stroke-width="1.5"/>')
    for i, (pain, fix, detail) in enumerate(WHY):
        y = top + 74 + i * 46
        body.append(f'<path d="M50 {y - 9} l7 7 m0 -7 l-7 7" stroke="{RED}" stroke-opacity="0.8" stroke-width="1.6" stroke-linecap="round"/>')
        body.append(t(72, y, escape(pain), 16, "#C9CEE8"))
        body.append(f'<path d="M630 {y - 6} l4 4 8 -9" fill="none" stroke="{GREEN}" stroke-width="2" stroke-linecap="round">'
                    f'<animate attributeName="stroke-opacity" values="1;0.35;1" dur="8s" begin="{i}s" repeatCount="indefinite"/></path>')
        body.append(t(652, y, hl(fix, GREEN, 700) + escape(" - " + detail), 16, "#C9CEE8"))
        if i < len(WHY) - 1:
            body.append(f'<path d="M40 {y + 18} H1160" stroke="#FFFFFF" stroke-opacity="0.04" stroke-width="1"/>')
    height = top + 74 + len(WHY) * 46 + 10
    desc = "Зачем 1C MCP Ollama Bridge: восемь задач, как их решают сейчас и как с мостом. " + " ".join(
        f"{pain} - {fix}: {detail}." for pain, fix, detail in WHY)
    return svg(height, "Зачем 1C MCP Ollama Bridge", desc, "\n  ".join(body))


# --------------------------------------------------------------------------------------------- как работает
def how_it_works():
    b = []
    b.append(kicker(40, 52, "КАК ЭТО РАБОТАЕТ", YELLOW, 1160))
    b.append(t(40, 96, "Вопрос из 1С " + hl("→", MUTED) + " ответ с данными базы.", 28, TEXT, 700))
    b.append(t(40, 126, "Модель выбирает инструмент и пишет вывод. Данные читает 1С с правами пользователя.", 17, DIM))

    # три блока
    def block(x, y, w, h, color, title, sub, lines, tag):
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{CARD}" stroke="{color}" stroke-opacity="0.45" stroke-width="1.5"/>',
               f'<rect x="{x}" y="{y + 18}" width="4" height="{h - 36}" rx="2" fill="{color}"/>',
               t(x + 24, y + 38, escape(title), 21, TEXT, 800),
               t(x + 24, y + 60, escape(sub), 13, color, family=MONO)]
        for k, line in enumerate(lines):
            out.append(t(x + 24, y + 96 + k * 24, escape(line), 15, "#C9CEE8"))
        out.append(t(x + w - 20, y + h - 18, escape(tag), 12, MUTED, anchor="end", family=MONO))
        return "\n  ".join(out)

    by, bh = 156, 250
    b.append(block(40, by, 300, bh, YELLOW, "База 1С", "расширение MCP_Сервер",
                   ["Форма вопроса: ход выполнения", "и ответ с таблицей", "13 инструментов, только чтение",
                    "Вызов в сеансе пользователя:", "его роли и RLS"], "1С:Предприятие 8.3.14+"))
    b.append(block(500, by, 270, bh, CYAN, "Оркестратор", "LLM_Orchestrator.py",
                   ["Диалог с моделью до 15 шагов", "Проверка JSON, защита", "от повторов",
                    "Ключи, HTTPS, монитор", "шагов и контекста"], ":9000"))
    b.append(block(930, by, 230, bh, VIOLET, "Ollama", "qwen2.5-coder:32b",
                   ["Локальная модель", "Выбирает инструмент", "и пишет вывод",
                    "Видеокарта 12 ГБ,", "контекст 4096 токенов"], ":11434"))

    # стрелки между блоками
    arrows = [
        (340, 500, by + 58, "1", "вопрос и токен", YELLOW, 1),
        (770, 930, by + 58, "2", "промпт", VIOLET, 1),
        (930, 770, by + 112, "3", "call_tool", VIOLET, -1),
        (500, 340, by + 112, "4", "выполни инструмент", CYAN, -1),
        (340, 500, by + 166, "5", "результат", GREEN, 1),
        (770, 930, by + 166, "6", "сокращенно", VIOLET, 1),
        (930, 770, by + 220, "7", "final · ResultFrom", VIOLET, -1),
        (500, 340, by + 220, "8", "ответ и таблица", YELLOW, -1),
    ]
    cycle = 8 * 0.9
    for i, (x1, x2, y, num, label, color, direction) in enumerate(arrows):
        pid = f"arr{i}"
        b.append(f'<path id="{pid}" d="M{x1 + 6 * direction} {y} H{x2 - 8 * direction}" stroke="{color}" stroke-opacity="0.45" stroke-width="2" stroke-dasharray="4 5"/>')
        tip = x2 - 8 * direction
        b.append(f'<path d="M{tip - 7 * direction} {y - 5} L{tip} {y} L{tip - 7 * direction} {y + 5}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
        cx = min(x1, x2) + 16
        b.append(f'<circle cx="{cx}" cy="{y - 16}" r="9" fill="{color}"/>')
        b.append(t(cx, y - 12, num, 11, "#0B0E1C", 800, "middle"))
        b.append(t(cx + 15, y - 12, escape(label), 12, "#C9CEE8"))
        b.append(f'<circle r="4" fill="{color}" opacity="0"><animateMotion dur="{cycle}s" begin="{i * 0.9}s" repeatCount="indefinite" keyPoints="0;1;1" keyTimes="0;0.11;1" calcMode="linear"><mpath href="#{pid}"/></animateMotion>'
                 f'<animate attributeName="opacity" values="0;1;0;0" keyTimes="0;0.02;0.11;1" dur="{cycle}s" begin="{i * 0.9}s" repeatCount="indefinite"/></circle>')

    # нижняя полоса
    y2 = by + bh + 26
    b.append(f'<rect x="40" y="{y2}" width="300" height="64" rx="14" fill="{CARD}" stroke="{LINE}" stroke-width="1.5"/>')
    b.append(t(60, y2 + 27, "Cursor, Claude Desktop", 15, TEXT, 700))
    b.append(t(60, y2 + 48, "/hs/mcp с личным токеном", 13, DIM, family=MONO))
    b.append(f'<rect x="364" y="{y2}" width="796" height="64" rx="14" fill="#16143A" stroke="{VIOLET}" stroke-opacity="0.5" stroke-width="1.5"/>')
    b.append(t(386, y2 + 27, "Модель не переписывает данные", 15, TEXT, 700))
    b.append(t(386, y2 + 48, "в шаге 7 она называет инструмент, оркестратор подставляет его полную таблицу", 13, DIM))

    pills = [("Права пользователя", GREEN), ("Только чтение", YELLOW), ("Данные в вашей сети", VIOLET),
             ("Ход выполнения в форме и мониторе", CYAN)]
    px = 40
    py = y2 + 92
    for label, color in pills:
        pw = len(label) * 8.6 + 44
        b.append(f'<rect x="{px}" y="{py}" width="{pw:.0f}" height="34" rx="17" fill="{color}" fill-opacity="0.1" stroke="{color}" stroke-opacity="0.5"/>')
        b.append(f'<circle cx="{px + 18}" cy="{py + 17}" r="4" fill="{color}"><animate attributeName="opacity" values="1;0.3;1" dur="2.4s" repeatCount="indefinite"/></circle>')
        b.append(t(px + 32, py + 22, escape(label), 14, color, 600))
        px += pw + 14
    height = py + 58
    desc = ("Как работает 1C MCP Ollama Bridge. 1: форма 1С отправляет оркестратору вопрос и токен пользователя. 2: оркестратор "
            "передает модели вопрос, инструменты и заметки администратора. 3: модель отвечает call_tool. 4: оркестратор просит форму "
            "выполнить инструмент. 5: форма выполняет его в сеансе пользователя, с ролями и RLS, и возвращает результат. 6: модель "
            "получает результат в сокращенном виде. 7: модель отвечает final и называет инструмент в ResultFrom. 8: форма выводит "
            "ответ и полную таблицу. Внешние MCP-клиенты подключаются к /hs/mcp с личным токеном.")
    return svg(height, "Как работает 1C MCP Ollama Bridge", desc, "\n  ".join(b))


# --------------------------------------------------------------------------------------------- для кого
AUDIENCE = [
    ("Руководителям", YELLOW, "Выручка и прибыль",
     ["Модель находит готовый отчет БСП,", "задает период и отвечает выводом", "в одну-три фразы и таблицей."]),
    ("Аналитикам", CYAN, "Где данные и сколько их",
     ["data_profile показывает таблицы", "с записями за период, read_table", "считает итоги без языка запросов."]),
    ("Программистам", VIOLET, "Метаданные и запросы",
     ["Структура объектов, кто пишет", "в регистр, образцы запросов", "из схем отчетов, run_query."]),
    ("Администраторам", GREEN, "Журнал и диагностика",
     ["Ошибки журнала регистрации,", "10 проверок подключения,", "монитор шагов модели."]),
    ("Безопасности", RED, "Данные не покидают сеть",
     ["Инструменты только читают,", "права и RLS пользователя,", "ключи в хранилище БСП."]),
    ("Разработчикам", PINK, "Открытый протокол MCP",
     ["Новый инструмент - обработка", "и два метода. Подключаются", "Cursor и Claude Desktop."]),
]


def audience():
    b = [kicker(40, 52, "ДЛЯ КОГО", YELLOW, 1160)]
    b.append(t(40, 96, "Одна форма в базе 1С. " + hl("Каждый получает ответ по своей задаче.", YELLOW), 28, TEXT, 700))
    cw, ch, gx, gy = 362, 168, 17, 18
    for i, (who, color, head, lines) in enumerate(AUDIENCE):
        col, row = i % 3, i // 3
        x, y = 40 + col * (cw + gx), 128 + row * (ch + gy)
        b.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="16" fill="{CARD}" stroke="{LINE}" stroke-width="1.5"/>')
        b.append(f'<rect x="{x}" y="{y + 20}" width="4" height="{ch - 40}" rx="2" fill="{color}">'
                 f'<animate attributeName="opacity" values="1;0.35;1" dur="6s" begin="{i}s" repeatCount="indefinite"/></rect>')
        b.append(t(x + 26, y + 40, escape(who.upper()), 12, color, family=MONO, spacing="2"))
        b.append(t(x + 26, y + 70, escape(head), 21, TEXT, 800))
        for k, line in enumerate(lines):
            b.append(t(x + 26, y + 100 + k * 22, escape(line), 15, "#C9CEE8"))
    height = 128 + 2 * ch + gy + 52
    b.append(t(40, height - 22, "13 инструментов · ни один не меняет данные · форма работает в тонком клиенте и веб-клиенте",
               13, MUTED, family=MONO))
    desc = "Для кого 1C MCP Ollama Bridge. " + " ".join(f"{who}: {head}. {' '.join(lines)}" for who, _, head, lines in AUDIENCE)
    return svg(height, "Для кого 1C MCP Ollama Bridge", desc, "\n  ".join(b))


# --------------------------------------------------------------------------------------------- три шага
STEPS = [
    ("1", "Расширение в базу", YELLOW, ["1C-MCP-Ollama-Bridge.cfe", "публикация /hs/mcp", "роль для пользователей"]),
    ("2", "Модель и оркестратор", VIOLET, ["ollama pull qwen2.5-coder:32b", "python LLM_Orchestrator.py", "ключи создаются сами"]),
    ("3", "Вопрос в форме", GREEN, ["Анализ конфигурации MCP", "адрес и ключ в настройках", "Ctrl+Enter - и ответ"]),
]


def quickstart():
    b = [kicker(40, 52, "ТРИ ШАГА", YELLOW, 1160)]
    b.append(t(40, 96, "Расширение " + hl("→", MUTED) + " модель " + hl("→", MUTED) + " " + hl("первый ответ.", GREEN), 28, TEXT, 700))
    cw, gap, y = 340, 70, 132
    for i, (num, title, color, lines) in enumerate(STEPS):
        x = 40 + i * (cw + gap)
        b.append(f'<rect x="{x}" y="{y}" width="{cw}" height="172" rx="16" fill="{CARD}" stroke="{color}" stroke-opacity="0.4" stroke-width="1.5">'
                 f'<animate attributeName="stroke-opacity" values="0.4;1;0.4;0.4" keyTimes="0;0.15;0.35;1" dur="6s" begin="{i * 2}s" repeatCount="indefinite"/></rect>')
        b.append(f'<circle cx="{x + 40}" cy="{y + 42}" r="18" fill="{color}"/>')
        b.append(t(x + 40, y + 49, num, 18, "#0B0E1C", 800, "middle"))
        b.append(t(x + 70, y + 49, escape(title), 21, TEXT, 800))
        for k, line in enumerate(lines):
            family = MONO if k < 2 and i < 2 else SANS
            b.append(t(x + 24, y + 96 + k * 26, escape(line), 14, "#C9CEE8", family=family))
        if i < 2:
            pid = f"step{i}"
            ax = x + cw + 8
            b.append(f'<path id="{pid}" d="M{ax} {y + 86} H{ax + gap - 16}" stroke="{MUTED}" stroke-width="2" stroke-dasharray="4 5"/>')
            b.append(f'<path d="M{ax + gap - 23} {y + 80} l7 6 -7 6" fill="none" stroke="{MUTED}" stroke-width="2" stroke-linecap="round"/>')
            b.append(pulse_on_path(pid, color, 2, i, 4))
    height = y + 172 + 54
    b.append(t(40, height - 22, "Подробно: docs/INSTALL.md и инструкция администратора", 13, MUTED, family=MONO))
    b.append(t(1160, height - 22, "MIT · бесплатно", 13, MUTED, anchor="end", family=MONO))
    desc = "Три шага до первого ответа. " + " ".join(f"{num}. {title}: {', '.join(lines)}." for num, title, _, lines in STEPS)
    return svg(height, "Установка 1C MCP Ollama Bridge в три шага", desc, "\n  ".join(b))


# --------------------------------------------------------------------------------------------- история и план
TAGS = {"Модель": VIOLET, "Данные": YELLOW, "Безопасность": GREEN, "Форма": CYAN, "Сборка": "#C3C8EA",
        "Документация": PINK}

VERSIONS = [
    ("1.0.0", "04.10.2026", "Первый ответ модели из базы", ["Модель", "Форма", "Сборка"], [
        "Оркестратор для Ollama: диалог с моделью и вызов инструментов 1С",
        "Форма \"Анализ конфигурации MCP\": вопрос обычным языком прямо в базе",
        "Проверка исходников и тест опубликованного сервера в GitHub Actions"]),
    ("1.1.0", "04.10.2026", "Видно, что делает модель", ["Модель", "Форма"], [
        "Монитор оркестратора: шаги модели, время, заполнение контекста",
        "Ответ в форме оформлен в HTML, ход выполнения рядом с ответом",
        "Повтор запроса к Ollama при сбое загрузки модели"]),
    ("1.2.0-beta", "05.10.2026", "Безопасность", ["Безопасность", "Сборка"], [
        "Ключи и HTTPS оркестратора, токен пользователя 1С на время вопроса",
        "Диагностика подключения из 10 проверок с подсказками",
        "Описание HTTP-сервиса в OpenAPI, роли для администратора и пользователей"]),
    ("1.2.0", "05.10.2026", "Два режима работы", ["Форма", "Модель"], [
        "Серверный режим и локальный: оркестратор на компьютере пользователя",
        "Автозапуск оркестратора из формы, кнопка \"Остановить\"",
        "Форма не запускает программы на сервере 1С"]),
    ("1.2.1", "05.10.2026", "Инструкции", ["Документация", "Форма"], [
        "Инструкции пользователя и администратора со снимками экрана",
        "\"Мои настройки\": пути к ollama.exe и python.exe с выбором файла",
        "Исправления формы: примеры вопросов, кнопки, диагностика"]),
    ("1.3.0", "10.10.2026", "Модель пишет запросы", ["Данные", "Модель", "Безопасность"], [
        "run_query и validate_query: запрос на языке 1С с проверкой прав по каждой таблице",
        "find_report_queries: образцы запросов из схем компоновки отчетов",
        "Заметки администратора для модели, результат запроса таблицей в форме"]),
    ("1.4.0", "11.10.2026", "Ответы данными базы", ["Данные", "Безопасность", "Модель"], [
        "Инструменты выполняются в сеансе пользователя: его роли и ограничения на уровне записей",
        "read_table, data_profile, журнал регистрации, готовые отчеты БСП с периодом и отборами",
        "Модель называет инструмент (ResultFrom), полная таблица попадает в ответ без пересказа"]),
]

NEXT = [
    ("Руководителям", YELLOW, ["Продажи, долги и деньги", "через готовые отчеты", "Сравнение периодов и графики"]),
    ("Аналитикам", CYAN, ["Администратор отбирает", "отчеты для модели", "Выгрузка ответа в Excel"]),
    ("Программистам", VIOLET, ["Поиск по коду модулей", "Объяснение и проверка", "запросов"]),
    ("Администраторам", GREEN, ["Оркестратор как служба", "Windows", "Журнал вопросов с отбором"]),
]


def roadmap():
    b = [kicker(40, 52, "ИСТОРИЯ И ПЛАН", YELLOW, 1160)]
    b.append(t(40, 96, "От MCP-сервера в расширении " + hl("до ответов данными базы", YELLOW) + ".", 28, TEXT, 700))
    rail, card_x = 76, 250
    card_w = W - card_x - 40
    step, top = 112, 206
    rail_end = top + step * (len(VERSIONS) - 1) + 44
    b.append(f'<path d="M{rail} 172 V{rail_end}" stroke="{YELLOW}" stroke-opacity="0.45" stroke-width="3" stroke-linecap="round"/>')
    b.append(f'<circle r="5" fill="{YELLOW}"><animateMotion dur="7s" repeatCount="indefinite" path="M{rail} 172 V{rail_end}"/>'
             f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.05;0.9;1" dur="7s" repeatCount="indefinite"/></circle>')

    # основа
    b.append(f'<circle cx="{rail}" cy="160" r="10" fill="{BG}" stroke="#C3C8EA" stroke-width="2.5"/>')
    b.append(t(100, 156, "Основа", 18, "#C3C8EA", 800))
    b.append(t(100, 174, "MIT", 12, MUTED, family=MONO))
    b.append(f'<rect x="{card_x}" y="134" width="{card_w}" height="52" rx="12" fill="none" stroke="{LINE}" stroke-width="1.5" stroke-dasharray="6 5"/>')
    b.append(t(card_x + 20, 165, hl("MCP-сервер в расширении 1С", TEXT, 700) + escape(
        " - проект vladimir-kharin/1c_mcp Владимира Харина, лицензия MIT"), 14, DIM))

    for i, (ver, date, title, tags, items) in enumerate(VERSIONS):
        y = top + i * step
        last = i == len(VERSIONS) - 1
        color = VIOLET if last else YELLOW
        b.append(f'<rect x="{card_x}" y="{y}" width="{card_w}" height="{step - 14}" rx="14" fill="{"#17153A" if last else CARD}" stroke="{VIOLET if last else LINE}" stroke-width="1.5"/>')
        b.append(f'<circle cx="{rail}" cy="{y + 40}" r="{10 if last else 8}" fill="{color}" stroke="{BG}" stroke-width="3"/>')
        if last:
            b.append(f'<circle cx="{rail}" cy="{y + 40}" r="12" fill="none" stroke="{VIOLET}" stroke-width="2">'
                     f'<animate attributeName="r" values="12;22;12" dur="2.6s" repeatCount="indefinite"/>'
                     f'<animate attributeName="opacity" values="0.8;0;0.8" dur="2.6s" repeatCount="indefinite"/></circle>')
        b.append(t(100, y + 40, ver, 19, color, 800))
        b.append(t(100, y + 59, date, 12, MUTED, family=MONO))
        b.append(t(card_x + 20, y + 28, escape(title), 16, TEXT, 800))
        tx = card_x + card_w - 16
        for tag in reversed(tags):
            c = TAGS[tag]
            tw = 14 + 7.2 * len(tag)
            tx -= tw
            b.append(f'<rect x="{tx:.0f}" y="{y + 12}" width="{tw:.0f}" height="20" rx="10" fill="{c}" fill-opacity="0.12" stroke="{c}" stroke-opacity="0.55"/>')
            b.append(t(round(tx + tw / 2), y + 26, tag, 11, c, 600, "middle"))
            tx -= 8
        for k, item in enumerate(items):
            ly = y + 50 + 18 * k
            b.append(f'<circle cx="{card_x + 25}" cy="{ly - 4}" r="2.4" fill="{color}"/>')
            b.append(t(card_x + 36, ly, escape(item), 13, "#C9CEE8"))

    ny = top + step * len(VERSIONS) + 20
    b.append(kicker(40, ny + 8, "ДАЛЬШЕ", GREEN, 1160))
    cw = (W - 80 - 3 * 16) / 4
    for j, (who, color, lines) in enumerate(NEXT):
        x = 40 + j * (cw + 16)
        y = ny + 28
        b.append(f'<rect x="{x:.0f}" y="{y}" width="{cw:.0f}" height="128" rx="14" fill="{CARD}" stroke="{LINE}" stroke-width="1.5"/>')
        b.append(f'<rect x="{x:.0f}" y="{y}" width="{cw:.0f}" height="5" rx="2.5" fill="{color}"/>')
        b.append(t(round(x + 20), y + 36, escape(who), 16, color, 800))
        for k, line in enumerate(lines):
            b.append(t(round(x + 20), y + 64 + k * 20, escape(line), 13, "#C9CEE8"))
    height = ny + 28 + 128 + 36
    b.append(t(40, height - 14, "порядок задач зависит от отзывов в issues", 12, MUTED, family=MONO))
    desc = ("История версий 1C MCP Ollama Bridge и план развития. Основа: MCP-сервер vladimir-kharin/1c_mcp (MIT). "
            + " ".join(f"{ver} ({date}) {title}: {'; '.join(items)}." for ver, date, title, _, items in VERSIONS)
            + " Дальше: " + " ".join(f"{who}: {' '.join(lines)}." for who, _, lines in NEXT))
    return svg(height, "История версий и план 1C MCP Ollama Bridge", desc, "\n  ".join(b))


# --------------------------------------------------------------------------------------------- обложка для соцсетей
def social():
    height = 640
    body = [f'<svg x="80" y="96" width="120" height="120" viewBox="0 0 32 32">{LOGO_MARK}</svg>',
            t(80, 300, "1C MCP Ollama Bridge", 76, TEXT, 800),
            t(80, 370, "Спросите базу 1С " + hl("обычным языком", YELLOW) + ".", 40, "#CDD3EE", 600),
            t(80, 430, "Локальная модель в Ollama · права пользователя 1С · 13 инструментов · MIT", 24, DIM),
            f'<path d="M80 486 H1200" stroke="url(#brand)" stroke-width="3" stroke-linecap="round" opacity="0.7"/>',
            t(80, 548, "github.com/Kickfok/1c-mcp-ollama", 26, MUTED, family=MONO)]
    out = svg(height, "1C MCP Ollama Bridge", "Обложка репозитория", "\n  ".join(body))
    return out.replace(f'width="{W}" height="{height}" viewBox="0 0 {W} {height}"', f'width="1280" height="{height}" viewBox="0 0 1280 {height}"').replace(
        f'<rect width="{W}" height="{height}"', f'<rect width="1280" height="{height}"').replace('rx="18" fill', 'rx="0" fill')


def main():
    files = {"hero.svg": hero(), "why.svg": why(), "how-it-works.svg": how_it_works(), "audience.svg": audience(),
             "quickstart.svg": quickstart(), "roadmap.svg": roadmap(), "social-preview.svg": social()}
    for name, content in files.items():
        (ASSETS / name).write_text(content, encoding="utf-8", newline="\n")
        print(name)


if __name__ == "__main__":
    main()
