const state = {
  currentProjectId: null,
  currentProjectMeta: null,
  questions: [],
};

function el(id) {
  return document.getElementById(id);
}

function log(msg, data) {
  const box = el("logBox");
  const line = `[${new Date().toLocaleTimeString()}] ${msg}` + (data ? ` ${JSON.stringify(data)}` : "");
  box.textContent = `${line}\n${box.textContent}`.slice(0, 20000);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const ct = res.headers.get("content-type") || "";
  let body;
  if (ct.includes("application/json")) {
    body = await res.json();
  } else {
    body = await res.text();
  }
  if (!res.ok) {
    const msg = typeof body === "string" ? body : body.detail || JSON.stringify(body);
    throw new Error(msg);
  }
  return body;
}

function requireProject() {
  if (!state.currentProjectId) {
    throw new Error("Please create or load a project first.");
  }
}

async function refreshProjects() {
  const list = await api("/projects");
  const sel = el("projectSelect");
  sel.innerHTML = "";
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No projects";
    sel.appendChild(opt);
    return list;
  }
  for (const p of list) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.id} [${p.device_type}] ${p.status}`;
    sel.appendChild(opt);
  }
  if (state.currentProjectId) {
    sel.value = state.currentProjectId;
  }
  return list;
}

async function loadProject(projectId) {
  const meta = await api(`/projects/${encodeURIComponent(projectId)}`);
  state.currentProjectId = projectId;
  state.currentProjectMeta = meta;
  el("projectMeta").textContent = JSON.stringify(meta, null, 2);
  renderArtifacts(meta.artifacts || []);
  log("project loaded", { projectId });
}

function renderArtifacts(files) {
  const box = el("artifactLinks");
  box.innerHTML = "";
  if (!state.currentProjectId || !files.length) {
    box.textContent = state.currentProjectId ? "No artifacts yet" : "No project selected";
    return;
  }
  for (const name of files) {
    const a = document.createElement("a");
    a.className = "artifact-link";
    a.href = `/projects/${encodeURIComponent(state.currentProjectId)}/artifact/${encodeURIComponent(name)}`;
    a.target = "_blank";
    a.rel = "noreferrer";
    a.textContent = name;
    box.appendChild(a);
  }
}

function collectAnswersFromUI() {
  const answers = {};
  for (const q of state.questions) {
    const input = document.querySelector(`[data-answer-id="${q.id}"]`);
    if (!input) continue;
    const raw = input.value.trim();
    if (!raw) {
      answers[q.id] = "";
      continue;
    }
    try {
      answers[q.id] = JSON.parse(raw);
    } catch {
      answers[q.id] = raw;
    }
  }
  return answers;
}

function renderQuestions(payload) {
  state.questions = payload.questions || [];
  const box = el("questionsContainer");
  box.innerHTML = "";
  if (!state.questions.length) {
    box.textContent = "No questions yet";
    return;
  }
  for (const q of state.questions) {
    const card = document.createElement("div");
    card.className = "question-card";
    const examples = (q.examples || []).map((x) => `- ${x}`).join("\n");
    const evidence = (q.evidence_refs || []).map((x) => `- ${x}`).join("\n");
    card.innerHTML = `
      <div class="qid">${q.id}</div>
      <div><strong>${q.question}</strong></div>
      <div class="why">Why: ${q.why}</div>
      <div class="why">Answer format: <code>${q.answer_format}</code></div>
      ${evidence ? `<pre class="examples">Evidence refs:\n${evidence}</pre>` : ""}
      ${examples ? `<pre class="examples">${examples}</pre>` : ""}
      <textarea data-answer-id="${q.id}" placeholder="Input JSON (recommended) or plain text"></textarea>
    `;
    box.appendChild(card);
  }
}

function setAnalyseProgress(progress, message, stage) {
  const p = Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0;
  const bar = el("analyseProgress");
  if (bar) bar.value = p;
  const text = el("analyseProgressText");
  if (text) {
    const stageText = stage ? ` [${stage}]` : "";
    text.textContent = `${p}%${stageText} ${message || ""}`.trim();
  }
}

async function createProjectHandler(e) {
  e.preventDefault();
  const body = {
    device_name: el("deviceName").value.trim(),
    device_type: el("deviceType").value,
  };
  if (!body.device_name) throw new Error("device_name is required");
  const res = await api("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  state.currentProjectId = res.id;
  log("project created", res);
  await refreshProjects();
  await loadProject(res.id);
}

async function uploadHandler(e) {
  e.preventDefault();
  requireProject();
  const driverFile = el("driverArchive").files[0];
  const refFile = el("referenceArchive").files[0];
  if (!driverFile || !refFile) {
    throw new Error("Please choose both driver zip and reference QEMU zip.");
  }
  const fd = new FormData();
  fd.append("driver_archive", driverFile);
  fd.append("reference_archive", refFile);
  if (el("uploadNote").value.trim()) fd.append("note", el("uploadNote").value.trim());
  const res = await api(`/projects/${encodeURIComponent(state.currentProjectId)}/upload`, {
    method: "POST",
    body: fd,
  });
  el("uploadResult").textContent = JSON.stringify(res, null, 2);
  log("upload finished", res);
  await loadProject(state.currentProjectId);
}

async function analyseHandler() {
  requireProject();
  const payload = {
    llm_config_path: el("llmConfigPath").value.trim() || null,
    use_llm_questions: true,
    allow_heuristic_fallback: false,
    question_top_k: 12,
    question_temperature: 0.1,
    question_max_tokens: 4096,
  };

  const analyseBtn = el("analyseBtn");
  analyseBtn.disabled = true;
  el("analyseSummary").textContent = "";
  el("analyseLive").textContent = "";
  setAnalyseProgress(0, "Analyse started", "prepare");
  log("analyse start", payload);

  try {
    const res = await fetch(`/projects/${encodeURIComponent(state.currentProjectId)}/analyse_stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok || !res.body) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) continue;
        let evt;
        try {
          evt = JSON.parse(line);
        } catch {
          continue;
        }
        if (evt.type === "status") {
          setAnalyseProgress(evt.progress, evt.message, evt.stage);
          el("analyseLive").textContent = JSON.stringify(evt, null, 2);
        } else if (evt.type === "done") {
          finalResult = evt.result || null;
          setAnalyseProgress(100, "Analyse completed", "done");
        } else if (evt.type === "error") {
          setAnalyseProgress(0, evt.message || "Analyse failed", "error");
          throw new Error(evt.message || "Analyse failed");
        }
      }
    }

    if (finalResult) {
      el("analyseSummary").textContent = JSON.stringify(finalResult, null, 2);
      log("analyse finished", finalResult.summary || finalResult);
      await loadQuestionsHandler();
      await loadProject(state.currentProjectId);
    }
  } finally {
    analyseBtn.disabled = false;
  }
}

async function loadQuestionsHandler() {
  requireProject();
  const payload = await api(`/projects/${encodeURIComponent(state.currentProjectId)}/questions`);
  renderQuestions(payload);
  log("questions loaded", {
    count: (payload.questions || []).length,
    source: payload.source,
    budget: payload.question_budget,
  });
}

async function saveAnswersHandler() {
  requireProject();
  const answers = collectAnswersFromUI();
  const res = await api(`/projects/${encodeURIComponent(state.currentProjectId)}/answers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answers }),
  });
  el("answersSaveResult").textContent = JSON.stringify(res, null, 2);
  log("answers saved", { count: res.answer_count });
  await loadProject(state.currentProjectId);
}

async function generateHandler() {
  requireProject();
  const payload = {
    llm_config_path: el("llmConfigPath").value.trim() || null,
    top_k: Number(el("topK").value || 12),
    generate_report: el("generateReport").checked,
    temperature: Number(el("temperature").value || 0.1),
    max_tokens: el("maxTokens").value.trim() ? Number(el("maxTokens").value.trim()) : null,
  };

  el("streamOutput").textContent = "";
  el("generateStatus").textContent = "Generation started...";
  log("generate start", payload);

  const res = await fetch(`/projects/${encodeURIComponent(state.currentProjectId)}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok || !res.body) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let gotError = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      let evt;
      try {
        evt = JSON.parse(line);
      } catch {
        continue;
      }
      if (evt.type === "status") {
        el("generateStatus").textContent = JSON.stringify(evt, null, 2);
      } else if (evt.type === "token") {
        el("streamOutput").textContent += evt.text || "";
        el("streamOutput").scrollTop = el("streamOutput").scrollHeight;
      } else if (evt.type === "error") {
        gotError = true;
        el("generateStatus").textContent = `Generation failed: ${evt.message}`;
        log("generate error", evt);
      } else if (evt.type === "done") {
        el("generateStatus").textContent = JSON.stringify(evt, null, 2);
        log("generate done", evt);
      }
    }
  }

  if (!gotError) {
    await loadProject(state.currentProjectId);
  }
}

function bind() {
  el("createProjectForm").addEventListener("submit", (e) => wrap(createProjectHandler, e));
  el("uploadForm").addEventListener("submit", (e) => wrap(uploadHandler, e));
  el("analyseBtn").addEventListener("click", () => wrap(analyseHandler));
  el("loadQuestionsBtn").addEventListener("click", () => wrap(loadQuestionsHandler));
  el("saveAnswersBtn").addEventListener("click", () => wrap(saveAnswersHandler));
  el("generateBtn").addEventListener("click", () => wrap(generateHandler));
  el("refreshProjectsBtn").addEventListener("click", () => wrap(refreshProjects));
  el("loadProjectBtn").addEventListener("click", () => {
    const id = el("projectSelect").value;
    if (!id) return;
    wrap(() => loadProject(id));
  });
}

async function wrap(fn, arg) {
  try {
    await fn(arg);
  } catch (err) {
    const msg = err?.message || String(err);
    log("error", { message: msg });
    alert(msg);
  }
}

async function init() {
  bind();
  await refreshProjects();
  setAnalyseProgress(0, "Idle", "prepare");
}

init();
