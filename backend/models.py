from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


StanowiskoType = Literal["starszy", "pracownik", "młodszy"]
NieobecnoscTyp = Literal["urlop", "chorobowe", "niedostępny"]
ZmianaTyp = Literal["D", "N"]


class EmployeeCreate(BaseModel):
    imie_nazwisko: str = Field(..., min_length=2, max_length=100)
    stanowisko: StanowiskoType
    etat: float = Field(default=1.0, gt=0.0, le=1.0)


class EmployeeUpdate(BaseModel):
    imie_nazwisko: Optional[str] = Field(default=None, min_length=2, max_length=100)
    stanowisko: Optional[StanowiskoType] = None
    etat: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    aktywny: Optional[bool] = None


class EmployeeOut(BaseModel):
    id: int
    imie_nazwisko: str
    stanowisko: StanowiskoType
    etat: float
    aktywny: bool


class AbsenceCreate(BaseModel):
    pracownik_id: int
    od: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    do: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    typ: NieobecnoscTyp
    notatka: str = ""
    usun_kolidujace_zmiany: bool = True


class AbsenceOut(BaseModel):
    id: int
    pracownik_id: int
    imie_nazwisko: str
    od: str
    do: str
    typ: NieobecnoscTyp
    notatka: str


class PreferenceCreate(BaseModel):
    pracownik_id: int
    preferencja: str = Field(..., min_length=2, max_length=200)
    regula_kod: Optional[str] = None


class PreferenceOut(BaseModel):
    id: int
    pracownik_id: int
    imie_nazwisko: str
    preferencja: str
    regula_kod: str


class ShiftAssignmentUpdate(BaseModel):
    miesiac: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    data: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    pracownik_id: int
    zmiana: Optional[ZmianaTyp] = None
    zablokowana: bool = True


class ShiftSwapRequest(BaseModel):
    miesiac: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    data: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    zmiana: ZmianaTyp
    z_pracownik_id: int
    na_pracownik_id: int
    zablokuj: bool = True


class GenerateScheduleRequest(BaseModel):
    miesiac: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    zachowaj_zablokowane: bool = True
    tryb_naprawy: bool = False
    obsada_dzien: int = Field(default=3, ge=1, le=10)
    obsada_noc: int = Field(default=2, ge=1, le=10)
    norma_godzin_etat: int = Field(default=160, ge=80, le=220)


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    kod: str
    data: Optional[str] = None
    pracownik_id: Optional[int] = None
    imie_nazwisko: Optional[str] = None
    opis: str


class ReplacementCandidate(BaseModel):
    pracownik_id: int
    imie_nazwisko: str
    stanowisko: StanowiskoType
    etat: float
    zaplanowane_godziny: int
    norma_godzin: int
    ocena: int
    dostepny: bool
    powody: list[str]


class AIChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    miesiac: str = Field(default="2026-12", pattern=r"^\d{4}-\d{2}$")
    session_id: str = Field(default="default")
    router_model: Optional[str] = None
    reasoning_model: Optional[str] = None
    writer_model: Optional[str] = None


class AIProposedAction(BaseModel):
    action_type: Literal[
        "ADD_ABSENCE",
        "SWAP_SHIFTS",
        "ASSIGN_SHIFT",
        "ADD_PREFERENCE",
        "REGENERATE_SCHEDULE",
    ]
    title: str
    description: str
    payload: dict[str, Any]