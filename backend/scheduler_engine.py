import calendar
import json
import random
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from backend.config import settings
from backend.models import ReplacementCandidate, ValidationIssue


FIXED_HOLIDAYS_MD = {
    (1, 1): "Nowy Rok",
    (1, 6): "Święto Trzech Króli",
    (5, 1): "Święto Pracy",
    (5, 3): "Święto Konstytucji 3 Maja",
    (8, 15): "Wniebowzięcie NMP",
    (11, 1): "Wszystkich Świętych",
    (11, 11): "Narodowe Święto Niepodległości",
    (12, 24): "Wigilia Bożego Narodzenia",
    (12, 25): "Boże Narodzenie (1. dzień)",
    (12, 26): "Boże Narodzenie (2. dzień)",
}

MAX_ISSUES_IN_AI_CONTEXT = 25

WEEKDAY_NAMES_PL = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]


def get_polish_holidays(year: int) -> dict[str, str]:
    holidays: dict[str, str] = {}
    for (m, d), name in FIXED_HOLIDAYS_MD.items():
        holidays[f"{year:04d}-{m:02d}-{d:02d}"] = name

    # Algorytm Meeusa/Jonesa/Butchera dla Wielkanocy
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    holidays[easter.isoformat()] = "Wielkanoc"
    holidays[(easter + timedelta(days=1)).isoformat()] = "Poniedziałek Wielkanocny"
    holidays[(easter + timedelta(days=49)).isoformat()] = "Zesłanie Ducha Świętego"
    holidays[(easter + timedelta(days=60)).isoformat()] = "Boże Ciało"
    return holidays


def parse_preference_rule(text: str) -> str:
    """Konwertuje opis preferencji w języku polskim na kod reguły używany przez silnik grafiku."""
    low = text.lower().strip()

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", low)
    if date_match and any(w in low for w in ["wolne", "nie prac", "urlop", "nie mogę", "odpada"]):
        return f"DAY_OFF:{date_match.group(1)}"

    if any(w in low for w in ["nie w weekend", "bez weekend", "wolne weekend"]):
        return "NO_WEEKENDS"

    if any(w in low for w in ["tylko dzienn", "bez noc", "nie chcę noc", "wolę dzienn"]):
        return "ONLY_DAY"

    if any(w in low for w in ["lubię nock", "lubię nocn", "tylko noc", "preferuję noc", "chcę nock"]):
        return "PREFER_NIGHT"

    day_map = {
        "poniedział": 0,
        "wtork": 1,
        "wtorek": 1,
        "środ": 2,
        "czwart": 3,
        "piąt": 4,
        "sobot": 5,
        "niedziel": 6,
    }
    matched_days = sorted({idx for stem, idx in day_map.items() if stem in low})
    if matched_days and any(w in low for w in ["najlepiej", "prefer", "chętnie", "tylko"]):
        return "PREFERRED_WEEKDAYS:" + ",".join(str(d) for d in matched_days)

    return "CUSTOM"


def get_month_days(miesiac: str) -> list[dict[str, Any]]:
    year, month = map(int, miesiac.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    holidays = get_polish_holidays(year)
    days = []
    for d in range(1, num_days + 1):
        dt = date(year, month, d)
        iso = dt.isoformat()
        wd = dt.weekday()
        days.append(
            {
                "data": iso,
                "dzien": d,
                "dzien_tyg_idx": wd,
                "dzien_tyg": WEEKDAY_NAMES_PL[wd],
                "czy_weekend": wd >= 5,
                "swieto": holidays.get(iso),
            }
        )
    return days


def _load_month_context(conn: sqlite3.Connection, miesiac: str) -> dict[str, Any]:
    days = get_month_days(miesiac)
    first_day = days[0]["data"]
    last_day = days[-1]["data"]
    prev_day = (date.fromisoformat(first_day) - timedelta(days=1)).isoformat()

    employees = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, imie_nazwisko, stanowisko, etat, aktywny
            FROM pracownicy
            WHERE aktywny = 1
            ORDER BY CASE stanowisko WHEN 'starszy' THEN 1 WHEN 'pracownik' THEN 2 ELSE 3 END, id
            """
        ).fetchall()
    ]

    absences = [
        dict(r)
        for r in conn.execute(
            """
            SELECT n.id, n.pracownik_id, p.imie_nazwisko, n.od, n.do, n.typ, n.notatka
            FROM nieobecnosci n
            JOIN pracownicy p ON p.id = n.pracownik_id
            WHERE n.do >= ? AND n.od <= ?
            ORDER BY n.od, n.pracownik_id
            """,
            (first_day, last_day),
        ).fetchall()
    ]

    preferences = [
        dict(r)
        for r in conn.execute(
            """
            SELECT pr.id, pr.pracownik_id, p.imie_nazwisko, pr.preferencja, pr.regula_kod
            FROM preferencje pr
            JOIN pracownicy p ON p.id = pr.pracownik_id
            ORDER BY pr.id
            """
        ).fetchall()
    ]

    shifts = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, miesiac, data, pracownik_id, zmiana, zablokowana
            FROM grafik
            WHERE miesiac = ?
            """,
            (miesiac,),
        ).fetchall()
    ]

    prev_day_shifts = {
        r["pracownik_id"]: r["zmiana"]
        for r in conn.execute(
            "SELECT pracownik_id, zmiana FROM grafik WHERE data = ?",
            (prev_day,),
        ).fetchall()
    }

    absence_map: dict[tuple[int, str], dict[str, Any]] = {}
    for ab in absences:
        cur = max(date.fromisoformat(ab["od"]), date.fromisoformat(first_day))
        end = min(date.fromisoformat(ab["do"]), date.fromisoformat(last_day))
        while cur <= end:
            absence_map[(ab["pracownik_id"], cur.isoformat())] = ab
            cur += timedelta(days=1)

    pref_by_emp: dict[int, list[dict[str, Any]]] = {}
    for pr in preferences:
        pref_by_emp.setdefault(pr["pracownik_id"], []).append(pr)

    return {
        "days": days,
        "employees": employees,
        "absences": absences,
        "preferences": preferences,
        "shifts": shifts,
        "prev_day_shifts": prev_day_shifts,
        "absence_map": absence_map,
        "pref_by_emp": pref_by_emp,
    }


def _evaluate_preference_delta(
    emp_prefs: list[dict[str, Any]],
    day_info: dict[str, Any],
    zmiana: str,
) -> tuple[float, list[str]]:
    """Zwraca (delta_punktacji, lista_naruszen_preferencji)."""
    score = 0.0
    violations: list[str] = []
    iso = day_info["data"]
    wd = day_info["dzien_tyg_idx"]
    is_weekend = day_info["czy_weekend"]

    for pr in emp_prefs:
        code = pr.get("regula_kod") or parse_preference_rule(pr["preferencja"])
        if code == "NO_WEEKENDS":
            if is_weekend:
                score -= 22.0
                violations.append(f"Preferencja: {pr['preferencja']}")
            else:
                score += 2.0
        elif code == "ONLY_DAY":
            if zmiana == "N":
                score -= 35.0
                violations.append(f"Preferencja: {pr['preferencja']}")
            else:
                score += 6.0
        elif code == "PREFER_NIGHT":
            if zmiana == "N":
                score += 12.0
            else:
                score -= 3.0
        elif code.startswith("DAY_OFF:"):
            target_date = code.split(":", 1)[1]
            if iso == target_date:
                score -= 60.0
                violations.append(f"Prośba o wolne ({target_date})")
        elif code.startswith("PREFERRED_WEEKDAYS:"):
            allowed = {int(x) for x in code.split(":", 1)[1].split(",") if x.isdigit()}
            if wd in allowed:
                score += 14.0
            else:
                score -= 8.0
                violations.append(f"Preferencja dni: {pr['preferencja']}")

    return score, violations


def validate_schedule(
    conn: sqlite3.Connection,
    miesiac: str,
    day_required: int = settings.day_shift_required,
    night_required: int = settings.night_shift_required,
    base_hours: int = settings.base_monthly_hours,
) -> list[ValidationIssue]:
    ctx = _load_month_context(conn, miesiac)
    days = ctx["days"]
    employees = {e["id"]: e for e in ctx["employees"]}
    absence_map = ctx["absence_map"]
    pref_by_emp = ctx["pref_by_emp"]
    prev_day_shifts = ctx["prev_day_shifts"]

    shift_by_emp_date: dict[tuple[int, str], str] = {}
    shifts_by_date: dict[str, dict[str, list[int]]] = {
        d["data"]: {"D": [], "N": []} for d in days
    }
    emp_hours: dict[int, int] = {eid: 0 for eid in employees}

    for s in ctx["shifts"]:
        eid = s["pracownik_id"]
        dt = s["data"]
        zm = s["zmiana"]
        if eid not in employees:
            continue
        shift_by_emp_date[(eid, dt)] = zm
        if dt in shifts_by_date:
            shifts_by_date[dt][zm].append(eid)
        emp_hours[eid] += settings.shift_hours

    issues: list[ValidationIssue] = []

    # 1. Walidacja dzienna obsady i ról
    for d in days:
        iso = d["data"]
        day_emps = shifts_by_date[iso]["D"]
        night_emps = shifts_by_date[iso]["N"]

        day_seniors = [eid for eid in day_emps if employees[eid]["stanowisko"] == "starszy"]
        night_seniors = [eid for eid in night_emps if employees[eid]["stanowisko"] == "starszy"]

        if len(day_seniors) == 0:
            issues.append(
                ValidationIssue(
                    severity="error",
                    kod="MISSING_SENIOR_DAY",
                    data=iso,
                    opis=f"{iso}: Brak starszego specjalisty na zmianie dziennej (D).",
                )
            )
        if len(night_seniors) == 0:
            issues.append(
                ValidationIssue(
                    severity="error",
                    kod="MISSING_SENIOR_NIGHT",
                    data=iso,
                    opis=f"{iso}: Brak starszego specjalisty na zmianie nocnej (N).",
                )
            )
        if len(day_emps) < day_required:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    kod="UNDERSTAFFED_DAY",
                    data=iso,
                    opis=f"{iso}: Niepełna obsada dzienna ({len(day_emps)}/{day_required} osób).",
                )
            )
        if len(night_emps) < night_required:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    kod="UNDERSTAFFED_NIGHT",
                    data=iso,
                    opis=f"{iso}: Niepełna obsada nocna ({len(night_emps)}/{night_required} osób).",
                )
            )

    # 2. Walidacja indywidualna (urlopy, 11h odpoczynku, ciągi zmian, preferencje)
    for eid, emp in employees.items():
        consecutive = 0
        for idx, d in enumerate(days):
            iso = d["data"]
            zm = shift_by_emp_date.get((eid, iso))
            if not zm:
                consecutive = 0
                continue

            consecutive += 1
            if consecutive > 5:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        kod="TOO_MANY_CONSECUTIVE_DAYS",
                        data=iso,
                        pracownik_id=eid,
                        imie_nazwisko=emp["imie_nazwisko"],
                        opis=f"{emp['imie_nazwisko']} ({iso}): {consecutive}. dzień pracy z rzędu (brak odpoczynku tygodniowego).",
                    )
                )

            if (eid, iso) in absence_map:
                ab = absence_map[(eid, iso)]
                issues.append(
                    ValidationIssue(
                        severity="error",
                        kod="ABSENCE_CONFLICT",
                        data=iso,
                        pracownik_id=eid,
                        imie_nazwisko=emp["imie_nazwisko"],
                        opis=f"{emp['imie_nazwisko']} ({iso}): Przypisana zmiana {zm} podczas nieobecności ({ab['typ']}).",
                    )
                )

            prev_zm = prev_day_shifts.get(eid) if idx == 0 else shift_by_emp_date.get((eid, days[idx - 1]["data"]))
            if prev_zm == "N" and zm == "D":
                issues.append(
                    ValidationIssue(
                        severity="error",
                        kod="REST_11H_VIOLATION",
                        data=iso,
                        pracownik_id=eid,
                        imie_nazwisko=emp["imie_nazwisko"],
                        opis=f"{emp['imie_nazwisko']} ({iso}): Zmiana dzienna (D) bezpośrednio po nocnej (N) — naruszenie 11h odpoczynku!",
                    )
                )

            _, pref_violations = _evaluate_preference_delta(pref_by_emp.get(eid, []), d, zm)
            for pv in pref_violations:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        kod="PREFERENCE_VIOLATION",
                        data=iso,
                        pracownik_id=eid,
                        imie_nazwisko=emp["imie_nazwisko"],
                        opis=f"{emp['imie_nazwisko']} ({iso}): {pv}.",
                    )
                )

        target_h = round(emp["etat"] * base_hours)
        if emp_hours[eid] > target_h + settings.shift_hours:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    kod="OVERTIME_WARNING",
                    pracownik_id=eid,
                    imie_nazwisko=emp["imie_nazwisko"],
                    opis=f"{emp['imie_nazwisko']}: Przekroczona norma etatu ({emp_hours[eid]}h / {target_h}h).",
                )
            )

    return issues


def generate_schedule(
    conn: sqlite3.Connection,
    miesiac: str,
    zachowaj_zablokowane: bool = True,
    tryb_naprawy: bool = False,
    obsada_dzien: int = settings.day_shift_required,
    obsada_noc: int = settings.night_shift_required,
    norma_godzin_etat: int = settings.base_monthly_hours,
) -> dict[str, Any]:
    ctx = _load_month_context(conn, miesiac)
    days = ctx["days"]
    employees = ctx["employees"]
    emp_by_id = {e["id"]: e for e in employees}
    absence_map = ctx["absence_map"]
    pref_by_emp = ctx["pref_by_emp"]
    prev_day_shifts = ctx["prev_day_shifts"]

    # Zachowujemy zablokowane komórki (lub wszystkie bezkonfliktowe w trybie naprawy)
    assigned: dict[tuple[int, str], str] = {}
    locked_cells: set[tuple[int, str]] = set()

    for s in ctx["shifts"]:
        eid = s["pracownik_id"]
        dt = s["data"]
        zm = s["zmiana"]
        if eid not in emp_by_id:
            continue
        if (eid, dt) in absence_map:
            continue
        if s["zablokowana"] and zachowaj_zablokowane:
            assigned[(eid, dt)] = zm
            locked_cells.add((eid, dt))
        elif tryb_naprawy:
            assigned[(eid, dt)] = zm

    target_hours = {e["id"]: max(settings.shift_hours, round(e["etat"] * norma_godzin_etat)) for e in employees}

    def emp_current_hours(eid: int) -> int:
        return sum(settings.shift_hours for (e, _), _ in assigned.items() if e == eid)

    def emp_night_count(eid: int) -> int:
        return sum(1 for (e, _), z in assigned.items() if e == eid and z == "N")

    def emp_weekend_count(eid: int) -> int:
        weekend_dates = {d["data"] for d in days if d["czy_weekend"]}
        return sum(1 for (e, dt), _ in assigned.items() if e == eid and dt in weekend_dates)

    def can_assign(eid: int, day_idx: int, zmiana: str, strict_hours: bool = True) -> bool:
        iso = days[day_idx]["data"]
        if (eid, iso) in assigned:
            return False
        if (eid, iso) in absence_map:
            return False

        # 11h odpoczynku: brak D po wczorajszej N, oraz brak N przed jutrzejszą D
        prev_zm = prev_day_shifts.get(eid) if day_idx == 0 else assigned.get((eid, days[day_idx - 1]["data"]))
        if prev_zm == "N" and zmiana == "D":
            return False

        if day_idx + 1 < len(days):
            next_zm = assigned.get((eid, days[day_idx + 1]["data"]))
            if zmiana == "N" and next_zm == "D":
                return False

        # Maksymalnie 2 noce z rzędu
        if zmiana == "N":
            n_before = 0
            for back in (1, 2):
                if day_idx - back >= 0 and assigned.get((eid, days[day_idx - back]["data"])) == "N":
                    n_before += 1
                else:
                    break
            if n_before >= 2:
                return False

        # Maksymalnie 4 dni pracy z rzędu (komfort + odpoczynek tygodniowy)
        streak = 1
        back = day_idx - 1
        while back >= 0 and (eid, days[back]["data"]) in assigned:
            streak += 1
            back -= 1
        fwd = day_idx + 1
        while fwd < len(days) and (eid, days[fwd]["data"]) in assigned:
            streak += 1
            fwd += 1
        if streak > 4:
            return False

        if strict_hours and emp_current_hours(eid) + settings.shift_hours > target_hours[eid] + 6:
            return False

        return True

    def candidate_score(eid: int, day_idx: int, zmiana: str) -> float:
        day_info = days[day_idx]
        cur_h = emp_current_hours(eid)
        tgt_h = target_hours[eid]
        deficit_ratio = (tgt_h - cur_h) / max(1, tgt_h)

        score = deficit_ratio * 100.0
        pref_delta, _ = _evaluate_preference_delta(pref_by_emp.get(eid, []), day_info, zmiana)
        score += pref_delta

        # Sprawiedliwy podział nocek i weekendów
        if zmiana == "N":
            score -= emp_night_count(eid) * 3.5
        if day_info["czy_weekend"]:
            score -= emp_weekend_count(eid) * 4.0

        # Lekkie zróżnicowanie przy remisach
        rng = random.Random(f"{miesiac}-{day_idx}-{zmiana}-{eid}")
        score += rng.uniform(-0.8, 0.8)
        return score

    # Przebieg 1: Gwarancja obsady 'starszy' na każdej zmianie N i D (najpierw trudniejsze N)
    for zmiana in ("N", "D"):
        for day_idx, d in enumerate(days):
            iso = d["data"]
            already_has_senior = any(
                assigned.get((e["id"], iso)) == zmiana
                for e in employees
                if e["stanowisko"] == "starszy"
            )
            if already_has_senior:
                continue

            seniors = [e["id"] for e in employees if e["stanowisko"] == "starszy"]
            eligible = [eid for eid in seniors if can_assign(eid, day_idx, zmiana, strict_hours=True)]
            if not eligible:
                eligible = [eid for eid in seniors if can_assign(eid, day_idx, zmiana, strict_hours=False)]

            if eligible:
                best = max(eligible, key=lambda eid: candidate_score(eid, day_idx, zmiana))
                assigned[(best, iso)] = zmiana

    # Przebieg 2: Uzupełnienie wymaganej liczby osób na zmianach N oraz D
    for zmiana, req_count in (("N", obsada_noc), ("D", obsada_dzien)):
        for day_idx, d in enumerate(days):
            iso = d["data"]
            while True:
                current_assigned = [e["id"] for e in employees if assigned.get((e["id"], iso)) == zmiana]
                if len(current_assigned) >= req_count:
                    break

                all_ids = [e["id"] for e in employees]
                eligible = [eid for eid in all_ids if can_assign(eid, day_idx, zmiana, strict_hours=True)]
                if not eligible:
                    eligible = [eid for eid in all_ids if can_assign(eid, day_idx, zmiana, strict_hours=False)]
                if not eligible:
                    break

                best = max(eligible, key=lambda eid: candidate_score(eid, day_idx, zmiana))
                assigned[(best, iso)] = zmiana

    # Przebieg 3: Lokalna optymalizacja zamian (poprawa balansu godzinowego i preferencji)
    for _ in range(80):
        improved = False
        for day_idx, d in enumerate(days):
            iso = d["data"]
            for zmiana in ("D", "N"):
                working = [
                    e["id"]
                    for e in employees
                    if assigned.get((e["id"], iso)) == zmiana and (e["id"], iso) not in locked_cells
                ]
                free_emps = [e["id"] for e in employees if (e["id"], iso) not in assigned]
                for w_id in working:
                    # Jeśli to jedyny starszy na zmianie, zastępca też musi być starszym
                    seniors_on_shift = [
                        e["id"]
                        for e in employees
                        if assigned.get((e["id"], iso)) == zmiana and e["stanowisko"] == "starszy"
                    ]
                    must_be_senior = emp_by_id[w_id]["stanowisko"] == "starszy" and len(seniors_on_shift) <= 1

                    old_score = candidate_score(w_id, day_idx, zmiana)
                    del assigned[(w_id, iso)]

                    best_sub = None
                    best_gain = 4.0  # minimalny próg poprawy
                    for f_id in free_emps:
                        if must_be_senior and emp_by_id[f_id]["stanowisko"] != "starszy":
                            continue
                        if not can_assign(f_id, day_idx, zmiana, strict_hours=True):
                            continue
                        new_score = candidate_score(f_id, day_idx, zmiana)
                        if new_score - old_score > best_gain:
                            best_gain = new_score - old_score
                            best_sub = f_id

                    if best_sub is not None:
                        assigned[(best_sub, iso)] = zmiana
                        improved = True
                    else:
                        assigned[(w_id, iso)] = zmiana
        if not improved:
            break

    # Zapis do bazy SQLite
    # Zapis do bazy SQLite
    conn.execute("DELETE FROM grafik WHERE miesiac = ?", (miesiac,))
    rows_to_insert = [
        (miesiac, dt, eid, zm, 1 if (eid, dt) in locked_cells else 0)
        for (eid, dt), zm in sorted(assigned.items(), key=lambda x: (x[0][1], x[0][0]))
    ]
    conn.executemany(
        """
        INSERT INTO grafik (miesiac, data, pracownik_id, zmiana, zablokowana)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    conn.commit()

    issues = validate_schedule(
        conn,
        miesiac,
        day_required=obsada_dzien,
        night_required=obsada_noc,
        base_hours=norma_godzin_etat,
    )

    employee_stats = []
    for emp in employees:
        eid = emp["id"]
        worked_h = emp_current_hours(eid)
        tgt_h = target_hours[eid]
        day_cnt = sum(1 for (e, _), z in assigned.items() if e == eid and z == "D")
        night_cnt = emp_night_count(eid)
        weekend_cnt = emp_weekend_count(eid)
        employee_stats.append(
            {
                "pracownik_id": eid,
                "imie_nazwisko": emp["imie_nazwisko"],
                "stanowisko": emp["stanowisko"],
                "etat": emp["etat"],
                "godziny_wypracowane": worked_h,
                "norma_godzin": tgt_h,
                "roznica_godzin": worked_h - tgt_h,
                "zmiany_dzienne": day_cnt,
                "zmiany_nocne": night_cnt,
                "zmiany_weekendowe": weekend_cnt,
            }
        )

    return {
        "miesiac": miesiac,
        "wygenerowane_zmiany": len(rows_to_insert),
        "zachowane_zablokowane": len(locked_cells),
        "bledy_krytyczne": sum(1 for i in issues if i.severity == "error"),
        "ostrzezenia": sum(1 for i in issues if i.severity == "warning"),
        "issues": [i.model_dump() for i in issues],
        "statystyki_pracownikow": employee_stats,
    }


def find_replacement_candidates(
    conn: sqlite3.Connection,
    data: str,
    zmiana: str,
    zastepowany_pracownik_id: Optional[int] = None,
    base_hours: int = settings.base_monthly_hours,
) -> list[ReplacementCandidate]:
    """Ocenia pracowników pod kątem zastępstwa na wskazanej dacie i zmianie ('D' lub 'N')."""
    miesiac = data[:7]
    ctx = _load_month_context(conn, miesiac)
    days = ctx["days"]
    employees = ctx["employees"]
    emp_by_id = {e["id"]: e for e in employees}
    absence_map = ctx["absence_map"]
    pref_by_emp = ctx["pref_by_emp"]
    prev_day_shifts = ctx["prev_day_shifts"]

    day_idx = next((idx for idx, d in enumerate(days) if d["data"] == data), None)
    if day_idx is None:
        return []

    day_info = days[day_idx]
    next_day_iso = (date.fromisoformat(data) + timedelta(days=1)).isoformat()
    next_month_first_day_shifts = {
        r["pracownik_id"]: r["zmiana"]
        for r in conn.execute(
            "SELECT pracownik_id, zmiana FROM grafik WHERE data = ?",
            (next_day_iso,),
        ).fetchall()
    }

    shift_by_emp_date: dict[tuple[int, str], str] = {}
    emp_hours: dict[int, int] = {e["id"]: 0 for e in employees}
    emp_nights: dict[int, int] = {e["id"]: 0 for e in employees}
    emp_weekends: dict[int, int] = {e["id"]: 0 for e in employees}
    weekend_dates = {d["data"] for d in days if d["czy_weekend"]}

    for s in ctx["shifts"]:
        eid = s["pracownik_id"]
        dt = s["data"]
        zm = s["zmiana"]
        if eid not in emp_by_id:
            continue
        shift_by_emp_date[(eid, dt)] = zm
        emp_hours[eid] += settings.shift_hours
        if zm == "N":
            emp_nights[eid] += 1
        if dt in weekend_dates:
            emp_weekends[eid] += 1

    remaining_seniors = [
        e["id"]
        for e in employees
        if e["id"] != zastepowany_pracownik_id
        and e["stanowisko"] == "starszy"
        and shift_by_emp_date.get((e["id"], data)) == zmiana
    ]
    requires_senior = len(remaining_seniors) == 0

    candidates: list[ReplacementCandidate] = []
    for emp in employees:
        eid = emp["id"]
        if eid == zastepowany_pracownik_id:
            continue

        cur_h = emp_hours[eid]
        tgt_h = max(settings.shift_hours, round(emp["etat"] * base_hours))
        powody: list[str] = []
        dostepny = True
        score = 60.0

        # Twarde blokady dostępności
        if (eid, data) in shift_by_emp_date:
            dostepny = False
            powody.append(f"Ma już przypisaną zmianę ({shift_by_emp_date[(eid, data)]}) w tym dniu")

        if (eid, data) in absence_map:
            dostepny = False
            powody.append(f"Nieobecność: {absence_map[(eid, data)]['typ']}")

        prev_zm = (
            prev_day_shifts.get(eid)
            if day_idx == 0
            else shift_by_emp_date.get((eid, days[day_idx - 1]["data"]))
        )
        if prev_zm == "N" and zmiana == "D":
            dostepny = False
            powody.append("Naruszenie 11h odpoczynku (zmiana N dzień wcześniej)")

        next_zm = (
            shift_by_emp_date.get((eid, days[day_idx + 1]["data"]))
            if day_idx + 1 < len(days)
            else next_month_first_day_shifts.get(eid)
        )
        if zmiana == "N" and next_zm == "D":
            dostepny = False
            powody.append("Naruszenie 11h odpoczynku (zmiana D następnego dnia)")

        if not dostepny:
            candidates.append(
                ReplacementCandidate(
                    pracownik_id=eid,
                    imie_nazwisko=emp["imie_nazwisko"],
                    stanowisko=emp["stanowisko"],
                    etat=emp["etat"],
                    zaplanowane_godziny=cur_h,
                    norma_godzin=tgt_h,
                    ocena=0,
                    dostepny=False,
                    powody=powody,
                )
            )
            continue

        # Ocena merytoryczna dostępnego kandydata
        if requires_senior:
            if emp["stanowisko"] == "starszy":
                score += 25.0
                powody.append("Spełnia wymóg starszego specjalisty na zmianie")
            else:
                score -= 45.0
                powody.append("Brak uprawnień starszego specjalisty (na zmianie brakuje seniora)")

        new_h = cur_h + settings.shift_hours
        if new_h <= tgt_h:
            deficit = tgt_h - cur_h
            score += min(25.0, (deficit / max(1, tgt_h)) * 60.0)
            powody.append(f"Niedobór godzin do etatu ({cur_h}h / {tgt_h}h)")
        elif new_h <= tgt_h + 6:
            score += 5.0
            powody.append(f"Zgodne z normą etatu ({new_h}h / {tgt_h}h)")
        else:
            overtime = new_h - tgt_h
            score -= overtime * 1.5
            powody.append(f"Nadgodziny: +{overtime}h ponad normę ({new_h}h / {tgt_h}h)")

        streak = 1
        back = day_idx - 1
        while back >= 0 and (eid, days[back]["data"]) in shift_by_emp_date:
            streak += 1
            back -= 1
        fwd = day_idx + 1
        while fwd < len(days) and (eid, days[fwd]["data"]) in shift_by_emp_date:
            streak += 1
            fwd += 1

        if streak > 5:
            score -= 25.0
            powody.append(f"Uwaga: {streak}. dzień pracy z rzędu")
        elif streak == 5:
            score -= 8.0
            powody.append("5. dzień pracy z rzędu")

        if zmiana == "N":
            night_streak = 1
            for step in (1, 2):
                if day_idx - step >= 0 and shift_by_emp_date.get((eid, days[day_idx - step]["data"])) == "N":
                    night_streak += 1
                else:
                    break
            for step in (1, 2):
                if day_idx + step < len(days) and shift_by_emp_date.get((eid, days[day_idx + step]["data"])) == "N":
                    night_streak += 1
                else:
                    break
            if night_streak > 2:
                score -= 18.0
                powody.append(f"Uwaga: {night_streak}. zmiana nocna z rzędu")
            score -= emp_nights[eid] * 1.5

        if day_info["czy_weekend"]:
            score -= emp_weekends[eid] * 2.0

        pref_delta, pref_violations = _evaluate_preference_delta(
            pref_by_emp.get(eid, []), day_info, zmiana
        )
        score += pref_delta * 0.5
        if pref_delta > 0:
            powody.append("Zgodne z preferencjami pracownika")
        powody.extend(pref_violations)

        if not powody:
            powody.append("Dostępny bez kolizji")

        candidates.append(
            ReplacementCandidate(
                pracownik_id=eid,
                imie_nazwisko=emp["imie_nazwisko"],
                stanowisko=emp["stanowisko"],
                etat=emp["etat"],
                zaplanowane_godziny=cur_h,
                norma_godzin=tgt_h,
                ocena=max(1, min(100, round(score))),
                dostepny=True,
                powody=powody,
            )
        )

    candidates.sort(key=lambda c: (c.dostepny, c.ocena), reverse=True)
    return candidates


def get_schedule_overview(
    conn: sqlite3.Connection,
    miesiac: str,
    day_required: int = settings.day_shift_required,
    night_required: int = settings.night_shift_required,
    base_hours: int = settings.base_monthly_hours,
) -> dict[str, Any]:
    """Zwraca kompletny stan grafiku na potrzeby frontendu oraz API."""
    ctx = _load_month_context(conn, miesiac)
    issues = validate_schedule(
        conn,
        miesiac,
        day_required=day_required,
        night_required=night_required,
        base_hours=base_hours,
    )

    stats_map: dict[int, dict[str, Any]] = {}
    for emp in ctx["employees"]:
        eid = emp["id"]
        norma = round(emp["etat"] * base_hours)
        stats_map[eid] = {
            "pracownik_id": eid,
            "imie_nazwisko": emp["imie_nazwisko"],
            "stanowisko": emp["stanowisko"],
            "etat": emp["etat"],
            "godziny_wypracowane": 0,
            "norma_godzin": norma,
            "roznica_godzin": -norma,
            "zmiany_dzienne": 0,
            "zmiany_nocne": 0,
            "zmiany_weekendowe": 0,
        }

    weekend_dates = {d["data"] for d in ctx["days"] if d["czy_weekend"]}
    for s in ctx["shifts"]:
        eid = s["pracownik_id"]
        if eid not in stats_map:
            continue
        st = stats_map[eid]
        st["godziny_wypracowane"] += settings.shift_hours
        st["roznica_godzin"] = st["godziny_wypracowane"] - st["norma_godzin"]
        if s["zmiana"] == "D":
            st["zmiany_dzienne"] += 1
        elif s["zmiana"] == "N":
            st["zmiany_nocne"] += 1
        if s["data"] in weekend_dates:
            st["zmiany_weekendowe"] += 1

    return {
        "miesiac": miesiac,
        "dni": ctx["days"],
        "pracownicy": ctx["employees"],
        "zmiany": ctx["shifts"],
        "nieobecnosci": ctx["absences"],
        "preferencje": ctx["preferences"],
        "walidacja": [i.model_dump() for i in issues],
        "statystyki": list(stats_map.values()),
    }


def build_schedule_context_json(
    conn: sqlite3.Connection,
    miesiac: str,
    base_hours: int = settings.base_monthly_hours,
) -> str:
    """Buduje skondensowany kontekst JSON miesiąca dla modeli Ollama."""
    overview = get_schedule_overview(conn, miesiac, base_hours=base_hours)
    compact_shifts_by_day: dict[str, dict[str, list[str]]] = {}
    emp_names = {e["id"]: e["imie_nazwisko"] for e in overview["pracownicy"]}

    for s in overview["zmiany"]:
        dt = s["data"]
        zm = s["zmiana"]
        compact_shifts_by_day.setdefault(dt, {"D": [], "N": []})[zm].append(
            emp_names.get(s["pracownik_id"], f"ID:{s['pracownik_id']}")
        )

    # Pusty miesiąc daje ponad 100 problemów — skracamy listę, by zmieścić się w kontekście modelu
    issues = sorted(overview["walidacja"], key=lambda i: i["severity"] != "error")
    payload = {
        "miesiac": miesiac,
        "pracownicy": overview["statystyki"],
        "nieobecnosci": overview["nieobecnosci"],
        "preferencje": overview["preferencje"],
        "obsada_dzienna": compact_shifts_by_day,
        "liczba_problemow_walidacji": len(issues),
        "problemy_walidacji": [
            {"waga": i["severity"], "opis": i["opis"]} for i in issues[:MAX_ISSUES_IN_AI_CONTEXT]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))