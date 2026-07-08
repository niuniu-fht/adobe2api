const storageKey = "adobe-pay-console:v1";

const packages = [
  { id: "A", name: "套餐A", code: "36513294", price: 0, days: 7 },
  { id: "B", name: "套餐B", code: "65BA7CA7", price: 0, days: 7 },
  { id: "C", name: "套餐C", code: "7164A328", price: 0, days: 14 },
];

const defaultState = {
  accounts: [],
  cards: [],
  records: [],
  logs: [],
  settings: {
    serviceApiKey: "",
    imageModel: "firefly-nano-banana-1k-1x1",
    registerCount: 4,
    concurrency: 2,
    registerUrl: "https://account.adobe.com/",
  },
  selectedAccounts: [],
  selectedPackages: ["A", "B", "C"],
};

let state = loadState();
let activeView = "dashboard";
let editContext = null;
let importContext = null;
let runningJob = null;

const els = {
  pageTitle: document.getElementById("pageTitle"),
  pageSubtitle: document.getElementById("pageSubtitle"),
  views: document.querySelectorAll(".view"),
  navItems: document.querySelectorAll(".nav-item"),
  metricAccounts: document.getElementById("metricAccounts"),
  metricEligible: document.getElementById("metricEligible"),
  metricCards: document.getElementById("metricCards"),
  metricOpened: document.getElementById("metricOpened"),
  liveLog: document.getElementById("liveLog"),
  modalLog: document.getElementById("modalLog"),
  accountRows: document.getElementById("accountRows"),
  adobeAccountRows: document.getElementById("adobeAccountRows"),
  cardRows: document.getElementById("cardRows"),
  recordRows: document.getElementById("recordRows"),
  accountCountLabel: document.getElementById("accountCountLabel"),
  selectAllAccounts: document.getElementById("selectAllAccounts"),
  selectAllAdobeAccounts: document.getElementById("selectAllAdobeAccounts"),
  trialModal: document.getElementById("trialModal"),
  editModal: document.getElementById("editModal"),
  editTitle: document.getElementById("editTitle"),
  editForm: document.getElementById("editForm"),
  deleteEditBtn: document.getElementById("deleteEditBtn"),
  importModal: document.getElementById("importModal"),
  importTitle: document.getElementById("importTitle"),
  importHelp: document.getElementById("importHelp"),
  importText: document.getElementById("importText"),
  packageGrid: document.getElementById("packageGrid"),
  cardSelect: document.getElementById("cardSelect"),
  accountSource: document.getElementById("accountSource"),
  willOpenCount: document.getElementById("willOpenCount"),
  openMode: document.getElementById("openMode"),
  startTrialBtn: document.getElementById("startTrialBtn"),
  stopJobBtn: document.getElementById("stopJobBtn"),
  toast: document.getElementById("toast"),
  serviceApiKey: document.getElementById("serviceApiKey"),
  imageModel: document.getElementById("imageModel"),
  registerCount: document.getElementById("registerCount"),
  concurrency: document.getElementById("concurrency"),
  registerUrlInput: document.getElementById("registerUrlInput"),
  registerUrlSetting: document.getElementById("registerUrlSetting"),
  scriptOutput: document.getElementById("scriptOutput"),
  cloakRegisterBtn: document.getElementById("cloakRegisterBtn"),
};

const titles = {
  dashboard: ["ADOBE 管理", "批量创建账号、测试出图、查询试用资格并记录开通结果。"],
  accounts: ["账号管理", "管理待开通账号、资格状态和图片测试结果。"],
  adobe: ["ADOBE管理", "按截图风格集中执行注册、出图测试和套餐开通任务。"],
  cards: ["卡片管理", "保存卡片别名、地区、余额和可用状态。"],
  records: ["开通记录", "查看每次套餐开通任务的结果和失败原因。"],
  settings: ["系统设置", "配置图片测试使用的本项目 API Key 和任务默认参数。"],
};

function loadState() {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return structuredClone(defaultState);
    const parsed = JSON.parse(raw);
    return {
      ...structuredClone(defaultState),
      ...parsed,
      settings: { ...defaultState.settings, ...(parsed.settings || {}) },
      selectedAccounts: Array.isArray(parsed.selectedAccounts) ? parsed.selectedAccounts : [],
      selectedPackages: Array.isArray(parsed.selectedPackages) ? parsed.selectedPackages : ["A", "B", "C"],
    };
  } catch (err) {
    return structuredClone(defaultState);
  }
}

function saveState() {
  localStorage.setItem(storageKey, JSON.stringify(state));
}

async function apiRequest(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (!res.ok) {
    const detail = data?.detail || data?.message || `HTTP ${res.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data || {};
}

function normalizeAccount(raw) {
  const item = raw || {};
  return {
    id: String(item.id || id("acct")),
    email: String(item.email || item.username || "").trim(),
    password: String(item.password || item.pass || "").trim(),
    status: String(item.status || "registered"),
    eligibility: String(item.eligibility || "unknown"),
    plan: String(item.plan || "-"),
    imageStatus: String(item.imageStatus || item.image_status || "untested"),
    ip: String(item.ip || item.proxy_ip || randomIp()),
    createdAt: String(item.createdAt || item.created_at || nowText()),
    lastAction: String(item.lastAction || item.last_action || "-"),
    emailProvider: String(item.emailProvider || item.email_provider || "local"),
    mailStatus: String(item.mailStatus || item.mail_status || ""),
    mailToken: String(item.mailToken || item.mail_token || ""),
    verificationCode: String(item.verificationCode || item.verification_code || ""),
    verificationLink: String(item.verificationLink || item.verification_link || ""),
    sessionStatePath: String(item.sessionStatePath || item.session_state_path || ""),
    cookieProfileId: String(item.cookieProfileId || item.cookie_profile_id || ""),
    tokenStatus: String(item.tokenStatus || item.token_status || ""),
    imageTestUrl: String(item.imageTestUrl || item.image_test_url || ""),
    imageTestError: String(item.imageTestError || item.image_test_error || ""),
  };
}

function accountPatch(account) {
  return {
    email: account.email,
    password: account.password,
    status: account.status,
    eligibility: account.eligibility,
    plan: account.plan,
    image_status: account.imageStatus,
    ip: account.ip,
    last_action: account.lastAction,
    email_provider: account.emailProvider,
    mail_token: account.mailToken,
    mail_status: account.mailStatus,
    verification_code: account.verificationCode,
    verification_link: account.verificationLink,
    session_state_path: account.sessionStatePath,
    cookie_profile_id: account.cookieProfileId,
    token_status: account.tokenStatus,
    image_test_url: account.imageTestUrl,
    image_test_error: account.imageTestError,
  };
}

function mergeAccounts(incoming) {
  const seen = new Set();
  const merged = [];
  [...incoming, ...state.accounts].forEach((item) => {
    const account = normalizeAccount(item);
    if (!account.id || seen.has(account.id)) return;
    seen.add(account.id);
    merged.push(account);
  });
  state.accounts = merged;
  state.selectedAccounts.push(...incoming.map((item) => normalizeAccount(item).id));
  dedupeSelection();
}

async function loadAccountsFromServer() {
  try {
    const data = await apiRequest("/api/v1/adobe/accounts");
    const accounts = Array.isArray(data.accounts) ? data.accounts.map(normalizeAccount) : [];
    state.accounts = accounts;
    if (Array.isArray(data.logs)) state.logs = data.logs;
    if (!state.selectedAccounts.length) {
      state.selectedAccounts = accounts.map((account) => account.id);
    }
    dedupeSelection();
    saveState();
  } catch (err) {
    log(`LOAD_ACCOUNTS_FAIL ${err.message}`, "live");
  }
}

async function persistAccount(account) {
  if (!account?.id) return;
  try {
    await apiRequest(`/api/v1/adobe/accounts/${encodeURIComponent(account.id)}`, {
      method: "PUT",
      body: JSON.stringify(accountPatch(account)),
    });
  } catch (err) {
    log(`SYNC_ACCOUNT_FAIL ${account.email} ${err.message}`, "live");
  }
}

function id(prefix) {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function nowText() {
  return new Date().toLocaleString("zh-CN", { hour12: false });
}

function timeOnly() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message, isBad = false) {
  els.toast.textContent = message;
  els.toast.className = `toast show${isBad ? " bad" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    els.toast.className = "toast";
  }, 2600);
}

function log(message, target = "both") {
  const line = `[${timeOnly()}] ${message}`;
  state.logs.push(line);
  state.logs = state.logs.slice(-220);
  saveState();
  if (target === "both" || target === "live") {
    els.liveLog.textContent = state.logs.join("\n");
    els.liveLog.scrollTop = els.liveLog.scrollHeight;
  }
  if (target === "both" || target === "modal") {
    els.modalLog.textContent = `${els.modalLog.textContent}${els.modalLog.textContent ? "\n" : ""}${line}`;
    els.modalLog.scrollTop = els.modalLog.scrollHeight;
  }
}

function appendScriptOutput(message, reset = false) {
  if (!els.scriptOutput) return;
  const line = `[${timeOnly()}] ${message}`;
  els.scriptOutput.textContent = reset
    ? line
    : `${els.scriptOutput.textContent}${els.scriptOutput.textContent ? "\n" : ""}${line}`;
  els.scriptOutput.scrollTop = els.scriptOutput.scrollHeight;
}

function appendScriptLines(raw, label = "STDOUT") {
  if (!raw) return;
  String(raw)
    .trim()
    .split(/\r?\n/)
    .slice(-160)
    .forEach((line) => {
      let text = line;
      try {
        const item = JSON.parse(line);
        text = JSON.stringify(item);
      } catch (err) {
        text = line;
      }
      appendScriptOutput(`${label} ${text}`);
      log(`SCRIPT ${text}`, "live");
    });
}

function seedData() {
  if (!state.cards.length) {
    state.cards = [
      {
        id: id("card"),
        label: "US Visa 4242",
        bin: "424242",
        last4: "4242",
        region: "US",
        type: "Visa",
        balance: 48.8,
        successRate: 92,
        status: "active",
      },
      {
        id: id("card"),
        label: "HK Master 5454",
        bin: "545454",
        last4: "5454",
        region: "HK",
        type: "Mastercard",
        balance: 16.3,
        successRate: 74,
        status: "active",
      },
    ];
  }
  if (!state.accounts.length) {
    createAccounts(4, false);
    state.accounts.forEach((account, index) => {
      account.eligibility = index === 3 ? "blocked" : "eligible";
      account.status = index === 3 ? "risk" : "ready";
      account.lastAction = "示例导入";
    });
  }
  saveState();
  renderAll();
  toast("示例数据已填充");
}

function createAccounts(count, shouldLog = true) {
  const safeCount = Math.max(1, Math.min(50, Number(count) || 1));
  const domains = ["mailbox.local", "trial.local", "studio.local"];
  const start = state.accounts.length + 1;
  for (let i = 0; i < safeCount; i += 1) {
    const seq = start + i;
    const account = {
      id: id("acct"),
      email: `adobe_user_${String(seq).padStart(3, "0")}@${domains[seq % domains.length]}`,
      password: `Aa${Math.random().toString(36).slice(2, 9)}!${seq}`,
      status: "registered",
      eligibility: "unknown",
      plan: "-",
      imageStatus: "untested",
      ip: randomIp(),
      createdAt: nowText(),
      lastAction: "一键注册",
    };
    state.accounts.unshift(account);
    state.selectedAccounts.push(account.id);
    if (shouldLog) log(`CREATE_ACCOUNT ${account.email} ip=${account.ip}`);
  }
  dedupeSelection();
  saveState();
}

async function registerAccounts(count) {
  const safeCount = Math.max(1, Math.min(100, Number(count) || 1));
  try {
    log(`REGISTER_START count=${safeCount}`, "live");
    const data = await apiRequest("/api/v1/adobe/register", {
      method: "POST",
      body: JSON.stringify({
        count: safeCount,
        domain: "trial.local",
        email_prefix: "adobe_user",
      }),
    });
    const accounts = Array.isArray(data.accounts) ? data.accounts.map(normalizeAccount) : [];
    mergeAccounts(accounts);
    if (Array.isArray(data.logs)) state.logs = data.logs;
    saveState();
    renderAll();
    toast(`注册任务已完成：${accounts.length} 个账号`);
    return accounts;
  } catch (err) {
    log(`REGISTER_FAIL ${err.message}`, "live");
    toast(err.message || "注册任务失败", true);
    return [];
  }
}

async function fetchMailbox(account) {
  if (!account?.id) return;
  try {
    log(`TEMPMAIL_FETCH_START ${account.email}`, "live");
    const data = await apiRequest(`/api/v1/adobe/accounts/${encodeURIComponent(account.id)}/emails`);
    const updated = normalizeAccount(data.account || account);
    const index = state.accounts.findIndex((item) => item.id === updated.id);
    if (index >= 0) state.accounts[index] = updated;
    const count = Array.isArray(data.emails) ? data.emails.length : 0;
    log(`TEMPMAIL_FETCH_OK ${updated.email} count=${count} status=${updated.mailStatus || "-"}`, "live");
    if (updated.verificationCode || updated.verificationLink) {
      log(`TEMPMAIL_VERIFY_FOUND ${updated.email}`, "live");
    }
    saveState();
    renderAll();
    toast(`收件箱已刷新：${count} 封邮件`);
  } catch (err) {
    log(`TEMPMAIL_FETCH_FAIL ${account.email} ${err.message}`, "live");
    toast(err.message || "收件箱刷新失败", true);
  }
}

function normalizedRegisterUrl() {
  const raw = String(
    (els.registerUrlInput && els.registerUrlInput.value) ||
      state.settings.registerUrl ||
      defaultState.settings.registerUrl
  ).trim();
  let url = raw || defaultState.settings.registerUrl;
  if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
  state.settings.registerUrl = url;
  if (els.registerUrlInput) els.registerUrlInput.value = url;
  if (els.registerUrlSetting) els.registerUrlSetting.value = url;
  saveState();
  return url;
}

function openRegisterUrl(account = null) {
  const url = normalizedRegisterUrl();
  const suffix = account ? ` account=${account.email}` : "";
  log(`OPEN_REGISTER_URL ${url}${suffix}`, "live");
  window.open(url, "_blank", "noopener,noreferrer");
}

async function realRegisterTest() {
  const accounts = await registerAccounts(1);
  const account = accounts[0];
  if (!account) return;
  const credentialText = `email=${account.email}\npassword=${account.password}`;
  try {
    await navigator.clipboard.writeText(credentialText);
    log(`REGISTER_CREDENTIALS_COPIED ${account.email}`, "live");
  } catch (err) {
    log(`REGISTER_CREDENTIALS_READY ${credentialText.replace(/\n/g, " ")}`, "live");
  }
  openRegisterUrl(account);
}

async function cloakRegister() {
  if (els.cloakRegisterBtn?.disabled) return;
  try {
    if (els.cloakRegisterBtn) {
      els.cloakRegisterBtn.disabled = true;
      els.cloakRegisterBtn.textContent = "脚本执行中…";
    }
    log("CLOAK_REGISTER_START proxy=project-config", "live");
    appendScriptOutput("RUN tools/cloak_adobe_register.py -- auto mode", true);
    toast("CloakBrowser 正在走代理自动注册，请等待浏览器流程完成…");
    const start = await apiRequest("/api/v1/adobe/register/cloak/job", {
      method: "POST",
      body: JSON.stringify({}),
    });
    appendScriptOutput(`JOB ${start.job_id} started used_proxy=${start.used_proxy}`);
    let data = start;
    let seenStdout = 0;
    let seenStderr = 0;
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, 1800));
      const job = await apiRequest(`/api/v1/adobe/register/cloak/jobs/${encodeURIComponent(start.job_id)}`);
      const stdoutLines = Array.isArray(job.stdout_lines) ? job.stdout_lines : [];
      const stderrLines = Array.isArray(job.stderr_lines) ? job.stderr_lines : [];
      stdoutLines.slice(seenStdout).forEach((line) => appendScriptOutput(`STDOUT ${line}`));
      stderrLines.slice(seenStderr).forEach((line) => appendScriptOutput(`STDERR ${line}`));
      seenStdout = stdoutLines.length;
      seenStderr = stderrLines.length;
      if (job.status !== "running") {
        data = {
          ...job,
          status: job.status === "succeeded" ? "ok" : "failed",
          stdout: stdoutLines.join("\n"),
          stderr: stderrLines.join("\n"),
        };
        break;
      }
    }
    const result = data.result || {};
    const account = result.account || {};
    const status = account.status || data.status || "unknown";
    log(
      `CLOAK_REGISTER_DONE status=${status} used_proxy=${data.used_proxy} email=${account.email || ""}`,
      "live"
    );
    appendScriptOutput(
      `DONE status=${status} used_proxy=${data.used_proxy} image=${account.imageTestUrl || result.image_test?.image_url || ""}`
    );
    if (data.status === "failed") {
      appendScriptOutput(`RETURN_CODE ${data.returncode ?? ""} ${data.error || ""}`);
    }
    await loadAccountsFromServer();
    setView("adobe");
    toast(status === "registered" ? "CloakBrowser 注册成功" : `CloakBrowser 流程结束：${status}`);
  } catch (err) {
    log(`CLOAK_REGISTER_FAIL ${err.message}`, "live");
    appendScriptOutput(`FAIL ${err.message}`);
    try {
      const detail = JSON.parse(err.message);
      appendScriptLines(detail.stdout || detail.detail?.stdout, "STDOUT");
      appendScriptLines(detail.stderr || detail.detail?.stderr, "STDERR");
    } catch (parseErr) {
      // ignore non-JSON error text
    }
    toast(err.message || "CloakBrowser 自动注册失败", true);
  } finally {
    if (els.cloakRegisterBtn) {
      els.cloakRegisterBtn.disabled = false;
      els.cloakRegisterBtn.textContent = "CloakBrowser 代理自动注册";
    }
  }
}

function randomIp() {
  const pools = [
    [49, 43, 160],
    [23, 105, 77],
    [104, 28, 31],
    [185, 199, 108],
  ];
  const pool = pools[Math.floor(Math.random() * pools.length)];
  return `${pool[0]}.${pool[1]}.${pool[2]}.${Math.floor(Math.random() * 180) + 20}`;
}

function dedupeSelection() {
  const valid = new Set(state.accounts.map((a) => a.id));
  state.selectedAccounts = Array.from(new Set(state.selectedAccounts)).filter((item) => valid.has(item));
}

function statusBadge(value, kind = "info") {
  return `<span class="status ${kind}">${escapeHtml(value)}</span>`;
}

function accountStatusLabel(account) {
  const map = {
    registered: ["已注册", "info"],
    ready: ["待开通", "ok"],
    risk: ["风控", "bad"],
    opened: ["已开通", "ok"],
    failed: ["失败", "bad"],
  };
  const item = map[account.status] || ["未知", "wait"];
  return statusBadge(item[0], item[1]);
}

function eligibilityLabel(value) {
  const map = {
    unknown: ["未查询", "wait"],
    eligible: ["可试用", "ok"],
    blocked: ["不可试用", "bad"],
    checking: ["查询中", "info"],
  };
  const item = map[value] || ["未查询", "wait"];
  return statusBadge(item[0], item[1]);
}

function imageLabel(value) {
  const map = {
    untested: ["未测试", "wait"],
    testing: ["测试中", "info"],
    ok: ["可出图", "ok"],
    fail: ["失败", "bad"],
  };
  const item = map[value] || ["未测试", "wait"];
  return statusBadge(item[0], item[1]);
}

function renderAll() {
  state.accounts = state.accounts.map(normalizeAccount);
  dedupeSelection();
  renderMetrics();
  renderLogs();
  renderAccounts();
  renderCards();
  renderRecords();
  renderPackages();
  renderSettings();
  updateWillOpenCount();
  saveState();
}

function renderMetrics() {
  els.metricAccounts.textContent = String(state.accounts.length);
  els.metricEligible.textContent = String(state.accounts.filter((a) => a.eligibility === "eligible").length);
  els.metricCards.textContent = String(state.cards.length);
  els.metricOpened.textContent = String(state.records.filter((r) => r.status === "success").length);
  els.accountCountLabel.textContent = `共 ${state.accounts.length} 个账号`;
}

function renderLogs() {
  els.liveLog.textContent = state.logs.join("\n") || `[${timeOnly()}] READY 等待任务`;
  els.liveLog.scrollTop = els.liveLog.scrollHeight;
}

function renderAccounts() {
  const rows = state.accounts.map((account) => {
    const checked = state.selectedAccounts.includes(account.id) ? "checked" : "";
    return `
      <tr>
        <td><input class="account-check" type="checkbox" data-id="${account.id}" ${checked} /></td>
        <td>
          <strong>${escapeHtml(account.email)}</strong><br />
          <span class="hint mono">${escapeHtml(account.createdAt)}</span><br />
          <span class="hint mono">${escapeHtml(account.emailProvider || "local")}${account.mailStatus ? ` · ${escapeHtml(account.mailStatus)}` : ""}${account.tokenStatus ? ` · token:${escapeHtml(account.tokenStatus)}` : ""}</span>
        </td>
        <td>${accountStatusLabel(account)}</td>
        <td>${eligibilityLabel(account.eligibility)}</td>
        <td>${escapeHtml(account.plan || "-")}</td>
        <td>${imageLabel(account.imageStatus)}</td>
        <td class="mono">${escapeHtml(account.ip)}</td>
        <td>
          <div class="row-actions">
            <button class="text-btn" type="button" data-action="query" data-id="${account.id}">查积分</button>
            <button class="text-btn" type="button" data-action="test" data-id="${account.id}">测试出图</button>
            ${account.emailProvider === "tempmail_lol" ? `<button class="text-btn" type="button" data-action="mailbox" data-id="${account.id}">收信</button>` : ""}
            <button class="text-btn" type="button" data-action="edit-account" data-id="${account.id}">编辑</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");

  const adobeRows = state.accounts.map((account) => {
    const checked = state.selectedAccounts.includes(account.id) ? "checked" : "";
    return `
      <tr>
        <td><input class="account-check" type="checkbox" data-id="${account.id}" ${checked} /></td>
        <td><strong>${escapeHtml(account.email)}</strong><br /><span class="hint mono">${escapeHtml(account.ip)}</span><br /><span class="hint mono">${escapeHtml(account.mailStatus || account.emailProvider || "-")}${account.tokenStatus ? ` · token:${escapeHtml(account.tokenStatus)}` : ""}</span></td>
        <td>${eligibilityLabel(account.eligibility)}</td>
        <td>${accountStatusLabel(account)}</td>
        <td>${imageLabel(account.imageStatus)}</td>
        <td>${escapeHtml(account.lastAction || "-")}</td>
      </tr>
    `;
  }).join("");

  els.accountRows.innerHTML = rows || `<tr><td class="empty" colspan="8">暂无账号，点击“一键注册”生成账号资料。</td></tr>`;
  els.adobeAccountRows.innerHTML = adobeRows || `<tr><td class="empty" colspan="6">暂无账号，点击“一键注册”生成账号资料。</td></tr>`;
  const allSelected = state.accounts.length > 0 && state.selectedAccounts.length === state.accounts.length;
  els.selectAllAccounts.checked = allSelected;
  els.selectAllAdobeAccounts.checked = allSelected;
}

function renderCards() {
  els.cardSelect.innerHTML = state.cards.map((card) => (
    `<option value="${card.id}">${escapeHtml(card.label)} · ${escapeHtml(card.region)} · ****${escapeHtml(card.last4)}</option>`
  )).join("");

  els.cardRows.innerHTML = state.cards.map((card) => `
    <tr>
      <td>
        <strong>${escapeHtml(card.label)}</strong><br />
        <span class="hint mono">BIN ${escapeHtml(card.bin)} · ****${escapeHtml(card.last4)}</span>
      </td>
      <td>${escapeHtml(card.region)}</td>
      <td>${escapeHtml(card.type)}</td>
      <td class="mono">$${Number(card.balance || 0).toFixed(2)}</td>
      <td>${Number(card.successRate || 0)}%</td>
      <td>${statusBadge(card.status === "active" ? "可用" : "停用", card.status === "active" ? "ok" : "bad")}</td>
      <td>
        <div class="row-actions">
          <button class="text-btn" type="button" data-action="edit-card" data-id="${card.id}">编辑</button>
          <button class="text-btn danger" type="button" data-action="delete-card" data-id="${card.id}">删除</button>
        </div>
      </td>
    </tr>
  `).join("") || `<tr><td class="empty" colspan="7">暂无卡片，请添加卡片别名。</td></tr>`;
}

function renderRecords() {
  els.recordRows.innerHTML = state.records.map((record) => `
    <tr>
      <td class="mono">${escapeHtml(record.time)}</td>
      <td>${escapeHtml(record.email)}</td>
      <td>${escapeHtml(record.packageName)}</td>
      <td>${escapeHtml(record.cardLabel)}</td>
      <td>${statusBadge(record.status === "success" ? "成功" : "失败", record.status === "success" ? "ok" : "bad")}</td>
      <td>${escapeHtml(record.message)}</td>
    </tr>
  `).join("") || `<tr><td class="empty" colspan="6">暂无开通记录。</td></tr>`;
}

function renderPackages() {
  els.packageGrid.innerHTML = packages.map((pkg) => {
    const checked = state.selectedPackages.includes(pkg.id) ? "checked" : "";
    return `
      <label class="package-card">
        <input class="package-check" type="checkbox" value="${pkg.id}" ${checked} />
        ${pkg.name} (${pkg.code})
      </label>
    `;
  }).join("");
}

function renderSettings() {
  els.serviceApiKey.value = state.settings.serviceApiKey || "";
  els.imageModel.value = state.settings.imageModel || defaultState.settings.imageModel;
  els.registerCount.value = state.settings.registerCount || 4;
  els.concurrency.value = state.settings.concurrency || 2;
  const registerUrl = state.settings.registerUrl || defaultState.settings.registerUrl;
  if (els.registerUrlInput) els.registerUrlInput.value = registerUrl;
  if (els.registerUrlSetting) els.registerUrlSetting.value = registerUrl;
}

function setView(view) {
  activeView = view;
  els.navItems.forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  els.views.forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  const [title, subtitle] = titles[view] || titles.dashboard;
  els.pageTitle.textContent = title;
  els.pageSubtitle.textContent = subtitle;
}

function selectedAccountObjects() {
  const ids = new Set(state.selectedAccounts);
  return state.accounts.filter((account) => ids.has(account.id));
}

function accountsForSource() {
  const source = els.accountSource.value;
  if (source === "all") return state.accounts;
  if (source === "eligible") return state.accounts.filter((account) => account.eligibility === "eligible");
  return selectedAccountObjects();
}

function updateWillOpenCount() {
  if (!els.willOpenCount) return;
  els.willOpenCount.textContent = `将开通 ${accountsForSource().length} 个账号`;
}

function queryEligibility(accounts = selectedAccountObjects()) {
  if (!accounts.length) {
    toast("请先选择账号", true);
    return;
  }
  accounts.forEach((account, index) => {
    account.eligibility = "checking";
    account.lastAction = "查询试用资格";
    setTimeout(() => {
      const score = (account.email.length + account.ip.length + index) % 10;
      account.eligibility = score >= 2 ? "eligible" : "blocked";
      account.status = account.eligibility === "eligible" ? "ready" : "risk";
      account.lastAction = account.eligibility === "eligible" ? "资格可用" : "资格不可用";
      log(`CHECK_TRIAL ${account.email} result=${account.eligibility} ip=${account.ip}`, "live");
      persistAccount(account);
      renderAll();
    }, 380 + index * 160);
  });
  saveState();
  renderAll();
  log(`QUERY_ELIGIBILITY queued=${accounts.length}`, "live");
}

async function testImages(accounts = selectedAccountObjects()) {
  if (!accounts.length) {
    toast("请先选择账号", true);
    return;
  }
  const apiKey = String(state.settings.serviceApiKey || "").trim();
  accounts.forEach((account) => {
    account.imageStatus = "testing";
    account.lastAction = "图片测试中";
  });
  renderAll();
  log(`IMAGE_TEST queued=${accounts.length} model=${state.settings.imageModel}`, "live");

  for (const account of accounts) {
    if (apiKey) {
      try {
        const res = await fetch("/v1/images/generations", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${apiKey}`,
          },
          body: JSON.stringify({
            model: state.settings.imageModel,
            prompt: `simple product photo test for ${account.email}`,
            n: 1,
            size: "1024x1024",
          }),
        });
        if (!res.ok) {
          const detail = await res.text();
          throw new Error(`HTTP ${res.status} ${detail.slice(0, 120)}`);
        }
        account.imageStatus = "ok";
        account.lastAction = "图片测试成功";
        log(`IMAGE_TEST_OK ${account.email}`, "live");
      } catch (err) {
        account.imageStatus = "fail";
        account.lastAction = "图片测试失败";
        log(`IMAGE_TEST_FAIL ${account.email} ${err.message}`, "live");
      }
    } else {
      await sleep(450);
      account.imageStatus = account.eligibility === "blocked" ? "fail" : "ok";
      account.lastAction = account.imageStatus === "ok" ? "图片测试成功" : "图片测试失败";
      log(`${account.imageStatus === "ok" ? "IMAGE_TEST_OK" : "IMAGE_TEST_FAIL"} ${account.email} mode=local`, "live");
    }
    await persistAccount(account);
    saveState();
    renderAll();
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startTrialJob() {
  const accounts = accountsForSource();
  const card = state.cards.find((item) => item.id === els.cardSelect.value);
  const selectedPackages = packages.filter((pkg) => state.selectedPackages.includes(pkg.id));

  if (!accounts.length) {
    toast("没有可开通账号", true);
    return;
  }
  if (!card) {
    toast("请先添加可用卡片", true);
    return;
  }
  if (!selectedPackages.length) {
    toast("至少选择一个套餐", true);
    return;
  }

  runningJob = { stopped: false };
  els.startTrialBtn.disabled = true;
  els.stopJobBtn.disabled = false;
  els.modalLog.textContent = "";
  log(`CREATE_ORDER queued accounts=${accounts.length} card=${card.label}`, "modal");

  for (const account of accounts) {
    if (runningJob.stopped) {
      log("TASK_STOPPED 用户停止任务", "modal");
      break;
    }

    for (const pkg of selectedPackages) {
      if (runningJob.stopped) break;
      await runOpenStep(account, card, pkg);
      saveState();
      renderAll();
    }
  }

  els.startTrialBtn.disabled = false;
  els.stopJobBtn.disabled = true;
  runningJob = null;
  log("TASK_FINISHED 批量任务结束", "modal");
}

async function runOpenStep(account, card, pkg) {
  log(`CREATE_ORDER HTTP 200 account=${account.email} package=${pkg.code}`, "modal");
  await sleep(350);
  if (account.eligibility === "unknown") {
    log(`Step1 CHECK_TRIAL ${account.email}`, "modal");
    account.eligibility = "eligible";
  }
  if (account.eligibility !== "eligible") {
    recordOpen(account, card, pkg, "failed", "试用资格不可用");
    account.status = "risk";
    account.lastAction = "开通失败";
    await persistAccount(account);
    log(`TRIAL_BLOCKED ${account.email}`, "modal");
    return;
  }

  log("Step2 UPDATE_ORDER 补 billingContract + cartIdentity ...", "modal");
  await sleep(320);
  log("UPDATE_ORDER HTTP 200", "modal");
  await sleep(240);
  log(`Step3 Tokenize 卡片 alias=${card.label} region=${card.region}`, "modal");
  await sleep(360);

  const successChance = Math.max(0, Math.min(100, Number(card.successRate || 0)));
  const ok = Math.random() * 100 <= successChance && Number(card.balance || 0) >= Number(pkg.price || 0);
  if (ok) {
    account.status = "opened";
    account.plan = `${pkg.name} ${pkg.days}天`;
    account.lastAction = "试用开通成功";
    card.balance = Math.max(0, Number(card.balance || 0) - Number(pkg.price || 0));
    recordOpen(account, card, pkg, "success", "试用资格已开通");
    await persistAccount(account);
    log(`CREATE_CANDIDATE_PAYMENT_INSTRUMENT OK account=${account.email}`, "modal");
  } else {
    account.status = "failed";
    account.lastAction = "支付确认失败";
    recordOpen(account, card, pkg, "failed", "卡片成功率或余额不足");
    await persistAccount(account);
    log(`PAYMENT_FAILED account=${account.email} card=${card.label}`, "modal");
  }
}

function recordOpen(account, card, pkg, status, message) {
  state.records.unshift({
    id: id("rec"),
    time: nowText(),
    email: account.email,
    packageName: `${pkg.name} (${pkg.code})`,
    cardLabel: `${card.label} ****${card.last4}`,
    status,
    message,
  });
  state.records = state.records.slice(0, 500);
}

function openTrialModal() {
  renderPackages();
  renderCards();
  updateWillOpenCount();
  els.modalLog.textContent = "";
  els.trialModal.classList.add("open");
  els.trialModal.setAttribute("aria-hidden", "false");
}

function closeTrialModal() {
  els.trialModal.classList.remove("open");
  els.trialModal.setAttribute("aria-hidden", "true");
}

function openEdit(type, item = null) {
  editContext = { type, id: item?.id || null };
  els.editTitle.textContent = type === "card" ? (item ? "编辑卡片" : "添加卡片") : (item ? "编辑账号" : "新增账号");
  els.deleteEditBtn.style.display = item ? "inline-flex" : "none";
  if (type === "card") {
    els.editForm.innerHTML = `
      ${field("label", "卡片别名", item?.label || "")}
      ${field("bin", "BIN", item?.bin || "")}
      ${field("last4", "尾号", item?.last4 || "")}
      ${field("region", "地区", item?.region || "US")}
      ${field("type", "类型", item?.type || "Visa")}
      ${field("balance", "余额", item?.balance ?? "20")}
      ${field("successRate", "成功率", item?.successRate ?? "88")}
    `;
  } else {
    els.editForm.innerHTML = `
      ${field("email", "账号", item?.email || "")}
      ${field("password", "密码", item?.password || "")}
      ${field("ip", "IP", item?.ip || randomIp())}
    `;
  }
  els.editModal.classList.add("open");
  els.editModal.setAttribute("aria-hidden", "false");
}

function field(name, label, value) {
  return `<label><span>${label}</span><input name="${name}" value="${escapeHtml(value)}" /></label>`;
}

function openImport(type) {
  importContext = type;
  els.importTitle.textContent = type === "cards" ? "批量导入卡片" : "批量导入账号";
  els.importHelp.textContent = type === "cards"
    ? "支持 JSON 数组，或每行一张卡：别名,BIN,尾号,地区,类型,余额,成功率。"
    : "支持 JSON 数组，或每行一个账号：邮箱,密码,IP。导入后会自动加入勾选列表。";
  els.importText.value = "";
  els.importModal.classList.add("open");
  els.importModal.setAttribute("aria-hidden", "false");
}

function closeImportModal() {
  els.importModal.classList.remove("open");
  els.importModal.setAttribute("aria-hidden", "true");
  importContext = null;
}

function fillImportExample() {
  if (importContext === "cards") {
    els.importText.value = [
      "US Visa 4242,424242,4242,US,Visa,32.5,91",
      "SG Master 5454,545454,5454,SG,Mastercard,18.8,76",
    ].join("\n");
    return;
  }
  els.importText.value = [
    "user_one@trial.local,Aa123456!,49.43.160.232",
    "user_two@trial.local,Aa789012!,23.105.77.41",
  ].join("\n");
}

function parseImportRows(raw) {
  const text = String(raw || "").trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && Array.isArray(parsed.items)) return parsed.items;
    if (parsed && Array.isArray(parsed.accounts)) return parsed.accounts;
    if (parsed && Array.isArray(parsed.cards)) return parsed.cards;
  } catch (err) {
    // Fallback to CSV-like rows below.
  }
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split(",").map((part) => part.trim()));
}

async function saveImport() {
  const rows = parseImportRows(els.importText.value);
  if (!rows.length) {
    toast("没有可导入的数据", true);
    return;
  }

  if (importContext === "cards") {
    const cards = rows.map((row) => {
      const data = Array.isArray(row)
        ? {
            label: row[0],
            bin: row[1],
            last4: row[2],
            region: row[3],
            type: row[4],
            balance: row[5],
            successRate: row[6],
          }
        : row;
      return {
        id: id("card"),
        label: String(data.label || data.name || "Imported Card").trim(),
        bin: String(data.bin || "000000").trim().slice(0, 8),
        last4: String(data.last4 || data.last || "0000").trim().slice(-4),
        region: String(data.region || data.country || "US").trim().toUpperCase(),
        type: String(data.type || data.brand || "Visa").trim(),
        balance: Number(data.balance ?? data.amount ?? 0),
        successRate: Math.max(0, Math.min(100, Number(data.successRate ?? data.success_rate ?? 80))),
        status: "active",
      };
    });
    state.cards.unshift(...cards);
    log(`IMPORT_CARDS count=${cards.length}`, "live");
    toast(`已导入 ${cards.length} 张卡片`);
  } else {
    const accounts = rows.map((row) => {
      const data = Array.isArray(row)
        ? { email: row[0], password: row[1], ip: row[2] }
        : row;
      return {
        id: id("acct"),
        email: String(data.email || data.username || data.account || "").trim() || `import_${Date.now()}@trial.local`,
        password: String(data.password || data.pass || "").trim() || `Aa${Math.random().toString(36).slice(2, 10)}!`,
        ip: String(data.ip || data.proxy_ip || randomIp()).trim(),
        status: "registered",
        eligibility: "unknown",
        plan: "-",
        imageStatus: "untested",
        createdAt: nowText(),
        lastAction: "批量导入",
      };
    });
    try {
      const data = await apiRequest("/api/v1/adobe/accounts/import", {
        method: "POST",
        body: JSON.stringify({ accounts }),
      });
      const imported = Array.isArray(data.accounts) ? data.accounts.map(normalizeAccount) : [];
      mergeAccounts(imported);
      log(`IMPORT_ACCOUNTS count=${imported.length} skipped=${Number(data.skipped_count || 0)}`, "live");
      toast(`已导入 ${imported.length} 个账号`);
    } catch (err) {
      log(`IMPORT_ACCOUNTS_FAIL ${err.message}`, "live");
      toast(err.message || "账号导入失败", true);
      return;
    }
  }

  saveState();
  renderAll();
  closeImportModal();
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function closeEditModal() {
  els.editModal.classList.remove("open");
  els.editModal.setAttribute("aria-hidden", "true");
  editContext = null;
}

async function saveEdit() {
  const data = Object.fromEntries(new FormData(els.editForm).entries());
  if (!editContext) return;

  if (editContext.type === "card") {
    const payload = {
      id: editContext.id || id("card"),
      label: String(data.label || "Card").trim(),
      bin: String(data.bin || "000000").trim().slice(0, 8),
      last4: String(data.last4 || "0000").trim().slice(-4),
      region: String(data.region || "US").trim().toUpperCase(),
      type: String(data.type || "Visa").trim(),
      balance: Number(data.balance || 0),
      successRate: Math.max(0, Math.min(100, Number(data.successRate || 0))),
      status: "active",
    };
    const index = state.cards.findIndex((item) => item.id === payload.id);
    if (index >= 0) state.cards[index] = payload;
    else state.cards.unshift(payload);
  } else {
    const payload = {
      id: editContext.id || id("acct"),
      email: String(data.email || "").trim() || `adobe_user_${Date.now()}@trial.local`,
      password: String(data.password || "").trim() || `Aa${Math.random().toString(36).slice(2, 10)}!`,
      ip: String(data.ip || randomIp()).trim(),
      status: "registered",
      eligibility: "unknown",
      plan: "-",
      imageStatus: "untested",
      createdAt: nowText(),
      lastAction: editContext.id ? "手动编辑" : "手动新增",
    };
    try {
      if (editContext.id) {
        const data = await apiRequest(`/api/v1/adobe/accounts/${encodeURIComponent(payload.id)}`, {
          method: "PUT",
          body: JSON.stringify(accountPatch(payload)),
        });
        const merged = normalizeAccount(data.account || payload);
        const index = state.accounts.findIndex((item) => item.id === merged.id);
        if (index >= 0) state.accounts[index] = merged;
        else state.accounts.unshift(merged);
      } else {
        const data = await apiRequest("/api/v1/adobe/accounts/import", {
          method: "POST",
          body: JSON.stringify({ accounts: [payload] }),
        });
        const imported = Array.isArray(data.accounts) ? data.accounts.map(normalizeAccount) : [];
        mergeAccounts(imported);
      }
      if (!state.selectedAccounts.includes(payload.id)) state.selectedAccounts.push(payload.id);
    } catch (err) {
      toast(err.message || "账号保存失败", true);
      return;
    }
  }

  saveState();
  renderAll();
  closeEditModal();
  toast("已保存");
}

async function deleteEdit() {
  if (!editContext?.id) return;
  if (editContext.type === "card") {
    state.cards = state.cards.filter((item) => item.id !== editContext.id);
  } else {
    try {
      await apiRequest(`/api/v1/adobe/accounts/${encodeURIComponent(editContext.id)}`, {
        method: "DELETE",
      });
    } catch (err) {
      toast(err.message || "账号删除失败", true);
      return;
    }
    state.accounts = state.accounts.filter((item) => item.id !== editContext.id);
  }
  saveState();
  renderAll();
  closeEditModal();
  toast("已删除");
}

function saveSettings() {
  state.settings.serviceApiKey = els.serviceApiKey.value.trim();
  state.settings.imageModel = els.imageModel.value.trim() || defaultState.settings.imageModel;
  state.settings.registerCount = Math.max(1, Math.min(50, Number(els.registerCount.value) || 4));
  state.settings.concurrency = Math.max(1, Math.min(10, Number(els.concurrency.value) || 2));
  state.settings.registerUrl = String(els.registerUrlSetting?.value || els.registerUrlInput?.value || defaultState.settings.registerUrl).trim() || defaultState.settings.registerUrl;
  if (!/^https?:\/\//i.test(state.settings.registerUrl)) {
    state.settings.registerUrl = `https://${state.settings.registerUrl}`;
  }
  saveState();
  renderAll();
  toast("设置已保存");
}

function handleTableClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const idValue = button.dataset.id;
  const account = state.accounts.find((item) => item.id === idValue);
  const card = state.cards.find((item) => item.id === idValue);
  if (action === "query" && account) queryEligibility([account]);
  if (action === "test" && account) testImages([account]);
  if (action === "mailbox" && account) fetchMailbox(account);
  if (action === "edit-account" && account) openEdit("account", account);
  if (action === "edit-card" && card) openEdit("card", card);
  if (action === "delete-card" && card) {
    state.cards = state.cards.filter((item) => item.id !== card.id);
    saveState();
    renderAll();
  }
}

function bindEvents() {
  els.navItems.forEach((item) => {
    item.addEventListener("click", () => setView(item.dataset.view));
  });
  document.querySelectorAll("[data-task]").forEach((item) => {
    item.addEventListener("click", () => {
      const task = item.dataset.task;
      if (task === "register") {
        registerAccounts(state.settings.registerCount);
        setView("accounts");
      }
      if (task === "image") testImages();
      if (task === "trial") openTrialModal();
    });
  });

  document.body.addEventListener("click", handleTableClick);
  document.body.addEventListener("change", (event) => {
    if (event.target.classList.contains("account-check")) {
      const accountId = event.target.dataset.id;
      if (event.target.checked) {
        state.selectedAccounts.push(accountId);
      } else {
        state.selectedAccounts = state.selectedAccounts.filter((item) => item !== accountId);
      }
      dedupeSelection();
      renderAll();
    }
    if (event.target.classList.contains("package-check")) {
      const value = event.target.value;
      if (event.target.checked) {
        state.selectedPackages.push(value);
      } else {
        state.selectedPackages = state.selectedPackages.filter((item) => item !== value);
      }
      state.selectedPackages = Array.from(new Set(state.selectedPackages));
      saveState();
      updateWillOpenCount();
    }
  });

  els.selectAllAccounts.addEventListener("change", toggleAllAccounts);
  els.selectAllAdobeAccounts.addEventListener("change", toggleAllAccounts);
  els.accountSource.addEventListener("change", updateWillOpenCount);

  document.getElementById("seedBtn").addEventListener("click", seedData);
  document.getElementById("registerBtn").addEventListener("click", () => {
    registerAccounts(state.settings.registerCount);
  });
  document.getElementById("adobeRegisterBtn").addEventListener("click", () => {
    registerAccounts(state.settings.registerCount);
  });
  document.getElementById("addAccountBtn").addEventListener("click", () => openEdit("account"));
  document.getElementById("addCardBtn").addEventListener("click", () => openEdit("card"));
  document.getElementById("importAccountsBtn").addEventListener("click", () => openImport("accounts"));
  document.getElementById("importCardsBtn").addEventListener("click", () => openImport("cards"));
  document.getElementById("exportAccountsBtn").addEventListener("click", () => {
    downloadJson(`adobe-accounts-${Date.now()}.json`, { accounts: state.accounts });
  });
  document.getElementById("exportCardsBtn").addEventListener("click", () => {
    downloadJson(`adobe-cards-${Date.now()}.json`, { cards: state.cards });
  });
  document.getElementById("queryEligibilityBtn").addEventListener("click", () => queryEligibility());
  document.getElementById("testImagesBtn").addEventListener("click", () => testImages());
  document.getElementById("adobeImageTestBtn").addEventListener("click", () => testImages());
  document.getElementById("openTrialBtn").addEventListener("click", openTrialModal);
  document.getElementById("adobeTrialBtn").addEventListener("click", openTrialModal);
  document.getElementById("adobeOpenWizardBtn").addEventListener("click", openTrialModal);
  document.getElementById("openRegisterUrlBtn").addEventListener("click", () => openRegisterUrl());
  document.getElementById("realRegisterTestBtn").addEventListener("click", realRegisterTest);
  document.getElementById("cloakRegisterBtn").addEventListener("click", cloakRegister);
  els.registerUrlInput.addEventListener("change", () => {
    state.settings.registerUrl = els.registerUrlInput.value.trim() || defaultState.settings.registerUrl;
    saveState();
    renderSettings();
  });
  document.getElementById("closeTrialModalBtn").addEventListener("click", closeTrialModal);
  document.getElementById("startTrialBtn").addEventListener("click", startTrialJob);
  document.getElementById("viewRecordsBtn").addEventListener("click", () => {
    closeTrialModal();
    setView("records");
  });
  document.getElementById("stopJobBtn").addEventListener("click", () => {
    if (runningJob) runningJob.stopped = true;
  });
  document.getElementById("clearLiveLogBtn").addEventListener("click", () => {
    state.logs = [];
    saveState();
    renderLogs();
    apiRequest("/api/v1/adobe/register/logs", { method: "DELETE" }).catch(() => {});
  });
  document.getElementById("clearRecordsBtn").addEventListener("click", () => {
    state.records = [];
    saveState();
    renderAll();
  });
  document.getElementById("exportRecordsBtn").addEventListener("click", () => {
    downloadJson(`adobe-open-records-${Date.now()}.json`, { records: state.records });
  });
  document.getElementById("closeEditModalBtn").addEventListener("click", closeEditModal);
  document.getElementById("saveEditBtn").addEventListener("click", saveEdit);
  document.getElementById("deleteEditBtn").addEventListener("click", deleteEdit);
  document.getElementById("closeImportModalBtn").addEventListener("click", closeImportModal);
  document.getElementById("loadImportExampleBtn").addEventListener("click", fillImportExample);
  document.getElementById("saveImportBtn").addEventListener("click", saveImport);
  document.getElementById("saveSettingsBtn").addEventListener("click", saveSettings);

  [els.trialModal, els.editModal, els.importModal].forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal && !runningJob) {
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
      }
    });
  });
}

function toggleAllAccounts(event) {
  state.selectedAccounts = event.target.checked ? state.accounts.map((item) => item.id) : [];
  renderAll();
}

bindEvents();
renderAll();
loadAccountsFromServer().then(() => renderAll());
setView(activeView);
