import json
import logging
import re
import sqlite3
from datetime import date
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from backend.config import settings
from backend.models import AIChatRequest, AIProposedAction
from backend.scheduler_engine import (
    build_schedule_context_json,
    find_replacement_candidates,
)

logger = logging.getLogger(__name__)

_THINKING_MODEL_PREFIXES = ("qwen3", "deepseek-r1")

_SESSION_HISTORY: dict[str, list[dict[str, str]]] = {}


def _strip_thinking_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json_object(raw: str) -> dict[str, Any]:
    cleaned = _strip_thinking_tags(raw)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Model nie zwrócił obiektu JSON.")
    return parsed


def _valid_iso_date(value: Any) -> Optional[str]:
    """Model routera bywa niedokładny — przepuszczamy tylko poprawne daty YYYY-MM-DD."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _describe_ollama_error(exc: httpx.HTTPError, models: list[str]) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return (
            f"Nie udało się połączyć z serwerem **Ollama** (`{settings.ollama_base_url}`). "
            "Uruchom Ollamę i spróbuj ponownie."
        )
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"Ollama działa, ale model nie odpowiedział w ciągu {settings.ollama_timeout_s:.0f} s. "
            "Spróbuj ponownie albo wybierz mniejszy model w ustawieniach potoku."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("error", "")
        except Exception:
            detail = exc.response.text[:200]
        return (
            f"Ollama zwróciła błąd HTTP {exc.response.status_code}: {detail or 'brak szczegółów'}. "
            f"Używane modele: {', '.join(f'`{m}`' for m in models)}."
        )
    return f"Przerwane połączenie z Ollamą ({type(exc).__name__}): {exc or 'brak szczegółów'}."


async def _ollama_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    json_mode: bool = False,
    temperature: float = 0.2,
    num_ctx: Optional[int] = None,
    num_predict: int = 800,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx or settings.ollama_num_ctx,
            "num_predict": num_predict,
        },
    }
    if json_mode:
        payload["format"] = "json"
    if model.lower().startswith(_THINKING_MODEL_PREFIXES):
        payload["think"] = False

    resp = await client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data.get("message", {}).get("content", "")


async def _run_router(
    client: httpx.AsyncClient,
    model: str,
    user_message: str,
    miesiac: str,
) -> dict[str, Any]:
    system_prompt = (
        "Jesteś routerem intencji w systemie grafików pracy Kostecki Labs. "
        "Zwróć WYŁĄCZNIE poprawny obiekt JSON z kluczami:\n"
        '- "intent": jedna z wartości ["INFO", "FIND_REPLACEMENT", "ADD_ABSENCE", '
        '"SWAP_SHIFTS", "ASSIGN_SHIFT", "ADD_PREFERENCE", "REGENERATE_SCHEDULE"]\n'
        '- "data": string YYYY-MM-DD lub null (domyślny miesiąc: ' + miesiac + ")\n"
        '- "zmiana": "D" albo "N" albo null\n'
        '- "pracownik": imię i nazwisko lub null\n'
        '- "potrzebuje_kandydatow": boolean (true jeśli pytanie dotyczy zastępstwa, '
        "urlopu/chorobowego na konkretnej zmianie lub obsady luki)"
    )
    try:
        raw = await _ollama_chat(
            client,
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            json_mode=True,
            temperature=0.0,
            num_ctx=2048,
            num_predict=150,
        )
        return _extract_json_object(raw)
    except httpx.TransportError:
        raise  # Ollama nie działa — nie ma sensu czekać na kolejne modele
    except Exception:
        # Deterministyczny fallback na wypadek błędu małego modelu routera
        date_m = re.search(r"\d{4}-\d{2}-\d{2}", user_message)
        low = user_message.lower()
        intent = "INFO"
        if any(w in low for w in ["zastęp", "kto może", "wskoczy", "obsadz"]):
            intent = "FIND_REPLACEMENT"
        elif any(w in low for w in ["urlop", "chorobow", "l4", "nieobecn"]):
            intent = "ADD_ABSENCE"
        elif any(w in low for w in ["zamień", "zamiana"]):
            intent = "SWAP_SHIFTS"
        elif any(w in low for w in ["preferencj", "woli", "nie chce"]):
            intent = "ADD_PREFERENCE"
        elif any(w in low for w in ["wygeneruj", "przelicz grafik", "napraw grafik"]):
            intent = "REGENERATE_SCHEDULE"

        zmiana = "N" if re.search(r"\b(noc|nocn|nock|zmian[aęy]\s*n)\b", low) else (
            "D" if re.search(r"\b(dzien|dzienn|zmian[aęy]\s*d)\b", low) else None
        )
        return {
            "intent": intent,
            "data": date_m.group(0) if date_m else None,
            "zmiana": zmiana,
            "pracownik": None,
            "potrzebuje_kandydatow": intent in ("FIND_REPLACEMENT", "ADD_ABSENCE"),
        }


async def _run_reasoning(
    client: httpx.AsyncClient,
    model: str,
    user_message: str,
    miesiac: str,
    schedule_context_json: str,
    replacements_json: Optional[str],
    history: list[dict[str, str]],
) -> dict[str, Any]:
    system_prompt = """Jesteś analitykiem grafików pracy laboratorium Kostecki Labs.
Zasady laboratorium:
- Zmiany trwają 12h: D (dzienna) oraz N (nocna).
- Na każdej zmianie D i N musi być minimum 1 pracownik ze stanowiskiem 'starszy'.
- Wymagany odpoczynek dobowy: minimum 11h (zakaz zmiany D bezpośrednio po zmianie N).
- Pracownik nie może mieć przypisanej zmiany w trakcie nieobecności (urlop, chorobowe, niedostępny).

Przeanalizuj zapytanie użytkownika na podstawie aktualnego stanu grafiku (JSON).
Zwróć WYŁĄCZNIE poprawny obiekt JSON o strukturze:
{
  "wnioski_analityczne": "zwięzłe fakty liczbowe, wykryte kolizje, rekomendacje",
  "proposed_action": null LUB obiekt:
  {
    "action_type": "ADD_ABSENCE" | "SWAP_SHIFTS" | "ASSIGN_SHIFT" | "ADD_PREFERENCE" | "REGENERATE_SCHEDULE",
    "title": "Krótki tytuł przycisku akcji",
    "description": "Co dokładnie zostanie zmienione w bazie",
    "payload": { ... }
  }
}

Wymagane pola w `payload` dla poszczególnych `action_type`:
- ADD_ABSENCE: {"pracownik_id": int, "od": "YYYY-MM-DD", "do": "YYYY-MM-DD", "typ": "urlop"|"chorobowe"|"niedostępny", "notatka": str, "usun_kolidujace_zmiany": true}
- SWAP_SHIFTS: {"miesiac": "YYYY-MM", "data": "YYYY-MM-DD", "zmiana": "D"|"N", "z_pracownik_id": int, "na_pracownik_id": int, "zablokuj": true}
- ASSIGN_SHIFT: {"miesiac": "YYYY-MM", "data": "YYYY-MM-DD", "pracownik_id": int, "zmiana": "D"|"N"|null, "zablokowana": true}
- ADD_PREFERENCE: {"pracownik_id": int, "preferencja": str}
- REGENERATE_SCHEDULE: {"miesiac": "YYYY-MM", "zachowaj_zablokowane": true, "tryb_naprawy": bool}

Ustaw `proposed_action` TYLKO wtedy, gdy użytkownik wyraźnie prosi o wprowadzenie zmiany lub zastępstwa i masz jednoznaczne ID pracowników oraz daty. W przeciwnym razie ustaw `null`."""

    context_block = f"AKTUALNY GRAFIK ({miesiac}):\n{schedule_context_json}"
    if replacements_json:
        context_block += f"\n\nRANKING KANDYDATÓW NA ZASTĘPSTWO:\n{replacements_json}"

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-4:])
    messages.append(
        {
            "role": "user",
            "content": f"{context_block}\n\nZAPYTANIE UŻYTKOWNIKA:\n{user_message}",
        }
    )

    raw = await _ollama_chat(client, model, messages, json_mode=True, temperature=0.1)
    try:
        return _extract_json_object(raw)
    except ValueError:  # także JSONDecodeError, np. ucięta odpowiedź
        logger.warning("Model %s zwrócił niepoprawny JSON: %.200s", model, raw)
        return {"wnioski_analityczne": _strip_thinking_tags(raw), "proposed_action": None}


async def _run_writer(
    client: httpx.AsyncClient,
    model: str,
    user_message: str,
    reasoning_result: dict[str, Any],
    history: list[dict[str, str]],
) -> str:
    system_prompt = (
        "Jesteś pomocnym asystentem planowania grafików w Kostecki Labs. "
        "Odpowiadaj po polsku, konkretnie, zwięźle i uprzejmie, używając formatowania Markdown (listy punktowane, pogrubienia). "
        "Opieraj się wyłącznie na dostarczonych wnioskach analitycznych — nie zmyślaj dat, godzin ani nazwisk. "
        "Jeśli przygotowano propozycję akcji (`proposed_action`), poinformuj użytkownika, że może ją zatwierdzić jednym kliknięciem."
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-4:])
    messages.append(
        {
            "role": "user",
            "content": (
                f"Pytanie użytkownika: {user_message}\n\n"
                f"Zweryfikowane dane analityczne:\n{json.dumps(reasoning_result, ensure_ascii=False, indent=2)}"
            ),
        }
    )
    raw = await _ollama_chat(client, model, messages, json_mode=False, temperature=0.35, num_predict=500)
    return _strip_thinking_tags(raw)


async def process_ai_chat(
    conn: sqlite3.Connection,
    request: AIChatRequest,
) -> dict[str, Any]:
    router_model = request.router_model or settings.ollama_router_model
    reasoning_model = request.reasoning_model or settings.ollama_reasoning_model
    writer_model = request.writer_model or settings.ollama_writer_model

    history = _SESSION_HISTORY.setdefault(request.session_id, [])
    schedule_context_json = build_schedule_context_json(conn, request.miesiac)

    try:
        timeout = httpx.Timeout(settings.ollama_timeout_s, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            route = await _run_router(client, router_model, request.message, request.miesiac)

            replacements_json: Optional[str] = None
            target_date = _valid_iso_date(route.get("data"))
            target_shift = route.get("zmiana")
            if target_date and target_shift in ("D", "N"):
                candidates = find_replacement_candidates(conn, target_date, target_shift)
                replacements_json = json.dumps(
                    [c.model_dump() for c in candidates[:6]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            reasoning_result = await _run_reasoning(
                client=client,
                model=reasoning_model,
                user_message=request.message,
                miesiac=request.miesiac,
                schedule_context_json=schedule_context_json,
                replacements_json=replacements_json,
                history=history,
            )

            proposed_action: Optional[AIProposedAction] = None
            raw_action = reasoning_result.get("proposed_action")
            if isinstance(raw_action, dict):
                try:
                    proposed_action = AIProposedAction(**raw_action)
                except ValidationError:
                    proposed_action = None

            try:
                reply_text = await _run_writer(
                    client=client,
                    model=writer_model,
                    user_message=request.message,
                    reasoning_result=reasoning_result,
                    history=history,
                )
            except httpx.HTTPError as exc:
                # Analiza jest już gotowa — pokazujemy ją zamiast błędu
                logger.warning("Model %s nie odpowiedział: %r", writer_model, exc)
                reply_text = ""
            if not reply_text:
                reply_text = str(
                    reasoning_result.get("wnioski_analityczne") or "Model nie zwrócił odpowiedzi."
                )

            history.append({"role": "user", "content": request.message})
            history.append({"role": "assistant", "content": reply_text})
            if len(history) > 12:
                del history[:-12]

            return {
                "reply": reply_text,
                "intent": route.get("intent", "INFO"),
                "proposed_action": proposed_action.model_dump() if proposed_action else None,
                "models_used": {
                    "router": router_model,
                    "reasoning": reasoning_model,
                    "writer": writer_model,
                },
            }

    except httpx.HTTPError as exc:
        logger.warning("Błąd komunikacji z Ollamą: %r", exc)
        return {
            "reply": _describe_ollama_error(exc, [router_model, reasoning_model, writer_model]),
            "intent": "ERROR",
            "proposed_action": None,
            "models_used": {
                "router": router_model,
                "reasoning": reasoning_model,
                "writer": writer_model,
            },
        }