"""
AI second opinion on a failed deploy.

The rule-based diagnose_failure() in wp_deploy_core runs first: it is fast,
free and decisive for the failure modes we already know (OOM, reboot,
SIGKILL, dropped SSH...). This module then sends the same evidence plus the
tail of the deploy log to Claude and asks for the most likely cause and the
concrete next commands to run.

Nothing secret leaves the machine: passwords, e-mails, the server IP and the
domain are replaced with placeholders before the request is built.
"""

import json
import os
import re

try:
    import anthropic
except ImportError:  # the tool still works without AI
    anthropic = None

LOG_TAIL_LINES = 300
LOG_TAIL_CHARS = 40_000

# Models that accept the server-side refusal fallback ("default" routing).
_FALLBACK_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2",
    "claude-sonnet-5-5",
}

SYSTEM_PROMPT = """\
Ти досвідчений Linux/DevOps-інженер. Тобі дають докази з невдалого
автоматичного встановлення WordPress (Apache, MySQL, PHP, WP-CLI, certbot)
на Ubuntu VPS через SSH:
- facts: зібрані з сервера значення (boot_id до/після, OOM-лічильники,
  мітки завершення скрипта, мінімум вільної пам'яті, CPU steal, тип
  віртуалізації, стан dpkg тощо);
- rule_conclusions: висновки, які вже зробили детерміновані правила;
- log_tail: останні рядки логу деплою.
Секрети, IP та домен замінені на плейсхолдери на кшталт <SERVER_IP>, це нормально.

Дай відповідь українською, коротко, у такому форматі:
ПРИЧИНА: одне-два речення про найімовірнішу причину.
ДОКАЗИ: 2-4 пункти з конкретними рядками логу чи значеннями facts.
ЩО ЗРОБИТИ: нумеровані кроки з точними командами для сервера.
ВПЕВНЕНІСТЬ: висока / середня / низька, і чому.
ТІКЕТ ХОСТЕРУ: так/ні і одне речення чому.

Не повторюй rule_conclusions, якщо нічого не додаєш, а уточнюй або
заперечуй їх доказами. Якщо доказів замало, прямо скажи, яких даних не вистачає
і якою командою їх зібрати. Не вигадуй рядків логу."""


def ai_available(cfg: dict) -> tuple:
    """(ok, reason). ok is True when AI analysis can run."""
    if not cfg.get("ai_enabled"):
        return False, "вимкнено в налаштуваннях"
    if anthropic is None:
        return False, "не встановлено пакет anthropic (pip install -r requirements.txt)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key")):
        return False, "немає ANTHROPIC_API_KEY (змінна середовища або config.local.json)"
    return True, ""


def redact(text: str, job: dict) -> str:
    """Replace everything identifying or secret with placeholders."""
    secrets_map = [
        (job.get("ssh_password"), "<SSH_PASSWORD>"),
        (job.get("db_password"), "<DB_PASSWORD>"),
        (job.get("wp_admin_password"), "<WP_PASSWORD>"),
        (job.get("wp_admin_email"), "<EMAIL>"),
        (job.get("ip"), "<SERVER_IP>"),
        (job.get("domain"), "<DOMAIN>"),
    ]
    # Longest first so a password that contains the domain is masked whole.
    for value, placeholder in sorted(secrets_map, key=lambda p: -len(p[0] or "")):
        if value and len(value) >= 3:
            text = text.replace(value, placeholder)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<EMAIL>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP>", text)
    return text


def _build_payload(job, facts, rule_conclusions, log_lines) -> str:
    tail = "\n".join(list(log_lines)[-LOG_TAIL_LINES:])[-LOG_TAIL_CHARS:]
    payload = {
        "facts": {k: v for k, v in facts.items() if not k.startswith("_")},
        "rule_conclusions": rule_conclusions,
        "log_tail": tail,
    }
    return redact(json.dumps(payload, ensure_ascii=False, indent=1, default=str), job)


def analyze_failure(cfg: dict, job: dict, facts: dict, rule_conclusions: list,
                    log_lines) -> list:
    """Ask Claude for a diagnosis. Returns lines to print. Never raises:
    a diagnostics problem must not hide the original failure."""
    ok, reason = ai_available(cfg)
    if not ok:
        return [f"ШІ-аналіз пропущено: {reason}."]

    model = cfg.get("ai_model") or "claude-opus-5-5"
    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key"),
        timeout=180.0,
        max_retries=2,
    )
    request = dict(
        model=model,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user",
                   "content": _build_payload(job, facts, rule_conclusions, log_lines)}],
    )
    if model in _FALLBACK_MODELS:
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"

    try:
        response = client.beta.messages.create(**request)
    except anthropic.AuthenticationError:
        return ["ШІ-аналіз: API-ключ недійсний."]
    except anthropic.RateLimitError:
        return ["ШІ-аналіз: перевищено ліміт запитів, спробуйте пізніше."]
    except anthropic.APIStatusError as e:
        return [f"ШІ-аналіз: помилка API {e.status_code}: {e.message}"]
    except anthropic.APIConnectionError as e:
        return [f"ШІ-аналіз: немає з'єднання з API ({e})."]

    if response.stop_reason == "refusal":
        return ["ШІ-аналіз: модель відмовилася відповідати на цей запит."]
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return ["ШІ-аналіз: порожня відповідь."]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if response.stop_reason == "max_tokens":
        lines.append("(відповідь обрізана)")
    return lines
