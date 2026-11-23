const state = {
  miesiac: "2026-12",
  overview: null,
  sessionId: "sess_" + Math.random().toString(36).slice(2, 10),
  selectedCell: null, // { pracownik_id, imie_nazwisko, data, zmiana, zablokowana }
};

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatSimpleMarkdown(text) {
  let safe = escapeHtml(text);
  safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  safe = safe.replace(/`([^`]+)`/g, '<code class="bg-slate-900 px-1 py-0.5 rounded text-indigo-300">$1</code>');
  safe = safe.replace(/\n/g, "<br>");
  return safe;
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `Błąd HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err.detail) detail = err.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}



async function checkOllamaHealth() {
  const dot = document.getElementById("ollamaDot");
  const txt = document.getElementById("ollamaStatusText");
  try {
    const data = await apiFetch("/api/health");
    if (data.ollama_connected) {
      dot.className = "w-2 h-2 rounded-full bg-emerald-400";
      txt.textContent = `Ollama OK (${data.available_models.length} modeli)`;
    } else {
      dot.className = "w-2 h-2 rounded-full bg-rose-500";
      txt.textContent = "Ollama offline";
    }
  } catch (_) {
    dot.className = "w-2 h-2 rounded-full bg-rose-500";
    txt.textContent = "Błąd połączenia z API";
  }
}

async function loadSchedule() {
  state.miesiac = document.getElementById("monthPicker").value || "2026-12";
  const data = await apiFetch(`/api/schedule/${state.miesiac}`);
  state.overview = data;
  renderScheduleTable();
  renderValidationPanel();
  renderTeamManagement();
}





function renderScheduleTable() {
  const { dni, pracownicy, zmiany, nieobecnosci, statystyki } = state.overview;
  const thead = document.getElementById("scheduleHead");
  const tbody = document.getElementById("scheduleBody");

  const shiftMap = new Map();
  const dayCounts = {};
  dni.forEach((d) => {
    dayCounts[d.data] = { D: 0, N: 0, seniorD: 0, seniorN: 0 };
  });

  const empRoleMap = new Map(pracownicy.map((e) => [e.id, e.stanowisko]));
  zmiany.forEach((s) => {
    shiftMap.set(`${s.pracownik_id}_${s.data}`, s);
    if (dayCounts[s.data]) {
      dayCounts[s.data][s.zmiana]++;
      if (empRoleMap.get(s.pracownik_id) === "starszy") {
        if (s.zmiana === "D") dayCounts[s.data].seniorD++;
        if (s.zmiana === "N") dayCounts[s.data].seniorN++;
      }
    }
  });

  const absenceMap = new Map();
  nieobecnosci.forEach((ab) => {
    dni.forEach((d) => {
      if (d.data >= ab.od && d.data <= ab.do) {
        absenceMap.set(`${ab.pracownik_id}_${d.data}`, ab);
      }
    });
  });

  const statsMap = new Map(statystyki.map((st) => [st.pracownik_id, st]));


  let headHtml = `<tr class="bg-slate-900 border-b border-slate-800">
    <th class="sticky-col text-left px-3 py-2 min-w-[210px] border-r border-slate-800 text-slate-300 font-semibold">
      Pracownik / Etat / Godziny
    </th>`;

  dni.forEach((d) => {
    const isHoliday = Boolean(d.swieto);
    const bgClass = isHoliday
      ? "bg-rose-950/60 text-rose-300"
      : d.czy_weekend
      ? "bg-slate-800/80 text-amber-300"
      : "text-slate-300";
    const titleAttr = isHoliday ? `title="${escapeHtml(d.swieto)}"` : "";
    headHtml += `<th ${titleAttr} class="px-1 py-1.5 text-center border-r border-slate-800/70 ${bgClass}">
      <div class="font-bold">${d.dzien}</div>
      <div class="text-[10px] opacity-80">${d.dzien_tyg}</div>
    </th>`;
  });
  headHtml += `</tr>`;

  // (D / N)
  headHtml += `<tr class="bg-slate-950 border-b border-slate-700 text-[10px]">
    <td class="sticky-col px-3 py-1 border-r border-slate-800 text-slate-400 font-medium">
      Obsada D (min 3) / N (min 2)
    </td>`;
  dni.forEach((d) => {
    const c = dayCounts[d.data];
    const okD = c.D >= 3 && c.seniorD >= 1;
    const okN = c.N >= 2 && c.seniorN >= 1;
    headHtml += `<td class="px-0.5 py-1 text-center border-r border-slate-800/70 leading-tight">
      <div class="${okD ? "text-emerald-400" : "text-rose-400 font-bold"}">D:${c.D}</div>
      <div class="${okN ? "text-indigo-300" : "text-rose-400 font-bold"}">N:${c.N}</div>
    </td>`;
  });
  headHtml += `</tr>`;
  thead.innerHTML = headHtml;

  // Wiersze pracowników
  const roleBadge = {
    starszy: "bg-amber-500/20 text-amber-300 border-amber-500/40",
    pracownik: "bg-slate-700/60 text-slate-300 border-slate-600",
    "młodszy": "bg-cyan-500/20 text-cyan-300 border-cyan-500/40",
  };

  let bodyHtml = "";
  pracownicy.forEach((emp) => {
    const st = statsMap.get(emp.id) || { godziny_wypracowane: 0, norma_godzin: 160 };
    const overNorm = st.godziny_wypracowane > st.norma_godzin + 12;
    const hoursColor = overNorm ? "text-rose-400 font-bold" : "text-slate-300";

    bodyHtml += `<tr class="border-b border-slate-800/70 hover:bg-slate-800/30">
      <td class="sticky-col px-3 py-1.5 border-r border-slate-800">
        <div class="flex items-center justify-between gap-2">
          <span class="font-medium text-slate-100 truncate">${escapeHtml(emp.imie_nazwisko)}</span>
          <span class="px-1.5 py-0.2 rounded border text-[10px] ${roleBadge[emp.stanowisko] || ""}">${emp.stanowisko}</span>
        </div>
        <div class="flex items-center justify-between text-[11px] text-slate-400 mt-0.5">
          <span>Etat: ${emp.etat}</span>
          <span class="${hoursColor}">${st.godziny_wypracowane}h / ${st.norma_godzin}h</span>
        </div>
      </td>`;

    dni.forEach((d) => {
      const key = `${emp.id}_${d.data}`;
      const shift = shiftMap.get(key);
      const absence = absenceMap.get(key);
      const weekendBg = d.czy_weekend || d.swieto ? "bg-slate-900/90" : "";

      let cellContent = "";
      let cellBg = weekendBg;

      if (absence) {
        const shortType = absence.typ === "chorobowe" ? "L4" : absence.typ === "urlop" ? "URL" : "ND";
        cellBg = "bg-rose-950/50";
        cellContent = `<span class="text-[10px] font-semibold text-rose-300" title="${escapeHtml(absence.typ + ": " + absence.notatka)}">${shortType}</span>`;
      }

      if (shift) {
        const isD = shift.zmiana === "D";
        cellBg = isD ? "bg-amber-500/20" : "bg-indigo-500/25";
        const textCol = isD ? "text-amber-300" : "text-indigo-200";
        const lockDot = shift.zablokowana ? '<span class="text-[9px] ml-0.5">🔒</span>' : "";
        cellContent = `<span class="font-bold ${textCol}">${shift.zmiana}</span>${lockDot}`;
      }

      bodyHtml += `<td
        class="shift-cell text-center border-r border-slate-800/60 cursor-pointer select-none ${cellBg}"
        data-emp-id="${emp.id}"
        data-emp-name="${escapeHtml(emp.imie_nazwisko)}"
        data-date="${d.data}"
        data-shift="${shift ? shift.zmiana : ""}"
        data-locked="${shift ? shift.zablokowana : 0}"
      >${cellContent}</td>`;
    });

    bodyHtml += `</tr>`;
  });

  tbody.innerHTML = bodyHtml;

  // Kliknięcie dowolnej komórki
  tbody.querySelectorAll(".shift-cell").forEach((cell) => {
    cell.addEventListener("click", () => {
      openCellModal({
        pracownik_id: Number(cell.dataset.empId),
        imie_nazwisko: cell.dataset.empName,
        data: cell.dataset.date,
        zmiana: cell.dataset.shift || null,
        zablokowana: cell.dataset.locked === "1",
      });
    });
  });
}

// --------WALIDACJA --------------

function renderValidationPanel() {
  const issues = state.overview.walidacja || [];
  const badge = document.getElementById("validationCountBadge");
  const list = document.getElementById("validationList");

  const errorCount = issues.filter((i) => i.severity === "error").length;
  badge.textContent = issues.length;
  badge.className =
    errorCount > 0
      ? "px-1.5 py-0.2 rounded-full text-[11px] bg-rose-600 text-white font-bold"
      : issues.length > 0
      ? "px-1.5 py-0.2 rounded-full text-[11px] bg-amber-500 text-slate-950 font-bold"
      : "px-1.5 py-0.2 rounded-full text-[11px] bg-emerald-600 text-white";

  if (issues.length === 0) {
    list.innerHTML = `<div class="bg-emerald-950/40 border border-emerald-700/50 rounded-xl p-4 text-xs text-emerald-200">
      Brak błędów i ostrzeżeń! Wszystkie zmiany spełniają wymogi obsady, 11h odpoczynku oraz preferencji.
    </div>`;
    return;
  }

  list.innerHTML = issues
    .map((iss) => {
      const isErr = iss.severity === "error";
      const boxCls = isErr
        ? "bg-rose-950/40 border-rose-700/60 text-rose-200"
        : "bg-amber-950/30 border-amber-700/50 text-amber-200";
      const tag = isErr ? "BŁĄD KRYTYCZNY" : "OSTRZEŻENIE";
      return `<div class="border rounded-lg p-3 text-xs flex items-start justify-between gap-3 ${boxCls}">
        <div>
          <span class="font-bold text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-black/30 mr-2">${tag}</span>
          <span>${escapeHtml(iss.opis)}</span>
        </div>
        <span class="text-[11px] opacity-70 font-mono">${escapeHtml(iss.kod)}</span>
      </div>`;
    })
    .join("");
}

// -------------------- ZESPOŁ URLOPY I PREFERENCJE --------------------

function renderTeamManagement() {
  const { pracownicy, nieobecnosci, preferencje } = state.overview;

  // Selecty pracowników
  const optionsHtml = pracownicy
    .map((e) => `<option value="${e.id}">${escapeHtml(e.imie_nazwisko)} (${e.stanowisko})</option>`)
    .join("");
  document.getElementById("absEmpSelect").innerHTML = optionsHtml;
  document.getElementById("prefEmpSelect").innerHTML = optionsHtml;

  // Lista pracowników
  document.getElementById("employeesList").innerHTML = pracownicy
    .map(
      (e) => `<div class="flex items-center justify-between bg-slate-800/70 px-2.5 py-1.5 rounded-lg text-xs">
        <div>
          <div class="font-medium text-slate-100">${escapeHtml(e.imie_nazwisko)}</div>
          <div class="text-[11px] text-slate-400">${e.stanowisko} • etat ${e.etat}</div>
        </div>
        <button onclick="removeEmployee(${e.id})" class="text-rose-400 hover:text-rose-300 text-xs cursor-pointer">Usuń</button>
      </div>`
    )
    .join("");

  // Lista nieobecności
  document.getElementById("absencesList").innerHTML =
    nieobecnosci.length === 0
      ? `<p class="text-xs text-slate-500">Brak zarejestrowanych nieobecności w tym miesiącu.</p>`
      : nieobecnosci
          .map(
            (a) => `<div class="flex items-center justify-between bg-slate-800/70 px-2.5 py-1.5 rounded-lg text-xs">
              <div>
                <div class="font-medium text-slate-100">${escapeHtml(a.imie_nazwisko)} <span class="text-rose-300">(${a.typ})</span></div>
                <div class="text-[11px] text-slate-400">${a.od} → ${a.do} ${a.notatka ? "• " + escapeHtml(a.notatka) : ""}</div>
              </div>
              <button onclick="removeAbsence(${a.id})" class="text-rose-400 hover:text-rose-300 text-xs cursor-pointer">Usuń</button>
            </div>`
          )
          .join("");

  // Lista preferencji
  document.getElementById("preferencesList").innerHTML =
    preferencje.length === 0
      ? `<p class="text-xs text-slate-500">Brak zdefiniowanych preferencji.</p>`
      : preferencje
          .map(
            (p) => `<div class="flex items-center justify-between bg-slate-800/70 px-2.5 py-1.5 rounded-lg text-xs">
              <div>
                <div class="font-medium text-slate-100">${escapeHtml(p.imie_nazwisko)}</div>
                <div class="text-[11px] text-slate-400">${escapeHtml(p.preferencja)} <code class="text-indigo-300">[${p.regula_kod}]</code></div>
              </div>
              <button onclick="removePreference(${p.id})" class="text-rose-400 hover:text-rose-300 text-xs cursor-pointer">Usuń</button>
            </div>`
          )
          .join("");
}

window.removeEmployee = async (id) => {
  await apiFetch(`/api/employees/${id}`, { method: "DELETE" });
  await loadSchedule();
};

window.removeAbsence = async (id) => {
  await apiFetch(`/api/absences/${id}`, { method: "DELETE" });
  await loadSchedule();
};

window.removePreference = async (id) => {
  await apiFetch(`/api/preferences/${id}`, { method: "DELETE" });
  await loadSchedule();
};

// -------------------- MODAL KOMÓRKI I ZASTĘPSTWA --------------------

async function openCellModal(cellInfo) {
  state.selectedCell = cellInfo;
  document.getElementById("cellModalTitle").textContent = `${cellInfo.imie_nazwisko} — ${cellInfo.data}`;
  document.getElementById("cellModalSubtitle").textContent = cellInfo.zmiana
    ? `Aktualna zmiana: ${cellInfo.zmiana === "D" ? "Dzienna (D)" : "Nocna (N)"}`
    : "Brak przypisanej zmiany (dzień wolny)";
  document.getElementById("cellLockCheckbox").checked = true;

  const repSection = document.getElementById("replacementSection");
  const repList = document.getElementById("replacementCandidatesList");

  if (cellInfo.zmiana === "D" || cellInfo.zmiana === "N") {
    repSection.classList.remove("hidden");
    repList.innerHTML = `<p class="text-xs text-slate-400">Wyszukiwanie kandydatów...</p>`;
    try {
      const candidates = await apiFetch(
        `/api/schedule/replacements?data=${cellInfo.data}&zmiana=${cellInfo.zmiana}&zastepowany_pracownik_id=${cellInfo.pracownik_id}`
      );
      repList.innerHTML = candidates
        .filter((c) => c.dostepny)
        .slice(0, 6)
        .map(
          (c) => `<div class="bg-slate-800 border border-slate-700 rounded-lg p-2.5 flex items-center justify-between gap-2 text-xs">
            <div>
              <div class="font-semibold text-white">
                ${escapeHtml(c.imie_nazwisko)}
                <span class="text-[11px] font-normal text-slate-400">(${c.stanowisko}, ${c.zaplanowane_godziny}/${c.norma_godzin}h)</span>
              </div>
              <div class="text-[11px] text-slate-300 mt-0.5">${escapeHtml(c.powody.join(" • "))}</div>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-bold">${c.ocena} pkt</span>
              <button
                onclick="executeSwap(${c.pracownik_id})"
                class="bg-emerald-600 hover:bg-emerald-500 text-white px-2.5 py-1 rounded text-[11px] font-medium cursor-pointer"
              >
                Podmień
              </button>
            </div>
          </div>`
        )
        .join("");
    } catch (e) {
      repList.innerHTML = `<p class="text-xs text-rose-400">${escapeHtml(e.message)}</p>`;
    }
  } else {
    repSection.classList.add("hidden");
  }

  document.getElementById("cellModal").classList.remove("hidden");
}

window.executeSwap = async (targetEmpId) => {
  const c = state.selectedCell;
  if (!c || !c.zmiana) return;
  await apiFetch("/api/schedule/swap", {
    method: "POST",
    body: JSON.stringify({
      miesiac: state.miesiac,
      data: c.data,
      zmiana: c.zmiana,
      z_pracownik_id: c.pracownik_id,
      na_pracownik_id: targetEmpId,
      zablokuj: true,
    }),
  });
  document.getElementById("cellModal").classList.add("hidden");
  await loadSchedule();
};

// -------------------- ASYSTENT AI (LANGGRAPH) --------------------

function appendChatBubble(role, htmlContent, meta = {}) {
  const container = document.getElementById("chatMessages");
  const wrapper = document.createElement("div");

  if (role === "user") {
    wrapper.className = "bg-indigo-600 text-white rounded-xl p-3 ml-8 leading-relaxed";
    wrapper.innerHTML = escapeHtml(htmlContent);
  } else {
    wrapper.className = "bg-slate-800/90 border border-slate-700 rounded-xl p-3 mr-2 text-slate-100 leading-relaxed space-y-2";
    let traceHtml = "";
    if (meta.graph_trace && meta.graph_trace.length) {
      traceHtml = `<div class="text-[10px] text-slate-400 font-mono border-t border-slate-700/80 pt-1.5">
        Ścieżka LangGraph: ${escapeHtml(meta.graph_trace.join(" → "))}
      </div>`;
    }

    let actionHtml = "";
    if (meta.proposed_action) {
      const actionId = "act_" + Math.random().toString(36).slice(2, 9);
      window[actionId] = meta.proposed_action;
      actionHtml = `<div class="bg-indigo-950/70 border border-indigo-500/50 rounded-lg p-2.5 space-y-2">
        <div class="font-semibold text-indigo-200">${escapeHtml(meta.proposed_action.title)}</div>
        <div class="text-[11px] text-slate-300">${escapeHtml(meta.proposed_action.description)}</div>
        <button
          onclick="approveAiAction('${actionId}', this)"
          class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-1.5 rounded-md text-xs cursor-pointer"
        >
          Zatwierdź i zapisz w grafiku
        </button>
      </div>`;
    }

    wrapper.innerHTML = `<div>${formatSimpleMarkdown(htmlContent)}</div>${actionHtml}${traceHtml}`;
  }

  container.appendChild(wrapper);
  container.scrollTop = container.scrollHeight;
}

window.approveAiAction = async (actionId, btnEl) => {
  const actionObj = window[actionId];
  if (!actionObj) return;
  btnEl.disabled = true;
  btnEl.textContent = "Wykonywanie...";
  try {
    await apiFetch("/api/ai/execute-action", {
      method: "POST",
      body: JSON.stringify(actionObj),
    });
    btnEl.className = "w-full bg-slate-700 text-emerald-300 font-medium py-1.5 rounded-md text-xs";
    btnEl.textContent = "Zastosowano w bazie danych";
    await loadSchedule();
  } catch (err) {
    btnEl.disabled = false;
    btnEl.textContent = "Błąd: " + err.message;
  }
};

async function sendChatMessage(messageText) {
  appendChatBubble("user", messageText);
  const sendBtn = document.getElementById("chatSendBtn");
  sendBtn.disabled = true;
  sendBtn.textContent = "Analiza...";

  try {
    const res = await apiFetch("/api/ai/chat", {
      method: "POST",
      body: JSON.stringify({
        message: messageText,
        miesiac: state.miesiac,
        session_id: state.sessionId,
        router_model: document.getElementById("modelRouter").value.trim(),
        reasoning_model: document.getElementById("modelReasoning").value.trim(),
        writer_model: document.getElementById("modelWriter").value.trim(),
      }),
    });
    appendChatBubble("assistant", res.reply, {
      proposed_action: res.proposed_action,
      graph_trace: res.graph_trace,
    });
  } catch (err) {
    appendChatBubble("assistant", `Błąd komunikacji z backendem: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = "Wyślij";
  }
}

// -------------------- ZDARZENIA INTERFEJSU --------------------

document.addEventListener("DOMContentLoaded", async () => {
  await checkOllamaHealth();
  await loadSchedule();

  document.getElementById("monthPicker").addEventListener("change", loadSchedule);

  // Zakładki
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.tab;
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.id !== `tab-${target}`);
      });
    });
  });

  // Modal komórki
  document.getElementById("closeCellModal").addEventListener("click", () => {
    document.getElementById("cellModal").classList.add("hidden");
  });

  document.querySelectorAll(".shift-choice-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const c = state.selectedCell;
      if (!c) return;
      const newShift = btn.dataset.setShift || null;
      const locked = document.getElementById("cellLockCheckbox").checked;
      await apiFetch("/api/schedule/shift", {
        method: "PUT",
        body: JSON.stringify({
          miesiac: state.miesiac,
          data: c.data,
          pracownik_id: c.pracownik_id,
          zmiana: newShift,
          zablokowana: locked,
        }),
      });
      document.getElementById("cellModal").classList.add("hidden");
      await loadSchedule();
    });
  });

  // Modal generowania
  const genModal = document.getElementById("generateModal");
  document.getElementById("btnOpenGenerate").addEventListener("click", () => genModal.classList.remove("hidden"));
  document.getElementById("closeGenerateModal").addEventListener("click", () => genModal.classList.add("hidden"));
  document.getElementById("cancelGenerateModal").addEventListener("click", () => genModal.classList.add("hidden"));

  document.getElementById("confirmGenerateBtn").addEventListener("click", async () => {
    const btn = document.getElementById("confirmGenerateBtn");
    btn.disabled = true;
    btn.textContent = "Generowanie...";
    try {
      await apiFetch("/api/schedule/generate", {
        method: "POST",
        body: JSON.stringify({
          miesiac: state.miesiac,
          zachowaj_zablokowane: document.getElementById("genKeepLocked").checked,
          tryb_naprawy: document.getElementById("genRepairMode").checked,
          obsada_dzien: Number(document.getElementById("genDayReq").value),
          obsada_noc: Number(document.getElementById("genNightReq").value),
          norma_godzin_etat: Number(document.getElementById("genBaseHours").value),
        }),
      });
      genModal.classList.add("hidden");
      await loadSchedule();
    } finally {
      btn.disabled = false;
      btn.textContent = "Uruchom generator";
    }
  });

  // Auto-naprawa z panelu walidacji
  document.getElementById("btnRepairSchedule").addEventListener("click", async () => {
    await apiFetch("/api/schedule/generate", {
      method: "POST",
      body: JSON.stringify({
        miesiac: state.miesiac,
        zachowaj_zablokowane: true,
        tryb_naprawy: true,
      }),
    });
    await loadSchedule();
  });

  // Migawki
  const snapModal = document.getElementById("snapshotsModal");
  async function refreshSnapshots() {
    const snaps = await apiFetch(`/api/snapshots/${state.miesiac}`);
    document.getElementById("snapshotsList").innerHTML =
      snaps.length === 0
        ? `<p class="text-slate-400">Brak zapisanych migawek dla tego miesiąca.</p>`
        : snaps
            .map(
              (s) => `<div class="bg-slate-800 border border-slate-700 rounded-lg p-2.5 flex items-center justify-between">
                <div>
                  <div class="font-medium text-white">${escapeHtml(s.nazwa)}</div>
                  <div class="text-[11px] text-slate-400">${s.utworzono}</div>
                </div>
                <button onclick="restoreSnapshot(${s.id})" class="bg-amber-600 hover:bg-amber-500 text-white px-2.5 py-1 rounded text-[11px] cursor-pointer">
                  Przywróć
                </button>
              </div>`
            )
            .join("");
  }

  window.restoreSnapshot = async (id) => {
    await apiFetch(`/api/snapshots/restore/${id}`, { method: "POST" });
    snapModal.classList.add("hidden");
    await loadSchedule();
  };

  document.getElementById("btnOpenSnapshots").addEventListener("click", async () => {
    await refreshSnapshots();
    snapModal.classList.remove("hidden");
  });
  document.getElementById("closeSnapshotsModal").addEventListener("click", () => snapModal.classList.add("hidden"));
  document.getElementById("btnSaveSnapshot").addEventListener("click", async () => {
    const nazwa = document.getElementById("snapshotNameInput").value.trim() || "Ręczna kopia grafiku";
    await apiFetch(`/api/snapshots/${state.miesiac}?nazwa=${encodeURIComponent(nazwa)}`, { method: "POST" });
    document.getElementById("snapshotNameInput").value = "";
    await refreshSnapshots();
  });

  // Formularze zespołu
  document.getElementById("formAddEmployee").addEventListener("submit", async (e) => {
    e.preventDefault();
    await apiFetch("/api/employees", {
      method: "POST",
      body: JSON.stringify({
        imie_nazwisko: document.getElementById("empName").value,
        stanowisko: document.getElementById("empRole").value,
        etat: Number(document.getElementById("empFte").value),
      }),
    });
    document.getElementById("empName").value = "";
    await loadSchedule();
  });

  document.getElementById("formAddAbsence").addEventListener("submit", async (e) => {
    e.preventDefault();
    await apiFetch("/api/absences", {
      method: "POST",
      body: JSON.stringify({
        pracownik_id: Number(document.getElementById("absEmpSelect").value),
        od: document.getElementById("absFrom").value,
        do: document.getElementById("absTo").value,
        typ: document.getElementById("absType").value,
        notatka: document.getElementById("absNote").value,
        usun_kolidujace_zmiany: true,
      }),
    });
    document.getElementById("absNote").value = "";
    await loadSchedule();
  });

  document.getElementById("formAddPreference").addEventListener("submit", async (e) => {
    e.preventDefault();
    await apiFetch("/api/preferences", {
      method: "POST",
      body: JSON.stringify({
        pracownik_id: Number(document.getElementById("prefEmpSelect").value),
        preferencja: document.getElementById("prefText").value,
      }),
    });
    document.getElementById("prefText").value = "";
    await loadSchedule();
  });

  // Czat AI
  document.getElementById("chatForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("chatInput");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    await sendChatMessage(msg);
  });

  document.querySelectorAll(".quick-prompt").forEach((btn) => {
    btn.addEventListener("click", () => sendChatMessage(btn.textContent.trim()));
  });

  document.getElementById("btnClearChat").addEventListener("click", () => {
    state.sessionId = "sess_" + Math.random().toString(36).slice(2, 10);
    document.getElementById("chatMessages").innerHTML = `<div class="bg-slate-800/80 border border-slate-700/80 rounded-xl p-3 text-slate-200">
      Rozpoczęto nową sesję czatu AI.
    </div>`;
  });
});