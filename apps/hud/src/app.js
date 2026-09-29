(() => {
  const WS_URL = "ws://127.0.0.1:8765";
  const orb = document.getElementById("orb");
  const stateEl = document.getElementById("state");
  const confirm = document.getElementById("confirm");
  const confirmText = document.getElementById("confirm-text");
  const hint = document.getElementById("hint");
  const activity = document.getElementById("activity");
  const jobsEl = document.getElementById("jobs");
  const jobCount = document.getElementById("job-count");
  const transcript = document.getElementById("transcript");
  const integrationsEl = document.getElementById("integrations");
  const routinesEl = document.getElementById("routines");
  let ws = null;
  let pttDown = false;
  const jobs = new Map();

  function setState(state) {
    const s = state || "idle";
    stateEl.textContent = s;
    orb.className = `orb ${s}`;
    if (s === "awaiting_confirm") {
      confirm.classList.remove("hidden");
    } else {
      confirm.classList.add("hidden");
    }
  }

  function renderJobs() {
    const active = [...jobs.values()].filter((job) =>
      !["done", "error", "cancelled"].includes(job.status)
    );
    jobCount.textContent = String(active.length);
    jobsEl.replaceChildren();
    for (const job of active.slice(0, 4)) {
      const row = document.createElement("div");
      row.className = "job";
      const label = document.createElement("div");
      label.className = "job-label";
      label.textContent = job.label || job.tool || job.title || "Trabajo";
      const progress = document.createElement("progress");
      progress.max = 1;
      progress.value = Number(job.progress || 0);
      const cancel = document.createElement("button");
      cancel.className = "job-cancel";
      cancel.textContent = "Cancelar";
      cancel.onclick = () => send("cancel_job", { job_id: job.id });
      row.append(label, progress, cancel);
      jobsEl.append(row);
    }
    activity.classList.toggle(
      "hidden",
      active.length === 0 &&
        !transcript.textContent &&
        integrationsEl.classList.contains("hidden") &&
        routinesEl.classList.contains("hidden")
    );
  }

  function renderIntegrations(statuses) {
    integrationsEl.replaceChildren();
    if (!statuses?.length) {
      integrationsEl.classList.add("hidden");
      return;
    }
    for (const item of statuses) {
      const chip = document.createElement("span");
      chip.className = `chip ${item.status || "stub"}`;
      chip.title = item.detail || item.status;
      chip.textContent = `${item.service}: ${item.status}`;
      integrationsEl.append(chip);
    }
    integrationsEl.classList.remove("hidden");
    activity.classList.remove("hidden");
  }

  function renderRoutines(routines) {
    routinesEl.replaceChildren();
    const items = (routines || []).filter((item) => item.enabled);
    if (!items.length) {
      routinesEl.classList.add("hidden");
      return;
    }
    for (const item of items.slice(0, 4)) {
      const chip = document.createElement("span");
      chip.className = "chip connected";
      chip.textContent = item.name;
      routinesEl.append(chip);
    }
    routinesEl.classList.remove("hidden");
    activity.classList.remove("hidden");
  }

  function upsertJob(payload, status) {
    if (!payload?.id) return;
    jobs.set(payload.id, { ...(jobs.get(payload.id) || {}), ...payload, status });
    renderJobs();
  }

  function send(type, payload = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type, payload }));
    }
  }

  function startPtt() {
    if (pttDown) return;
    pttDown = true;
    send("ptt_down");
    setState("listening");
    if (hint) hint.textContent = "Suelta para responder…";
  }

  function stopPtt() {
    if (!pttDown) return;
    pttDown = false;
    send("ptt_up");
    setState("thinking");
    if (hint) hint.textContent = "Ctrl+Alt+Space · clic en el orbe";
  }

  function connect() {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => setState("idle");
    ws.onclose = () => {
      setState("idle");
      setTimeout(connect, 1500);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "state") {
        setState(msg.payload?.state);
      }
      if (msg.type === "policy" && msg.payload?.needs_confirm) {
        const reason = msg.payload.reason ? ` · ${msg.payload.reason}` : "";
        const dest = msg.payload.destination ? ` → ${msg.payload.destination}` : "";
        confirmText.textContent = `¿Confirmas ${msg.payload.tool || "acción"}${dest}${reason}?`;
        confirm.classList.remove("hidden");
      }
      if (msg.type === "jobs_snapshot") {
        jobs.clear();
        for (const job of msg.payload?.jobs || []) jobs.set(job.id, job);
        renderJobs();
      }
      if (msg.type === "job_start" || msg.type === "mission_start") {
        upsertJob({ id: msg.payload?.job_id || msg.payload?.id, ...msg.payload, label: msg.payload?.label || msg.payload?.tool || "Cursor" }, "running");
      }
      if (msg.type === "job_progress" || msg.type === "mission_progress") {
        upsertJob({ id: msg.payload?.job_id || msg.payload?.id, ...msg.payload }, "running");
      }
      if (msg.type === "job_verifying") upsertJob(msg.payload, "verifying");
      if (msg.type === "job_done" || msg.type === "mission_done") upsertJob({ id: msg.payload?.job_id || msg.payload?.id, ...msg.payload }, "done");
      if (msg.type === "job_error" || msg.type === "mission_error") upsertJob({ id: msg.payload?.job_id || msg.payload?.id, ...msg.payload }, "error");
      if (msg.type === "job_cancelled" || msg.type === "mission_cancelled") upsertJob({ id: msg.payload?.job_id || msg.payload?.id, ...msg.payload }, "cancelled");
      if (msg.type === "integrations") {
        renderIntegrations(msg.payload?.statuses);
      }
      if (msg.type === "routines") {
        renderRoutines(msg.payload?.routines);
      }
      if (msg.type === "transcript") {
        const role = msg.payload?.role === "assistant" ? "ADAM" : "Luisfer";
        transcript.textContent = `${role}: ${msg.payload?.text || ""}`;
        activity.classList.remove("hidden");
      }
    };
  }

  document.getElementById("yes").onclick = () => {
    send("confirm", { text: "sí" });
    confirm.classList.add("hidden");
  };
  document.getElementById("no").onclick = () => {
    send("confirm", { text: "no" });
    confirm.classList.add("hidden");
  };

  // Hold-to-talk on the orb
  orb.addEventListener("mousedown", (e) => {
    e.preventDefault();
    startPtt();
  });
  window.addEventListener("mouseup", () => stopPtt());
  orb.addEventListener("touchstart", (e) => {
    e.preventDefault();
    startPtt();
  }, { passive: false });
  window.addEventListener("touchend", () => stopPtt());

  window.addEventListener("keydown", (e) => {
    if (e.code === "Space" && e.ctrlKey && e.altKey) {
      e.preventDefault();
      startPtt();
    }
  });
  window.addEventListener("keyup", (e) => {
    if (e.code === "Space" || (e.ctrlKey === false && e.altKey === false)) {
      if (pttDown) stopPtt();
    }
  });

  // Electron global shortcut = press to start, press again to stop (toggle)
  if (window.adamHud?.onPttToggle) {
    window.adamHud.onPttToggle(() => {
      if (pttDown) stopPtt();
      else startPtt();
    });
  }

  connect();
})();
