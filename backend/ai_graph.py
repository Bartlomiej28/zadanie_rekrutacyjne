import json
import re
import sqlite3
import time
from typing import Any, Literal, Optional, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from backend.config import settings
from backend.models import AIChatRequest, AIProposedAction
from backend.scheduler_engine import (
    build_schedule_context_json,
    find_replacement_candidates,
)

_SESSION_HISTORY: dict[str, list[dict[str, str]]] = {}


class AgentGraphState(TypedDict, total=False):
    # Wejście
    user_message: str
    miesiac: str
    session_id: str
    router_model: str
    reasoning_model: str
    writer_model: str
    history: list[dict[str, str]]
    schedule_context_json: str

    # Wynik routera
    intent: str
    target_date: Optional[str]
    target_shift: Optional[str]
    target_employee_name: Optional[str]
    needs_candidates: bool

    # Wynik narzędzia zastępstw
    replacements_json: Optional[str]

    # Wynik wnioskowania (qwen3:8b)
    reasoning_result: dict[str, Any]
    proposed_action: Optional[dict[str, Any]]

    # Wynik generatora odpowiedzi (gemma3:12b)
    reply: str
    error: Optional[str]
    graph_trace: list[str]


def _strip_thinking_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json_object(raw: str) -> dict[str, Any]:
    cleaned = _strip_thinking_tags(raw)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


async def _call_ollama(
    model: str,
    messages: list[dict[str, str]],
    json_mode: bool = False,
    temperature: float = 0.15,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")


def _build_workflow(conn: sqlite3.Connection):
    async def router_node(state: AgentGraphState) -> AgentGraphState:
        t0 = time.perf_counter()
        trace = list(state.get("graph_trace", []))
        miesiac = state["miesiac"]
        user_message = state["user_message"]

        system_prompt = (
            "Jesteś szybkim routerem intencji w systemie grafików laboratorium Kostecki Labs. "
            "Zwróć WYŁĄCZNIE poprawny obiekt JSON z kluczami:\n"
            '- "intent": jedna z wartości ["INFO", "FIND_REPLACEMENT", "ADD_ABSENCE", '
            '"SWAP_SHIFTS", "ASSIGN_SHIFT", "ADD_PREFERENCE", "REGENERATE_SCHEDULE"]\n'
            f'- "data": string YYYY-MM-DD lub null (domyślny miesiąc kontekstu: {miesiac})\n'
            '- "zmiana": "D" albo "N" albo null\n'
            '- "pracownik": imię i nazwisko pracownika z wiadomości lub null\n'
            '- "needs_candidates": boolean (true gdy pytanie dotyczy zastępstwa, nieobecności '
            "w konkretnym dniu lub obsady konkretnej zmiany)"
        )

        try:
            raw = await _call_ollama(
                model=state["router_model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                json_mode=True,
                temperature=0.0,
            )
            parsed = _extract_json_object(raw)
        except Exception:
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

            zmiana = (
                "N"
                if re.search(r"\b(noc|nocn|nock|zmian[aęy]\s*n)\b", low)
                else ("D" if re.search(r"\b(dzien|dzienn|zmian[aęy]\s*d)\b", low) else None)
            )
            parsed = {
                "intent": intent,
                "data": date_m.group(0) if date_m else None,
                "zmiana": zmiana,
                "pracownik": None,
                "needs_candidates": intent in ("FIND_REPLACEMENT", "ADD_ABSENCE", "SWAP_SHIFTS"),
            }

        target_date = parsed.get("data")
        target_shift = parsed.get("zmiana") if parsed.get("zmiana") in ("D", "N") else None
        needs_candidates = bool(target_date and target_shift)

        dt = round((time.perf_counter() - t0) * 1000)
        trace.append(f"router ({state['router_model']}, {dt}ms) -> {parsed.get('intent', 'INFO')}")

        return {
            "intent": parsed.get("intent", "INFO"),
            "target_date": target_date,
            "target_shift": target_shift,
            "target_employee_name": parsed.get("pracownik"),
            "needs_candidates": needs_candidates,
            "graph_trace": trace,
        }

    def route_after_classification(
        state: AgentGraphState,
    ) -> Literal["fetch_candidates", "reasoning"]:
        if state.get("needs_candidates"):
            return "fetch_candidates"
        return "reasoning"

    def fetch_candidates_node(state: AgentGraphState) -> AgentGraphState:
        t0 = time.perf_counter()
        trace = list(state.get("graph_trace", []))
        target_date = state["target_date"]
        target_shift = state["target_shift"]

        # Próba dopasowania ID pracownika, jeśli padło nazwisko (np. osoba idąca na urlop)
        replaced_id: Optional[int] = None
        emp_name = (state.get("target_employee_name") or "").strip().lower()
        if emp_name:
            ctx_obj = json.loads(state["schedule_context_json"])
            for p in ctx_obj.get("pracownicy", []):
                if emp_name in p["imie_nazwisko"].lower():
                    replaced_id = p["pracownik_id"]
                    break

        candidates = find_replacement_candidates(
            conn,
            data=target_date,
            zmiana=target_shift,
            zastepowany_pracownik_id=replaced_id,
        )
        serialized = json.dumps(
            [c.model_dump() for c in candidates[:8]],
            ensure_ascii=False,
            indent=2,
        )

        dt = round((time.perf_counter() - t0) * 1000)
        trace.append(f"fetch_candidates ({target_date} {target_shift}, {dt}ms)")
        return {
            "replacements_json": serialized,
            "graph_trace": trace,
        }

    async def reasoning_node(state: AgentGraphState) -> AgentGraphState:
        t0 = time.perf_counter()
        trace = list(state.get("graph_trace", []))

        system_prompt = """Jesteś głównym analitykiem grafików pracy laboratorium Kostecki Labs.
Twarde reguły laboratorium:
- Zmiany trwają 12h: D (dzienna) oraz N (nocna).
- Na każdej zmianie D i N musi być minimum 1 pracownik ze stanowiskiem 'starszy'.
- Wymagany odpoczynek dobowy: minimum 11h (zakaz zmiany D bezpośrednio po wczorajszej zmianie N).
- Pracownik nie może mieć zmiany w trakcie nieobecności (urlop, chorobowe, niedostępny).

Przeanalizuj pytanie użytkownika na podstawie aktualnego stanu grafiku (JSON).
Zwróć WYŁĄCZNIE poprawny obiekt JSON o strukturze:
{
  "wnioski_analityczne": "precyzyjne fakty liczbowe, wykryte kolizje, rekomendacje kandydatów",
  "proposed_action": null LUB obiekt:
  {
    "action_type": "ADD_ABSENCE" | "SWAP_SHIFTS" | "ASSIGN_SHIFT" | "ADD_PREFERENCE" | "REGENERATE_SCHEDULE",
    "title": "Krótki tytuł przycisku akcji",
    "description": "Opis zmiany, która zostanie zapisana w bazie SQLite",
    "payload": { ... }
  }
}

Wymagana struktura `payload` dla każdego `action_type`:
- ADD_ABSENCE: {"pracownik_id": int, "od": "YYYY-MM-DD", "do": "YYYY-MM-DD", "typ": "urlop"|"chorobowe"|"niedostępny", "notatka": str, "usun_kolidujace_zmiany": true}
- SWAP_SHIFTS: {"miesiac": "YYYY-MM", "data": "YYYY-MM-DD", "zmiana": "D"|"N", "z_pracownik_id": int, "na_pracownik_id": int, "zablokuj": true}
- ASSIGN_SHIFT: {"miesiac": "YYYY-MM", "data": "YYYY-MM-DD", "pracownik_id": int, "zmiana": "D"|"N"|null, "zablokowana": true}
- ADD_PREFERENCE: {"pracownik_id": int, "preferencja": str}
- REGENERATE_SCHEDULE: {"miesiac": "YYYY-MM", "zachowaj_zablokowane": true, "tryb_naprawy": bool}

Ustaw `proposed_action` TYLKO wtedy, gdy użytkownik wyraźnie prosi o wykonanie modyfikacji/zastępstwa/dodania urlopu i masz jednoznaczne ID pracowników oraz daty. W przeciwnym razie ustaw `null`."""

        context_parts = [f"AKTUALNY GRAFIK ({state['miesiac']}):\n{state['schedule_context_json']}"]
        if state.get("replacements_json"):
            context_parts.append(
                f"RANKING KANDYDATÓW NA ZASTĘPSTWO ({state.get('target_date')} zmiana {state.get('target_shift')}):\n"
                f"{state['replacements_json']}"
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(state.get("history", [])[-4:])
        messages.append(
            {
                "role": "user",
                "content": f"{chr(10).join(context_parts)}\n\nZAPYTANIE UŻYTKOWNIKA:\n{state['user_message']}",
            }
        )

        raw = await _call_ollama(
            model=state["reasoning_model"],
            messages=messages,
            json_mode=True,
            temperature=0.1,
        )
        parsed = _extract_json_object(raw)

        validated_action: Optional[dict[str, Any]] = None
        raw_action = parsed.get("proposed_action")
        if isinstance(raw_action, dict):
            try:
                validated_action = AIProposedAction(**raw_action).model_dump()
            except ValidationError:
                validated_action = None

        dt = round((time.perf_counter() - t0) * 1000)
        trace.append(f"reasoning ({state['reasoning_model']}, {dt}ms)")
        return {
            "reasoning_result": parsed,
            "proposed_action": validated_action,
            "graph_trace": trace,
        }

    async def writer_node(state: AgentGraphState) -> AgentGraphState:
        t0 = time.perf_counter()
        trace = list(state.get("graph_trace", []))

        system_prompt = (
            "Jesteś asystentem planowania grafików w Kostecki Labs. "
            "Napisz zwięzłą, konkretną i pomocną odpowiedź po polsku w formacie Markdown. "
            "Opieraj się wyłącznie na zweryfikowanych danych analitycznych z wejścia. "
            "Jeżeli w danych znajduje się `proposed_action` (różne od null), poinformuj użytkownika, "
            "że poniżej pojawił się przycisk pozwalający zatwierdzić tę zmianę jednym kliknięciem."
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(state.get("history", [])[-4:])
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Pytanie użytkownika: {state['user_message']}\n\n"
                    f"Zweryfikowane dane analityczne:\n"
                    f"{json.dumps(state.get('reasoning_result', {}), ensure_ascii=False, indent=2)}"
                ),
            }
        )

        raw = await _call_ollama(
            model=state["writer_model"],
            messages=messages,
            json_mode=False,
            temperature=0.35,
        )
        reply = _strip_thinking_tags(raw)

        dt = round((time.perf_counter() - t0) * 1000)
        trace.append(f"writer ({state['writer_model']}, {dt}ms)")
        return {
            "reply": reply,
            "graph_trace": trace,
        }

    graph = StateGraph(AgentGraphState)
    graph.add_node("router", router_node)
    graph.add_node("fetch_candidates", fetch_candidates_node)
    graph.add_node("reasoning", reasoning_node)
    graph.add_node("writer", writer_node)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        route_after_classification,
        {
            "fetch_candidates": "fetch_candidates",
            "reasoning": "reasoning",
        },
    )
    graph.add_edge("fetch_candidates", "reasoning")
    graph.add_edge("reasoning", "writer")
    graph.add_edge("writer", END)

    return graph.compile()


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
        workflow = _build_workflow(conn)
        final_state = await workflow.ainvoke(
            {
                "user_message": request.message,
                "miesiac": request.miesiac,
                "session_id": request.session_id,
                "router_model": router_model,
                "reasoning_model": reasoning_model,
                "writer_model": writer_model,
                "history": list(history),
                "schedule_context_json": schedule_context_json,
                "graph_trace": [],
            }
        )

        reply_text = final_state.get("reply", "")
        history.append({"role": "user", "content": request.message})
        history.append({"role": "assistant", "content": reply_text})
        if len(history) > 12:
            del history[:-12]

        return {
            "reply": reply_text,
            "intent": final_state.get("intent", "INFO"),
            "proposed_action": final_state.get("proposed_action"),
            "graph_trace": final_state.get("graph_trace", []),
            "models_used": {
                "router": router_model,
                "reasoning": reasoning_model,
                "writer": writer_model,
            },
        }
    except httpx.HTTPError as exc:
        return {
            "reply": (
                f"Brak połączenia z lokalnym serwerem **Ollama** (`{settings.ollama_base_url}`). "
                f"Upewnij się, że Ollama jest uruchomiona i masz pobrane modele: "
                f"`{router_model}`, `{reasoning_model}`, `{writer_model}`.\n\nSzczegóły: `{exc}`"
            ),
            "intent": "ERROR",
            "proposed_action": None,
            "graph_trace": ["error: ollama_connection_failed"],
            "models_used": {
                "router": router_model,
                "reasoning": reasoning_model,
                "writer": writer_model,
            },
        }