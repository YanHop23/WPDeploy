"""
AI second opinion on a failed deploy.

The rule-based diagnose_failure() in wp_deploy_core runs first: it is fast,
free and decisive for the failure modes we already know (OOM, reboot,
SIGKILL, dropped SSH...). This module then sends the same evidence plus the
tail of the deploy log to an LLM and asks for the most likely cause and the
concrete next commands to run.

Two providers, picked with "ai_provider" in config.local.json:
- "gemini": Google Gemini API, has a free tier (key from aistudio.google.com);
- "claude": Anthropic Claude API (paid, key from platform.claude.com).

Nothing secret leaves the machine: passwords, e-mails, the server IP and the
domain are replaced with placeholders before the request is built.
"""

import json
import os
import re
import urllib.error
import urllib.request

try:
    import anthropic
except ImportError:  # only needed for the "claude" provider
    anthropic = None

LOG_TAIL_LINES = 300
LOG_TAIL_CHARS = 40_000
REQUEST_TIMEOUT = 180  # seconds

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Claude models that accept the server-side refusal fallback ("default" routing).
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
і якою командою їх зібрати. Не вигадуй рядків логу. Без markdown-розмітки."""


class AIError(Exception):
    """A provider failed; the message is shown to the user as is."""


def provider_name(cfg: dict) -> str:
    return "Gemini" if cfg.get("ai_provider") == "gemini" else "Claude"


def _key(cfg: dict) -> str:
    if cfg.get("ai_provider") == "gemini":
        return os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_api_key") or ""
    return os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key") or ""


def ai_available(cfg: dict) -> tuple:
    """(ok, reason). ok is True when AI analysis can run."""
    if not cfg.get("ai_enabled"):
        return False, "вимкнено в налаштуваннях"
    provider = cfg.get("ai_provider")
    if provider not in ("gemini", "claude"):
        return False, f"невідомий ai_provider {provider!r} (має бути gemini або claude)"
    if provider == "claude" and anthropic is None:
        return False, "не встановлено пакет anthropic (pip install -r requirements.txt)"
    if not _key(cfg):
        var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
        return False, f"немає {var} (змінна середовища або config.local.json)"
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


# ---------- providers: each returns the answer text or raises AIError ----------

def _ask_gemini(cfg: dict, user_text: str) -> str:
    model = cfg.get("gemini_model") or "gemini-3.1-flash-lite"
    body = json.dumps({
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL.format(model=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": _key(cfg)},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
            detail = f"{err.get('status', '')}: {err.get('message', '')}"
        except (ValueError, AttributeError):
            detail = e.reason
        if e.code == 429:
            raise AIError("вичерпано безкоштовний ліміт Gemini, спробуйте пізніше "
                          f"або іншу модель (gemini_model). {detail}")
        if e.code in (400, 403) and "API key" in str(detail):
            raise AIError("Gemini API-ключ недійсний.")
        if e.code == 404:
            raise AIError(f"модель {model!r} не знайдена, змініть gemini_model. {detail}")
        raise AIError(f"помилка Gemini API {e.code} {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIError(f"немає з'єднання з Gemini API ({e}).")

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "невідомо")
        raise AIError(f"Gemini не відповів (blockReason: {reason}).")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        text += "\n(відповідь обрізана)"
    return text


def _ask_claude(cfg: dict, user_text: str) -> str:
    model = cfg.get("ai_model") or "claude-opus-5-5"
    client = anthropic.Anthropic(api_key=_key(cfg), timeout=float(REQUEST_TIMEOUT),
                                 max_retries=2)
    request = dict(
        model=model,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    if model in _FALLBACK_MODELS:
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"
    try:
        response = client.beta.messages.create(**request)
    except anthropic.AuthenticationError:
        raise AIError("Claude API-ключ недійсний.")
    except anthropic.RateLimitError:
        raise AIError("перевищено ліміт запитів Claude, спробуйте пізніше.")
    except anthropic.APIStatusError as e:
        raise AIError(f"помилка Claude API {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise AIError(f"немає з'єднання з Claude API ({e}).")

    if response.stop_reason == "refusal":
        raise AIError("модель відмовилася відповідати на цей запит.")
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        text += "\n(відповідь обрізана)"
    return text


def analyze_failure(cfg: dict, job: dict, facts: dict, rule_conclusions: list,
                    log_lines) -> list:
    """Ask the configured LLM for a diagnosis. Returns lines to print. Never
    raises: a diagnostics problem must not hide the original failure."""
    ok, reason = ai_available(cfg)
    if not ok:
        return [f"ШІ-аналіз пропущено: {reason}."]
    ask = _ask_gemini if cfg.get("ai_provider") == "gemini" else _ask_claude
    try:
        text = ask(cfg, _build_payload(job, facts, rule_conclusions, log_lines))
    except AIError as e:
        return [f"ШІ-аналіз: {e}"]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines or ["ШІ-аналіз: порожня відповідь."]
