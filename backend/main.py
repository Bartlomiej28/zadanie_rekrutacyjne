import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.ai_agent import process_ai_chat
from backend.config import settings
from backend.database import db_session, init_db, save_snapshot
from backend.models import (
    AbsenceCreate,
    AbsenceOut,
    AIChatRequest,
    AIProposedAction,
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    GenerateScheduleRequest,
    PreferenceCreate,
    PreferenceOut,
    ReplacementCandidate,
    ShiftAssignmentUpdate,
    ShiftSwapRequest,
    ZmianaTyp,
)
from backend.scheduler_engine import (
    find_replacement_candidates,
    generate_schedule,
    get_schedule_overview,
    parse_preference_rule,
    validate_schedule,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Kostecki Labs — System Zarządzania Grafikiem AI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.get("/api/health")
async def health_check():
    ollama_ok = False
    available_models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            if resp.status_code == 200:
                ollama_ok = True
                available_models = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        pass

    return {
        "status": "ok",
        "ollama_connected": ollama_ok,
        "ollama_url": settings.ollama_base_url,
        "configured_models": {
            "router": settings.ollama_router_model,
            "reasoning": settings.ollama_reasoning_model,
            "writer": settings.ollama_writer_model,
        },
        "available_models": available_models,
    }



@app.get("/api/employees", response_model=list[EmployeeOut])
def list_employees(include_inactive: bool = False):
    with db_session() as conn:
        sql = "SELECT id, imie_nazwisko, stanowisko, etat, aktywny FROM pracownicy"
        if not include_inactive:
            sql += " WHERE aktywny = 1"
        sql += " ORDER BY CASE stanowisko WHEN 'starszy' THEN 1 WHEN 'pracownik' THEN 2 ELSE 3 END, id"
        rows = conn.execute(sql).fetchall()
        return [
            EmployeeOut(
                id=r["id"],
                imie_nazwisko=r["imie_nazwisko"],
                stanowisko=r["stanowisko"],
                etat=r["etat"],
                aktywny=bool(r["aktywny"]),
            )
            for r in rows
        ]


@app.post("/api/employees", response_model=EmployeeOut)
def create_employee(body: EmployeeCreate):
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO pracownicy (imie_nazwisko, stanowisko, etat, aktywny) VALUES (?, ?, ?, 1)",
            (body.imie_nazwisko.strip(), body.stanowisko, body.etat),
        )
        emp_id = int(cur.lastrowid)
        return EmployeeOut(
            id=emp_id,
            imie_nazwisko=body.imie_nazwisko.strip(),
            stanowisko=body.stanowisko,
            etat=body.etat,
            aktywny=True,
        )


@app.patch("/api/employees/{employee_id}", response_model=EmployeeOut)
def update_employee(employee_id: int, body: EmployeeUpdate):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM pracownicy WHERE id = ?", (employee_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Nie znaleziono pracownika.")

        imie = body.imie_nazwisko.strip() if body.imie_nazwisko is not None else row["imie_nazwisko"]
        stanowisko = body.stanowisko if body.stanowisko is not None else row["stanowisko"]
        etat = body.etat if body.etat is not None else row["etat"]
        aktywny = int(body.aktywny) if body.aktywny is not None else row["aktywny"]

        conn.execute(
            "UPDATE pracownicy SET imie_nazwisko = ?, stanowisko = ?, etat = ?, aktywny = ? WHERE id = ?",
            (imie, stanowisko, etat, aktywny, employee_id),
        )
        return EmployeeOut(
            id=employee_id,
            imie_nazwisko=imie,
            stanowisko=stanowisko,
            etat=etat,
            aktywny=bool(aktywny),
        )


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int):
    with db_session() as conn:
        cur = conn.execute("DELETE FROM pracownicy WHERE id = ?", (employee_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono pracownika.")
        return {"deleted": True, "id": employee_id}


# ------------ NIEOBECNOSCI ---


@app.get("/api/absences", response_model=list[AbsenceOut])
def list_absences(miesiac: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$")):
    with db_session() as conn:
        if miesiac:
            rows = conn.execute(
                """
                SELECT n.id, n.pracownik_id, p.imie_nazwisko, n.od, n.do, n.typ, n.notatka
                FROM nieobecnosci n
                JOIN pracownicy p ON p.id = n.pracownik_id
                WHERE substr(n.do, 1, 7) >= ? AND substr(n.od, 1, 7) <= ?
                ORDER BY n.od
                """,
                (miesiac, miesiac),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT n.id, n.pracownik_id, p.imie_nazwisko, n.od, n.do, n.typ, n.notatka
                FROM nieobecnosci n
                JOIN pracownicy p ON p.id = n.pracownik_id
                ORDER BY n.od DESC
                """
            ).fetchall()
        return [AbsenceOut(**dict(r)) for r in rows]


@app.post("/api/absences", response_model=AbsenceOut)
def create_absence(body: AbsenceCreate):
    if body.od > body.do:
        raise HTTPException(status_code=400, detail="Data 'od' nie może być późniejsza niż 'do'.")

    with db_session() as conn:
        emp = conn.execute("SELECT id, imie_nazwisko FROM pracownicy WHERE id = ?", (body.pracownik_id,)).fetchone()
        if not emp:
            raise HTTPException(status_code=404, detail="Nie znaleziono pracownika.")

        cur = conn.execute(
            "INSERT INTO nieobecnosci (pracownik_id, od, do, typ, notatka) VALUES (?, ?, ?, ?, ?)",
            (body.pracownik_id, body.od, body.do, body.typ, body.notatka),
        )
        absence_id = int(cur.lastrowid)

        if body.usun_kolidujace_zmiany:
            conn.execute(
                "DELETE FROM grafik WHERE pracownik_id = ? AND data >= ? AND data <= ?",
                (body.pracownik_id, body.od, body.do),
            )

        return AbsenceOut(
            id=absence_id,
            pracownik_id=body.pracownik_id,
            imie_nazwisko=emp["imie_nazwisko"],
            od=body.od,
            do=body.do,
            typ=body.typ,
            notatka=body.notatka,
        )


@app.delete("/api/absences/{absence_id}")
def delete_absence(absence_id: int):
    with db_session() as conn:
        cur = conn.execute("DELETE FROM nieobecnosci WHERE id = ?", (absence_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono nieobecności.")
        return {"deleted": True, "id": absence_id}


# -------- PREFERENCJE ---------


@app.get("/api/preferences", response_model=list[PreferenceOut])
def list_preferences():
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT pr.id, pr.pracownik_id, p.imie_nazwisko, pr.preferencja, pr.regula_kod
            FROM preferencje pr
            JOIN pracownicy p ON p.id = pr.pracownik_id
            ORDER BY pr.id
            """
        ).fetchall()
        return [PreferenceOut(**dict(r)) for r in rows]


@app.post("/api/preferences", response_model=PreferenceOut)
def create_preference(body: PreferenceCreate):
    with db_session() as conn:
        emp = conn.execute("SELECT id, imie_nazwisko FROM pracownicy WHERE id = ?", (body.pracownik_id,)).fetchone()
        if not emp:
            raise HTTPException(status_code=404, detail="Nie znaleziono pracownika.")

        regula = body.regula_kod or parse_preference_rule(body.preferencja)
        cur = conn.execute(
            "INSERT INTO preferencje (pracownik_id, preferencja, regula_kod) VALUES (?, ?, ?)",
            (body.pracownik_id, body.preferencja.strip(), regula),
        )
        return PreferenceOut(
            id=int(cur.lastrowid),
            pracownik_id=body.pracownik_id,
            imie_nazwisko=emp["imie_nazwisko"],
            preferencja=body.preferencja.strip(),
            regula_kod=regula,
        )


@app.delete("/api/preferences/{preference_id}")
def delete_preference(preference_id: int):
    with db_session() as conn:
        cur = conn.execute("DELETE FROM preferencje WHERE id = ?", (preference_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Nie znaleziono preferencji.")
        return {"deleted": True, "id": preference_id}


# ---- GRAFIK WALIDACJA I ZASTEPSTWWA -----


@app.get("/api/schedule/{miesiac}")
def get_schedule(
    miesiac: str,
    obsada_dzien: int = settings.day_shift_required,
    obsada_noc: int = settings.night_shift_required,
    norma_godzin_etat: int = settings.base_monthly_hours,
):
    with db_session() as conn:
        return get_schedule_overview(
            conn,
            miesiac,
            day_required=obsada_dzien,
            night_required=obsada_noc,
            base_hours=norma_godzin_etat,
        )


@app.post("/api/schedule/generate")
def api_generate_schedule(body: GenerateScheduleRequest):
    with db_session() as conn:
        existing = [
            dict(r)
            for r in conn.execute(
                "SELECT data, pracownik_id, zmiana, zablokowana FROM grafik WHERE miesiac = ?",
                (body.miesiac,),
            ).fetchall()
        ]
        if existing:
            save_snapshot(
                conn,
                body.miesiac,
                "Auto-kopia przed generowaniem",
                json.dumps(existing, ensure_ascii=False),
            )

        return generate_schedule(
            conn,
            miesiac=body.miesiac,
            zachowaj_zablokowane=body.zachowaj_zablokowane,
            tryb_naprawy=body.tryb_naprawy,
            obsada_dzien=body.obsada_dzien,
            obsada_noc=body.obsada_noc,
            norma_godzin_etat=body.norma_godzin_etat,
        )


@app.put("/api/schedule/shift")
def update_single_shift(body: ShiftAssignmentUpdate):
    with db_session() as conn:
        if body.zmiana is None:
            conn.execute(
                "DELETE FROM grafik WHERE data = ? AND pracownik_id = ?",
                (body.data, body.pracownik_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO grafik (miesiac, data, pracownik_id, zmiana, zablokowana)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(data, pracownik_id) DO UPDATE SET
                    miesiac = excluded.miesiac,
                    zmiana = excluded.zmiana,
                    zablokowana = excluded.zablokowana
                """,
                (body.miesiac, body.data, body.pracownik_id, body.zmiana, int(body.zablokowana)),
            )

        issues = validate_schedule(conn, body.miesiac)
        return {"updated": True, "walidacja": [i.model_dump() for i in issues]}


@app.post("/api/schedule/swap")
def swap_shifts(body: ShiftSwapRequest):
    with db_session() as conn:
        conn.execute(
            "DELETE FROM grafik WHERE data = ? AND pracownik_id = ? AND zmiana = ?",
            (body.data, body.z_pracownik_id, body.zmiana),
        )
        conn.execute(
            """
            INSERT INTO grafik (miesiac, data, pracownik_id, zmiana, zablokowana)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(data, pracownik_id) DO UPDATE SET
                zmiana = excluded.zmiana,
                zablokowana = excluded.zablokowana
            """,
            (body.miesiac, body.data, body.na_pracownik_id, body.zmiana, int(body.zablokuj)),
        )
        issues = validate_schedule(conn, body.miesiac)
        return {"swapped": True, "walidacja": [i.model_dump() for i in issues]}


@app.get("/api/schedule/replacements", response_model=list[ReplacementCandidate])
def get_replacements(
    data: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    zmiana: ZmianaTyp = Query(...),
    zastepowany_pracownik_id: Optional[int] = Query(default=None),
):
    with db_session() as conn:
        return find_replacement_candidates(
            conn,
            data=data,
            zmiana=zmiana,
            zastepowany_pracownik_id=zastepowany_pracownik_id,
        )


# --- MIGAWKI GRAFIKU ---


@app.get("/api/snapshots/{miesiac}")
def list_snapshots(miesiac: str):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, miesiac, nazwa, utworzono FROM grafik_migawki WHERE miesiac = ? ORDER BY id DESC",
            (miesiac,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/snapshots/{miesiac}")
def create_manual_snapshot(miesiac: str, nazwa: str = "Wersja zapisana ręcznie"):
    with db_session() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT data, pracownik_id, zmiana, zablokowana FROM grafik WHERE miesiac = ?",
                (miesiac,),
            ).fetchall()
        ]
        snap_id = save_snapshot(conn, miesiac, nazwa, json.dumps(rows, ensure_ascii=False))
        return {"id": snap_id, "miesiac": miesiac, "nazwa": nazwa}


@app.post("/api/snapshots/restore/{snapshot_id}")
def restore_snapshot(snapshot_id: int):
    with db_session() as conn:
        snap = conn.execute(
            "SELECT id, miesiac, nazwa, dane_json FROM grafik_migawki WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if not snap:
            raise HTTPException(status_code=404, detail="Nie znaleziono migawki.")

        miesiac = snap["miesiac"]
        entries = json.loads(snap["dane_json"])
        conn.execute("DELETE FROM grafik WHERE miesiac = ?", (miesiac,))
        conn.executemany(
            """
            INSERT INTO grafik (miesiac, data, pracownik_id, zmiana, zablokowana)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (miesiac, e["data"], e["pracownik_id"], e["zmiana"], e.get("zablokowana", 0))
                for e in entries
            ],
        )
        return {"restored": True, "miesiac": miesiac, "nazwa": snap["nazwa"]}


# ----- ASYSTENT (OLLAMA) ----------


@app.post("/api/ai/chat")
async def ai_chat(body: AIChatRequest):
    with db_session() as conn:
        return await process_ai_chat(conn, body)


@app.post("/api/ai/execute-action")
def execute_ai_action(action: AIProposedAction):
    p = action.payload
    if action.action_type == "ADD_ABSENCE":
        return create_absence(AbsenceCreate(**p))
    if action.action_type == "SWAP_SHIFTS":
        return swap_shifts(ShiftSwapRequest(**p))
    if action.action_type == "ASSIGN_SHIFT":
        return update_single_shift(ShiftAssignmentUpdate(**p))
    if action.action_type == "ADD_PREFERENCE":
        return create_preference(PreferenceCreate(**p))
    if action.action_type == "REGENERATE_SCHEDULE":
        return api_generate_schedule(GenerateScheduleRequest(**p))
    raise HTTPException(status_code=400, detail=f"Nieobsługiwany typ akcji: {action.action_type}")


# -------- SERWOWANIE FRONTENDU --------

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_index():
        index_file = FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return {"message": "API Kostecki Labs działa. Dokumentacja: /docs"}