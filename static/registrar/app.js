const els = {
  countInput: document.getElementById("countInput"),
  registerMode: document.getElementById("registerMode"),
  startBtn: document.getElementById("startBtn"),
  refreshJobsBtn: document.getElementById("refreshJobsBtn"),
  refreshAccountsBtn: document.getElementById("refreshAccountsBtn"),
  diagGptBtn: document.getElementById("diagGptBtn"),
  planImageTestBtn: document.getElementById("planImageTestBtn"),
  batchRefreshClioBtn: document.getElementById("batchRefreshClioBtn"),
  batchImageTestBtn: document.getElementById("batchImageTestBtn"),
  clearLogBtn: document.getElementById("clearLogBtn"),
  statePill: document.getElementById("statePill"),
  jobInfo: document.getElementById("jobInfo"),
  logBox: document.getElementById("logBox"),
  historyList: document.getElementById("historyList"),
  accountRows: document.getElementById("accountRows"),
  accountSummary: document.getElementById("accountSummary"),
  accountTokenSummary: document.getElementById("accountTokenSummary"),
  metricTotal: document.getElementById("metricTotal"),
  metricSuccess: document.getElementById("metricSuccess"),
  metricChallenge: document.getElementById("metricChallenge"),
  metricWebImage: document.getElementById("metricWebImage"),
  metricImage: document.getElementById("metricImage"),
  metricFailed: document.getElementById("metricFailed"),
  toast: document.getElementById("toast"),
};

let currentJobId = localStorage.getItem("registrar:lastJobId") || "";
let polling = null;
let busy = new Set();

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const detail = data?.detail || data?.message || `HTTP ${res.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data || {};
}
function toast(text) { els.toast.textContent = text; els.toast.className = "toast show"; clearTimeout(toast.timer); toast.timer = setTimeout(() => { els.toast.className = "toast"; }, 2600); }
function print(line) { els.logBox.textContent += `${els.logBox.textContent ? "\n" : ""}[${new Date().toLocaleTimeString("zh-CN", {hour12:false})}] ${line}`; els.logBox.scrollTop = els.logBox.scrollHeight; }
function setLoading(key, on) { on ? busy.add(key) : busy.delete(key); renderButtonsLoading(); }
function renderButtonsLoading() { document.querySelectorAll("[data-busy-key]").forEach(btn => { const on = busy.has(btn.dataset.busyKey); btn.disabled = on; btn.classList.toggle("loading", on); }); }
function setState(status) { els.statePill.textContent = String(status || "ready").toUpperCase(); els.statePill.className = `state-pill ${status === "running" ? "running" : status === "succeeded" ? "done" : status === "failed" ? "failed" : ""}`; els.startBtn.disabled = status === "running"; }
function escapeHtml(value) { return String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;"); }
function badge(text, kind="wait") { return `<span class="badge ${kind}">${escapeHtml(text)}</span>`; }

function renderJob(job) {
  els.metricTotal.textContent = String(job.total || 0);
  els.metricSuccess.textContent = String(job.success_count || 0);
  els.metricChallenge.textContent = String(job.challenge_count || 0);
  els.metricWebImage.textContent = String(job.web_image_success_count || 0);
  els.metricImage.textContent = String(job.image_success_count || 0);
  els.metricFailed.textContent = String(job.failed_count || 0);
  els.jobInfo.textContent = `Job ${job.id} · mode=${job.mode || "http_request"} · ${job.current || 0}/${job.total || 0} · proxy=${job.used_proxy ? job.proxy : "未启用"}`;
  if (Array.isArray(job.logs)) { els.logBox.textContent = job.logs.join("\n") || "等待日志..."; els.logBox.scrollTop = els.logBox.scrollHeight; }
  setState(job.status || "ready");
}
async function pollJob(jobId) {
  const job = await api(`/api/v1/registrar/jobs/${encodeURIComponent(jobId)}`);
  renderJob(job);
  if (job.status !== "running") {
    clearInterval(polling); polling = null;
    toast(`注册机完成：成功 ${job.success_count || 0}/${job.total || 0}`);
    await Promise.all([loadHistory(), loadAccounts()]);
  }
}
async function startJob() {
  const count = Math.max(1, Math.min(50, Number(els.countInput.value) || 1));
  const mode = els.registerMode?.value || "http_request";
  els.logBox.textContent = `正在启动自动注册脚本 mode=${mode}...`;
  setState("running");
  const data = await api("/api/v1/registrar/jobs", { method: "POST", body: JSON.stringify({ count, mode }) });
  currentJobId = data.job_id; localStorage.setItem("registrar:lastJobId", currentJobId);
  print(`JOB_STARTED id=${currentJobId} count=${count} mode=${data.mode || mode}`);
  if (polling) clearInterval(polling);
  await pollJob(currentJobId);
  polling = setInterval(() => pollJob(currentJobId).catch(err => print(`POLL_FAIL ${err.message}`)), 1500);
}
async function loadHistory() {
  const data = await api("/api/v1/registrar/jobs?limit=8");
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  els.historyList.innerHTML = jobs.length ? jobs.map(job => `
    <button class="history-item" type="button" data-job="${escapeHtml(job.id)}">
      <span><strong>${escapeHtml(job.id)}</strong><br>${escapeHtml(job.status)} · ${job.current || 0}/${job.total || 0}</span>
      <span>成功 ${job.success_count || 0} · 网页 ${job.web_image_success_count || 0} · 6001 ${job.image_success_count || 0} · 失败 ${job.failed_count || 0}</span>
    </button>`).join("") : '<div class="history-item">暂无历史任务</div>';
}

function accountStatusKind(v) { return v === "registered" || v === "ready" ? "ok" : v === "need_human" || v === "failed" ? "bad" : "wait"; }
function imageKind(v) { return v === "passed" || v === "ok" ? "ok" : v === "failed" || v === "fail" ? "bad" : "wait"; }
function renderAccountTokenSummary(accounts) {
  let withActive = 0, withClio = 0, projectxOnly = 0, noActive = 0, imagePassed = 0, imageFailed = 0;
  accounts.forEach(a => {
    const ts = a.token_summary || {};
    const active = Number(ts.active || 0);
    const activeClients = ts.active_by_client_id || {};
    if (active > 0) withActive += 1; else noActive += 1;
    if ((activeClients["clio-playground-web"] || []).length) withClio += 1;
    else if ((activeClients["projectx_webapp"] || []).length) projectxOnly += 1;
    const img = String(a.image_status || "").toLowerCase();
    if (["passed", "ok", "success", "succeeded"].includes(img)) imagePassed += 1;
    if (["failed", "fail"].includes(img)) imageFailed += 1;
  });
  els.accountTokenSummary.innerHTML = [
    ["账号", accounts.length],
    ["有 active token", withActive],
    ["Clio 可出图", withClio],
    ["仅 ProjectX", projectxOnly],
    ["无 active token", noActive],
    ["6001 已通过", imagePassed],
    ["6001 失败", imageFailed],
  ].map(([k, v]) => `<span class="summary-pill">${escapeHtml(k)} <strong>${escapeHtml(v)}</strong></span>`).join("");
}
async function loadAccounts() {
  const data = await api("/api/v1/adobe/accounts");
  const accounts = Array.isArray(data.accounts) ? data.accounts : [];
  els.accountSummary.textContent = `共 ${accounts.length} 个账号`;
  renderAccountTokenSummary(accounts);
  if (!accounts.length) { els.accountRows.innerHTML = '<tr><td colspan="6" class="empty">暂无账号</td></tr>'; return; }
  els.accountRows.innerHTML = accounts.map(a => {
    const id = escapeHtml(a.id);
    const code = a.verification_code || "";
    const hasCookie = !!a.session_state_path || !!a.cookie_profile_id;
    return `<tr>
      <td><strong>${escapeHtml(a.email)}</strong><br><span class="muted mono">${escapeHtml(a.password || "")}</span></td>
      <td>${badge(a.status || "-", accountStatusKind(a.status))}<br><span class="muted">mail:${escapeHtml(a.mail_status || "-")}</span><br><span class="muted">token:${escapeHtml(a.token_status || "-")}</span>${a.token_summary ? `<br><span class="muted mono">active:${escapeHtml(a.token_summary.active || 0)} img:${escapeHtml(a.token_summary.preferred_image_token_id || "-")}</span>` : ""}${a.token_refresh_status ? `<br><span class="muted">refresh:${escapeHtml(a.token_refresh_status)}</span>` : ""}</td>
      <td><span class="code-box" id="code-${id}">${escapeHtml(code || "-")}</span></td>
      <td>${hasCookie ? badge("可导出", "ok") : badge("无cookie", "wait")}<br><span class="muted mono">${escapeHtml(a.cookie_profile_id || "")}</span></td>
      <td>
        <div>网页：${badge(a.web_image_status || "untested", imageKind(a.web_image_status))}${a.web_image_test_url ? ` <a class="link" href="${escapeHtml(a.web_image_test_url)}" target="_blank">打开</a>` : ""}</div>
        <div class="mt6">6001：${badge(a.image_status || "untested", imageKind(a.image_status))}${a.image_test_url ? ` <a class="link" href="${escapeHtml(a.image_test_url)}" target="_blank">打开</a>` : ""}</div>
        ${a.image_test_token_id ? `<div class="mt6 muted mono">token:${escapeHtml(a.image_test_token_id)}</div>` : ""}
      </td>
      <td class="row-actions">
        <button class="ghost small" data-action="code" data-id="${id}" data-busy-key="code-${id}">获取验证码</button>
        <button class="ghost small" data-action="refresh-clio" data-id="${id}" data-email="${escapeHtml(a.email || "")}" data-busy-key="clio-${id}">刷新Clio</button>
        <button class="ghost small" data-action="image-test" data-id="${id}" data-email="${escapeHtml(a.email || "")}" data-busy-key="image-${id}">测试出图</button>
        <a class="ghost small" href="/api/v1/registrar/accounts/${id}/cookie-export?download=true" target="_blank">导出Cookie</a>
        <button class="ghost small" data-action="copy" data-text="${escapeHtml((a.email || "") + "\n" + (a.password || ""))}">复制账号</button>
      </td>
    </tr>`;
  }).join("");
  renderButtonsLoading();
}
async function fetchCode(accountId) {
  const key = `code-${accountId}`; setLoading(key, true); print(`FETCH_CODE_START account=${accountId}`);
  try {
    const data = await api(`/api/v1/registrar/accounts/${encodeURIComponent(accountId)}/verification-code?wait_seconds=60`, { method: "POST" });
    const code = data.code || data.link || "";
    const box = document.getElementById(`code-${accountId}`);
    if (box) box.textContent = code || "未收到";
    print(`FETCH_CODE_DONE status=${data.status} code=${data.code || "-"} emails=${data.email_count}`);
    toast(code ? `验证码：${code}` : "暂未收到验证码");
    await loadAccounts();
  } catch (err) { print(`FETCH_CODE_FAIL ${err.message}`); toast(err.message); }
  finally { setLoading(key, false); }
}
async function runAccountImageTest(accountId, email) {
  const key = `image-${accountId}`;
  setLoading(key, true);
  print(`ACCOUNT_IMAGE_TEST_START account=${accountId} email=${email || "-"}`);
  try {
    const data = await api(`/api/v1/registrar/accounts/${encodeURIComponent(accountId)}/image-test`, {
      method: "POST",
      body: JSON.stringify({
        model: "firefly-nano-banana-1k-1x1",
        size: "1024x1024",
        prompt: `account acceptance test for ${email || accountId}: a small blue crystal cube on a clean white background`,
      }),
    });
    print(`ACCOUNT_IMAGE_TEST_DONE status=${data.status} http=${data.status_code} token=${data.token_id || "-"} url=${data.image_url || "-"} report=${data.report_path || "-"}`);
    if (data.error) print(`ACCOUNT_IMAGE_TEST_ERROR ${data.error}`);
    toast(data.status === "ok" ? "账号出图测试成功" : "账号出图测试失败");
    await loadAccounts();
  } catch (err) {
    print(`ACCOUNT_IMAGE_TEST_FAIL ${err.message}`);
    toast(err.message);
  } finally {
    setLoading(key, false);
  }
}
async function refreshAccountClio(accountId, email) {
  const key = `clio-${accountId}`;
  setLoading(key, true);
  print(`REFRESH_CLIO_START account=${accountId} email=${email || "-"}`);
  try {
    const data = await api(`/api/v1/registrar/accounts/${encodeURIComponent(accountId)}/refresh-clio-token`, { method: "POST" });
    const ts = data.token_summary || {};
    print(`REFRESH_CLIO_DONE status=${data.status} profile=${data.profile_id || "-"} active=${ts.active || 0} img=${ts.preferred_image_token_id || "-"} clio=${((ts.active_by_client_id || {})["clio-playground-web"] || []).join(",") || "-"}`);
    if (data.detail) print(`REFRESH_CLIO_DETAIL ${data.detail}`);
    toast(data.status === "ok" ? "Clio token 已刷新" : "Clio token 刷新失败");
    await loadAccounts();
  } catch (err) {
    print(`REFRESH_CLIO_FAIL ${err.message}`);
    toast(err.message);
  } finally {
    setLoading(key, false);
  }
}
async function runBatchImageTest() {
  const key = "batchImage";
  setLoading(key, true);
  print("BATCH_IMAGE_TEST_JOB_START only_failed=true limit=5");
  try {
    const data = await api("/api/v1/registrar/account-jobs", {
      method: "POST",
      body: JSON.stringify({
        action: "image-test-batch",
        model: "firefly-nano-banana-1k-1x1",
        size: "1024x1024",
        limit: 5,
        only_failed: true,
      }),
    });
    print(`BATCH_IMAGE_TEST_JOB id=${data.job_id}`);
    await followAccountJob(data.job_id, key, "批量出图测试");
  } catch (err) {
    print(`BATCH_IMAGE_TEST_FAIL ${err.message}`);
    toast(err.message);
    setLoading(key, false);
  }
}
async function followAccountJob(jobId, busyKey, label) {
  setState("running");
  let done = false;
  while (!done) {
    const job = await api(`/api/v1/registrar/account-jobs/${encodeURIComponent(jobId)}`);
    const total = job.total || 0;
    els.jobInfo.textContent = `${label} Job ${job.id} · ${job.current || 0}/${total} · success=${job.success_count || 0} failed=${job.failed_count || 0} skipped=${job.skipped_count || 0}`;
    if (Array.isArray(job.logs)) {
      els.logBox.textContent = job.logs.join("\n") || "等待日志...";
      els.logBox.scrollTop = els.logBox.scrollHeight;
    }
    setState(job.status || "running");
    done = job.status !== "running";
    if (!done) await new Promise(resolve => setTimeout(resolve, 1500));
    else {
      toast(`${label}完成：成功 ${job.success_count || 0}/${total}`);
      await loadAccounts();
      setLoading(busyKey, false);
    }
  }
}
async function refreshClioBatch() {
  const key = "batchClio";
  setLoading(key, true);
  print("REFRESH_CLIO_BATCH_JOB_START only_missing_clio=true limit=10");
  try {
    const data = await api("/api/v1/registrar/account-jobs", {
      method: "POST",
      body: JSON.stringify({ action: "refresh-clio-batch", limit: 10, only_missing_clio: true }),
    });
    print(`REFRESH_CLIO_BATCH_JOB id=${data.job_id}`);
    await followAccountJob(data.job_id, key, "批量刷新Clio");
  } catch (err) {
    print(`REFRESH_CLIO_BATCH_FAIL ${err.message}`);
    toast(err.message);
    setLoading(key, false);
  }
}
async function planBatchImageTest() {
  const key = "planImage";
  setLoading(key, true);
  print("BATCH_IMAGE_PLAN_START only_failed=true limit=20");
  try {
    const plan = await api("/api/v1/registrar/accounts/image-test-plan", {
      method: "POST",
      body: JSON.stringify({
        model: "firefly-nano-banana-1k-1x1",
        size: "1024x1024",
        limit: 20,
        only_failed: true,
      }),
    });
    print(`BATCH_IMAGE_PLAN_DONE selected=${plan.counters?.selected || 0} matched=${plan.counters?.matched_scope || 0} clio_ready=${plan.counters?.clio_ready || 0} projectx_only=${plan.counters?.projectx_only || 0} no_token=${plan.counters?.no_token || 0} passed_skipped=${plan.counters?.passed_skipped || 0}`);
    (plan.candidates || []).forEach(item => print(`PLAN_CANDIDATE email=${item.email} token=${item.selected_token_id} client=${item.selected_client_id} status=${item.image_status || "-"}`));
    (plan.skipped || []).slice(0, 8).forEach(item => print(`PLAN_SKIPPED email=${item.email} reason=${item.reason}`));
    toast(`预检完成：候选 ${plan.counters?.selected || 0}`);
  } catch (err) {
    print(`BATCH_IMAGE_PLAN_FAIL ${err.message}`);
    toast(err.message);
  } finally {
    setLoading(key, false);
  }
}
async function copyText(text) { await navigator.clipboard.writeText(text); toast("已复制"); }

function compactCounts(counts) {
  if (!counts || typeof counts !== "object") return "-";
  return Object.entries(counts).map(([k, v]) => `${k}:${v}`).join(", ") || "-";
}
async function diagnoseGptImage2() {
  const key = "gptDiag";
  setLoading(key, true);
  print("GPT_IMAGE_2_DIAG_START 汇总最近日志...");
  try {
    const before = await api("/api/v1/diagnostics/gpt-image-2?limit=100");
    print(`GPT_IMAGE_2_DIAG_SUMMARY conclusion=${before.conclusion}`);
    print(`GPT_IMAGE_2_DIAG_TOKENS projectx_active=${before.tokens?.projectx_active || 0} clio_active=${before.tokens?.clio_active || 0} proxy=${before.proxy || "off"}`);
    print(`GPT_IMAGE_2_DIAG_LOGS status_counts=${compactCounts(before.logs?.gpt_image_2?.status_counts)} latest_probe=${before.latest_probe?.path || "-"}`);
    print("GPT_IMAGE_2_PROBE_START 直接探测 Adobe 上游 submit...");
    const probe = await api("/api/v1/diagnostics/gpt-image-2/probe", {
      method: "POST",
      body: JSON.stringify({ size: "1024x1024", quality: "low", timeout_seconds: 180 }),
    });
    print(`GPT_IMAGE_2_PROBE_DONE status=${probe.status} returncode=${probe.returncode} counts=${compactCounts(probe.summary?.status_counts)} accepted=${probe.summary?.accepted_count || 0}/${probe.summary?.total || 0}`);
    if (probe.latest_probe?.path) print(`GPT_IMAGE_2_PROBE_FILE ${probe.latest_probe.path}`);
    const after = await api("/api/v1/diagnostics/gpt-image-2?limit=100");
    print(`GPT_IMAGE_2_FINAL conclusion=${after.conclusion}`);
    toast(after.conclusion || "诊断完成");
  } catch (err) {
    print(`GPT_IMAGE_2_DIAG_FAIL ${err.message}`);
    toast(err.message);
  } finally {
    setLoading(key, false);
  }
}

els.startBtn.addEventListener("click", () => startJob().catch(err => { setState("failed"); print(`START_FAIL ${err.message}`); toast(err.message); }));
els.refreshJobsBtn.addEventListener("click", () => loadHistory().catch(err => toast(err.message)));
els.refreshAccountsBtn.addEventListener("click", () => { print("REFRESH_ACCOUNTS"); loadAccounts().catch(err => toast(err.message)); });
els.diagGptBtn.addEventListener("click", () => diagnoseGptImage2());
els.planImageTestBtn.addEventListener("click", () => planBatchImageTest());
els.batchRefreshClioBtn.addEventListener("click", () => refreshClioBatch());
els.batchImageTestBtn.addEventListener("click", () => runBatchImageTest());
els.clearLogBtn.addEventListener("click", () => { els.logBox.textContent = ""; });
els.historyList.addEventListener("click", async event => { const btn = event.target.closest("[data-job]"); if (!btn) return; currentJobId = btn.dataset.job; localStorage.setItem("registrar:lastJobId", currentJobId); if (polling) clearInterval(polling); await pollJob(currentJobId); polling = setInterval(() => pollJob(currentJobId).catch(() => {}), 1500); });
els.accountRows.addEventListener("click", event => {
  const btn = event.target.closest("button");
  if (!btn) return;
  if (btn.dataset.action === "code") fetchCode(btn.dataset.id);
  if (btn.dataset.action === "refresh-clio") refreshAccountClio(btn.dataset.id, btn.dataset.email || "");
  if (btn.dataset.action === "image-test") runAccountImageTest(btn.dataset.id, btn.dataset.email || "");
  if (btn.dataset.action === "copy") copyText(btn.dataset.text || "");
});

loadHistory().catch(() => {});
loadAccounts().catch(err => print(`LOAD_ACCOUNTS_FAIL ${err.message}`));
if (currentJobId) { pollJob(currentJobId).then(() => { polling = setInterval(() => pollJob(currentJobId).catch(() => {}), 1500); }).catch(() => setState("ready")); } else { setState("ready"); }
