import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

from backend.config import settings


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session() -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pracownicy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imie_nazwisko TEXT NOT NULL,
                stanowisko TEXT NOT NULL CHECK(stanowisko IN ('starszy', 'pracownik', 'młodszy')),
                etat REAL NOT NULL DEFAULT 1.0 CHECK(etat > 0 AND etat <= 1.0),
                aktywny INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS nieobecnosci (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pracownik_id INTEGER NOT NULL,
                od TEXT NOT NULL,
                do TEXT NOT NULL,
                typ TEXT NOT NULL CHECK(typ IN ('urlop', 'chorobowe', 'niedostępny')),
                notatka TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(pracownik_id) REFERENCES pracownicy(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS preferencje (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pracownik_id INTEGER NOT NULL,
                preferencja TEXT NOT NULL,
                regula_kod TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(pracownik_id) REFERENCES pracownicy(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS grafik (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                miesiac TEXT NOT NULL,
                data TEXT NOT NULL,
                pracownik_id INTEGER NOT NULL,
                zmiana TEXT NOT NULL CHECK(zmiana IN ('D', 'N')),
                zablokowana INTEGER NOT NULL DEFAULT 0,
                UNIQUE(data, pracownik_id),
                FOREIGN KEY(pracownik_id) REFERENCES pracownicy(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS grafik_migawki (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                miesiac TEXT NOT NULL,
                nazwa TEXT NOT NULL,
                utworzono TEXT NOT NULL,
                dane_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_grafik_miesiac ON grafik(miesiac);
            CREATE INDEX IF NOT EXISTS idx_grafik_data ON grafik(data);
            CREATE INDEX IF NOT EXISTS idx_nieobecnosci_pracownik ON nieobecnosci(pracownik_id);
            """
        )

        count = conn.execute("SELECT COUNT(*) AS cnt FROM pracownicy").fetchone()["cnt"]
        if count == 0:
            _seed_kostecki_labs(conn)


def _seed_kostecki_labs(conn: sqlite3.Connection) -> None:
    employees = [
        (1, "Anna Kowalska", "starszy", 1.0),
        (2, "Marek Nowak", "starszy", 1.0),
        (3, "Katarzyna Wiśniewska", "starszy", 1.0),
        (4, "Piotr Wójcik", "starszy", 1.0),
        (5, "Ewa Kamińska", "starszy", 0.75),
        (6, "Paweł Zieliński", "starszy", 1.0),
        (7, "Magdalena Szymańska", "pracownik", 1.0),
        (8, "Łukasz Woźniak", "pracownik", 1.0),
        (9, "Agnieszka Dąbrowska", "pracownik", 1.0),
        (10, "Michał Kozłowski", "pracownik", 0.75),
        (11, "Joanna Jankowska", "pracownik", 1.0),
        (12, "Krzysztof Mazur", "pracownik", 0.5),
        (13, "Natalia Kwiatkowska", "młodszy", 1.0),
        (14, "Bartosz Krawczyk", "młodszy", 0.5),
    ]
    conn.executemany(
        "INSERT INTO pracownicy (id, imie_nazwisko, stanowisko, etat, aktywny) VALUES (?, ?, ?, ?, 1)",
        employees,
    )

    absences = [
        (1, "2026-12-21", "2026-12-27", "urlop", "Urlop świąteczny"),
        (6, "2026-12-14", "2026-12-18", "urlop", "Urlop wypoczynkowy"),
        (9, "2026-12-07", "2026-12-11", "urlop", "Urlop wypoczynkowy"),
        (8, "2026-12-28", "2026-12-31", "urlop", "Urlop sylwestrowy"),
        (2, "2026-12-03", "2026-12-03", "niedostępny", "Sprawy prywatne"),
    ]
    conn.executemany(
        "INSERT INTO nieobecnosci (pracownik_id, od, do, typ, notatka) VALUES (?, ?, ?, ?, ?)",
        absences,
    )

    preferences = [
        (11, "wolę nie w weekendy", "NO_WEEKENDS"),
        (5, "wolę tylko dzienne zmiany", "ONLY_DAY"),
        (10, "lubię nocki", "PREFER_NIGHT"),
        (4, "chcę wolne 2026-12-31", "DAY_OFF:2026-12-31"),
        (13, "chcę wolne 2026-12-24", "DAY_OFF:2026-12-24"),
        (12, "najlepiej wtorki i czwartki", "PREFERRED_WEEKDAYS:1,3"),
    ]
    conn.executemany(
        "INSERT INTO preferencje (pracownik_id, preferencja, regula_kod) VALUES (?, ?, ?)",
        preferences,
    )


def save_snapshot(conn: sqlite3.Connection, miesiac: str, nazwa: str, dane_json: str) -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO grafik_migawki (miesiac, nazwa, utworzono, dane_json) VALUES (?, ?, ?, ?)",
        (miesiac, nazwa, now_str, dane_json),
    )
    return int(cur.lastrowid)