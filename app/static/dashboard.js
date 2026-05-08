const body = document.body;
const refreshSec = Number(body.dataset.refreshSec || 15);
const pageSize = Number(body.dataset.pageSize || 18);

const statusChip = document.getElementById("statusChip");
const statusText = document.getElementById("statusText");
const lastUpdatedLabel = document.getElementById("lastUpdatedLabel");
const refreshLabel = document.getElementById("refreshLabel");
const scanCountdownLabel = document.getElementById("scanCountdownLabel");
const scanCountdownBar = document.getElementById("scanCountdownBar");
const persistenceLabel = document.getElementById("persistenceLabel");
const persistenceNote = document.getElementById("persistenceNote");
const riskBanner = document.getElementById("riskBanner");
const scanButton = document.getElementById("scanButton");
const killSwitchToggle = document.getElementById("killSwitchToggle");
const watchToggle = document.getElementById("watchToggle");
const liveTradingToggle = document.getElementById("liveTradingToggle");
const autoExecuteToggle = document.getElementById("autoExecuteToggle");
const tradingCard = document.getElementById("tradingCard");
const armedIndicator = document.getElementById("armedIndicator");
const armedIndicatorTitle = armedIndicator?.querySelector("strong");
const armedIndicatorDetail = armedIndicator?.querySelector("p");
const tradingSummary = document.getElementById("tradingSummary");
const riskSummaryNote = document.getElementById("riskSummaryNote");
const walletStatusNote = document.getElementById("walletStatusNote");
const walletAddress = document.getElementById("walletAddress");
const walletPol = document.getElementById("walletPol");
const walletUsdc = document.getElementById("walletUsdc");
const walletPusd = document.getElementById("walletPusd");
const preflightSummary = document.getElementById("preflightSummary");
const preflightList = document.getElementById("preflightList");
const summaryGrid = document.getElementById("summaryGrid");
const criteriaFunnel = document.getElementById("criteriaFunnel");
const opportunityRows = document.getElementById("opportunityRows");
const strategyStack = document.getElementById("strategyStack");
const alertFeed = document.getElementById("alertFeed");
const executionFeed = document.getElementById("executionFeed");
const positionFeed = document.getElementById("positionFeed");
const tradePnlValue = document.getElementById("tradePnlValue");
const tradePnlMeta = document.getElementById("tradePnlMeta");
const tradePnlNote = document.getElementById("tradePnlNote");
const marketList = document.getElementById("marketList");

const TEXT = {
  waitingSync: "\u5c1a\u672a\u540c\u6b65",
  noData: "\u5c1a\u672a\u5efa\u7acb\u8cc7\u6599",
  noOpportunities: "\u76ee\u524d\u6c92\u6709\u7b26\u5408\u689d\u4ef6\u7684\u5019\u9078\u6a5f\u6703\u3002",
  noStrategies: "\u5c1a\u672a\u6709\u7b56\u7565\u7d71\u8a08\u8cc7\u6599\u3002",
  noAlerts: "\u5c1a\u672a\u9001\u51fa\u4efb\u4f55\u8b66\u793a\u3002",
  noExecutions: "\u5c1a\u672a\u6709\u57f7\u884c\u4e8b\u4ef6\u3002",
  noTrades: "\u76ee\u524d\u6c92\u6709\u4efb\u4f55\u4ea4\u6613\u3002",
  noTradeRecords: "\u76ee\u524d\u6c92\u6709\u4efb\u4f55\u4ea4\u6613\u7d00\u9304",
  noMarkets: "\u5c1a\u672a\u540c\u6b65\u5e02\u5834\u8cc7\u6599\u3002",
  scanNow: "\u7acb\u5373\u6383\u63cf",
  scanAndExecute: "\u7acb\u5373\u6383\u63cf\u4e26\u57f7\u884c",
  scanning: "\u6383\u63cf\u4e2d",
  scanningArmed: "\u6383\u63cf\u8207\u57f7\u884c\u4e2d",
  scanDone: "\u6383\u63cf\u5b8c\u6210",
  armedWatching: "\u6b66\u88dd\u76e3\u770b\u4e2d",
  armedScanning: "\u6b66\u88dd\u6383\u63cf\u4e2d",
  armedSuccess: "\u6b66\u88dd\u6210\u529f\uff0cLive \u8207\u81ea\u52d5\u4e0b\u55ae\u5df2\u555f\u7528",
  armedWatchStopped: "\u5df2\u6b66\u88dd\uff0c\u4f46 watch \u5df2\u505c\u6b62",
  watchStopped: "watch \u5df2\u505c\u6b62",
  watchStale: "watch \u6383\u63cf\u505c\u6eef",
  watchStartingState: "watch \u555f\u52d5\u4e2d",
  watchRunningTitle: "\u6b66\u88dd\u76e3\u770b\u4e2d",
  watchStoppedTitle: "watch \u5df2\u505c\u6b62",
  watchRunningDetail: "\u80cc\u666f\u76e3\u770b\u547d\u4e2d\u53ef\u4ea4\u6613\u689d\u4ef6\u6642\uff0c\u6703\u76f4\u63a5\u8d70\u771f\u5be6\u57f7\u884c\u6d41\u7a0b\u3002",
  watchIdleDetail: "watch \u6b63\u5728\u904b\u4f5c\uff0c\u4f46 Live / \u81ea\u52d5\u4e0b\u55ae\u5c1a\u672a\u540c\u6642\u555f\u7528\u3002",
  watchStoppedDetail: "\u80cc\u666f watch \u5df2\u505c\u6389\uff0c\u73fe\u5728\u4e0d\u6703\u81ea\u52d5\u6383\u63cf\uff0c\u4e5f\u4e0d\u6703\u81ea\u52d5\u9001\u55ae\u3002",
  updateFailed: "\u66f4\u65b0\u4ea4\u6613\u63a7\u5236\u5931\u6557",
  scanFailed: "\u6383\u63cf\u5931\u6557",
  watchStart: "\u555f\u52d5 watch",
  watchStop: "\u505c\u6b62 watch",
  watchStarting: "watch \u555f\u52d5\u4e2d",
  watchStopping: "watch \u505c\u6b62\u4e2d",
  watchUpdated: "watch \u72c0\u614b\u5df2\u66f4\u65b0",
  liveOn: "\u95dc\u9589 Live \u6a21\u5f0f",
  liveOff: "\u555f\u7528 Live \u6a21\u5f0f",
  autoOn: "\u95dc\u9589\u81ea\u52d5\u4e0b\u55ae",
  autoOff: "\u555f\u7528\u81ea\u52d5\u4e0b\u55ae",
  killOn: "\u89e3\u9664\u7dca\u6025\u505c\u6b62",
  killOff: "\u7dca\u6025\u505c\u6b62",
  privateKeyMissing: "\u5c1a\u672a\u8f38\u5165\u79c1\u9470",
  walletLoaded: "\u5df2\u8b80\u53d6\u9322\u5305\u5730\u5740\u8207\u4ee3\u5e63\u9918\u984d",
  walletMissing: "\u5c1a\u672a\u8f38\u5165\u79c1\u9470",
  preflightMissing: "\u5c1a\u672a\u5efa\u7acb\u4ea4\u6613\u524d\u7f6e\u6aa2\u67e5\u5831\u544a\u3002",
  preflightWaiting: "\u7b49\u5f85\u5f8c\u7aef\u540c\u6b65\u3002",
  preflightNoChecks: "\u76ee\u524d\u6c92\u6709\u6aa2\u67e5\u660e\u7d30\u3002",
  preflightReady: "\u524d\u7f6e\u6aa2\u67e5\u901a\u904e\uff0c\u76ee\u524d\u53ef\u7528\u62b5\u62bc\u54c1\uff1a",
  preflightBlockedPrefix: "\u5c1a\u6709 ",
  preflightBlockedSuffix: " \u500b\u963b\u64cb\u9805\u76ee\uff0cLive / \u81ea\u52d5\u4e0b\u55ae\u6703\u4fdd\u6301\u9396\u5b9a\u3002",
  statusOk: "\u901a\u904e",
  statusProblem: "\u6709\u554f\u984c",
  statusStale: "\u5feb\u53d6",
  tradePnlEmptyNote: "\u82e5\u5c1a\u672a\u6210\u4ea4\uff0c\u9019\u500b\u5340\u584a\u6703\u4fdd\u6301\u7a7a\u767d\u63d0\u793a\u3002",
  persistenceSqlite:
    "\u76ee\u524d\u6301\u4e45\u5316\u5f8c\u7aef\uff1aSQLite\u3002runtime controls\u3001execution claims \u8207 audit log \u90fd\u5df2\u5beb\u5165\u8cc7\u6599\u5eab\u3002",
  persistencePostgres:
    "\u76ee\u524d\u6301\u4e45\u5316\u5f8c\u7aef\uff1aPostgreSQL\u3002runtime controls\u3001execution claims \u8207 audit log \u90fd\u5df2\u5beb\u5165\u8cc7\u6599\u5eab\u3002",
  persistenceCloudWarning:
    "\u76ee\u524d\u4ecd\u4f7f\u7528 SQLite\u3002runtime controls \u8207\u4ea4\u6613\u7d00\u9304\u53ef\u904b\u4f5c\uff0c\u4f46\u9577\u6642\u9593\u9ad8\u983b\u5beb\u5165\u4e0b\u7a69\u5b9a\u6027\u4ecd\u8f03\u5f31\u3002",
};

let latestTradingState = {
  live_trading_enabled: false,
  auto_execute_enabled: false,
  kill_switch_enabled: false,
  armed: false,
};

let latestWatchState = {
  running: false,
  state: "stopped",
  message: "",
};

let nextDashboardSyncAt = Date.now() + refreshSec * 1000;
let dashboardLoadInFlight = false;

function formatNumber(value, fractionDigits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(Number(value));
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function formatToken(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  const numeric = Number(value);
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: Math.abs(numeric) >= 1 ? 2 : 4,
    maximumFractionDigits: 4,
  }).format(numeric);
}

function formatSignedToken(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  const numeric = Number(value);
  const prefix = numeric > 0 ? "+" : numeric < 0 ? "-" : "";
  return `${prefix}${formatToken(Math.abs(numeric))}`;
}

function formatTime(value) {
  if (!value) {
    return TEXT.waitingSync;
  }
  return new Date(value).toLocaleString("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function activeDashboardSyncSec() {
  const watchInterval = Number(latestWatchState?.scan_interval_sec || 0);
  if (latestWatchState?.running && watchInterval > 0) {
    return watchInterval;
  }
  return refreshSec;
}

function resetDashboardSyncCountdown() {
  nextDashboardSyncAt = Date.now() + activeDashboardSyncSec() * 1000;
  renderSyncCountdown();
}

function renderSyncCountdown() {
  if (!refreshLabel || !scanCountdownLabel || !scanCountdownBar) {
    return;
  }

  const watchInterval = Number(latestWatchState?.scan_interval_sec || 0);
  const syncSec = activeDashboardSyncSec();
  const remainingMs = Math.max(nextDashboardSyncAt - Date.now(), 0);
  const totalMs = Math.max(syncSec * 1000, 1);
  const progress = Math.min(Math.max((1 - remainingMs / totalMs) * 100, 0), 100);

  if (latestWatchState?.running && watchInterval > 0) {
    refreshLabel.textContent = `${watchInterval} 秒掃描 / ${refreshSec} 秒同步`;
    scanCountdownLabel.textContent = `下次同步倒數 ${(remainingMs / 1000).toFixed(1)} 秒`;
    scanCountdownBar.parentElement?.classList.remove("stopped");
  } else {
    refreshLabel.textContent = `${refreshSec} 秒同步`;
    scanCountdownLabel.textContent = latestWatchState?.state === "stale" ? "watch 掃描停滯，改用頁面同步倒數" : "watch 已停止，改用頁面同步倒數";
    scanCountdownBar.parentElement?.classList.add("stopped");
  }

  scanCountdownBar.style.width = `${progress}%`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function marketLink(slug) {
  return `https://polymarket.com/market/${slug}`;
}

function looksMojibake(value) {
  if (!value) {
    return false;
  }
  return /[ÃÂÅÆÇÉÏÖØÙÚà-ÿ]/.test(String(value));
}

function cleanOpportunityTitle(item) {
  const title = String(item?.title || "").replace(/^\[[^\]]+\]\s*/, "").trim();
  return title || "\u672a\u547d\u540d\u6a5f\u6703";
}

function qualificationLabel(item) {
  const rawLabel = item?.qualification_label;
  if (rawLabel && !looksMojibake(rawLabel)) {
    return rawLabel;
  }
  return item?.qualification_tier === "actionable" ? "\u53ef\u76f4\u63a5\u8b66\u793a" : "\u5019\u9078\u89c0\u5bdf";
}

function strategyLabel(strategyType) {
  const mapping = {
    late_resolution: "Near-close maker",
  };
  return mapping[strategyType] || strategyType || "-";
}

function executionStatusLabel(status) {
  const mapping = {
    submitted: "\u5df2\u9001\u51fa",
    duplicate_claim: "\u91cd\u8907\u8a8d\u9818",
    risk_blocked: "\u98a8\u63a7\u963b\u64cb",
    preflight_blocked: "\u524d\u6aa2\u963b\u64cb",
    controls_restored: "\u5df2\u6062\u5fa9",
    rearmed: "\u5df2\u91cd\u65b0\u6b66\u88dd",
    partial_failure: "\u90e8\u5206\u5931\u6557",
    failed: "\u5931\u6557",
    cancelled: "\u5df2\u53d6\u6d88",
    filled: "\u5df2\u6210\u4ea4",
  };
  return mapping[status] || status || "-";
}

function executionEventPresentation(item) {
  const armed = Boolean(latestTradingState?.armed);

  if (item?.status === "preflight_blocked" && armed) {
    return {
      pillClass: "ok",
      statusLabel: "\u5df2\u6062\u5fa9",
      message: "\u9019\u662f\u6b77\u53f2\u963b\u64cb\u4e8b\u4ef6\uff0c\u76ee\u524d Live \u524d\u7f6e\u6aa2\u67e5\u5df2\u901a\u904e\uff0c\u7cfb\u7d71\u5df2\u91cd\u65b0\u6b66\u88dd\u3002",
    };
  }

  if (item?.status === "controls_restored") {
    return {
      pillClass: "ok",
      statusLabel: executionStatusLabel(item.status),
      message:
        "\u7576\u6642\u7684\u81e8\u6642\u98a8\u63a7\u9396\u5b9a\u5df2\u9084\u539f\u3002" +
        (armed ? "\u76ee\u524d\u72c0\u614b\u70ba\u5df2\u6b66\u88dd\u3002" : "\u76ee\u524d\u8acb\u4ee5\u4e0a\u65b9\u4ea4\u6613\u63a7\u5236\u5361\u7247\u70ba\u6e96\u3002"),
    };
  }

  const pillClass =
    item?.status === "submitted"
      ? "ok"
      : item?.status === "duplicate_claim"
        ? "neutral"
        : item?.status === "rearmed"
          ? "ok"
          : "problem";

  return {
    pillClass,
    statusLabel: executionStatusLabel(item?.status),
    message: item?.message || "-",
  };
}

function readableMarketName(slug) {
  if (!slug) {
    return "風控事件";
  }
  const parts = String(slug).split("-updown-");
  if (parts.length === 2) {
    const asset = parts[0].toUpperCase();
    return `${asset} Up/Down`;
  }
  return String(slug).replace(/-/g, " ");
}

function executionEventSummary(item) {
  const legs = Array.isArray(item?.details?.legs) ? item.details.legs : [];
  const leg = legs[0] || {};
  const response = leg.response || {};
  const notional =
    response.required_collateral ??
    item?.details?.estimated_notional ??
    (Number.isFinite(Number(leg.target_price)) && Number.isFinite(Number(leg.requested_size))
      ? Number(leg.target_price) * Number(leg.requested_size)
      : null);
  return {
    market: readableMarketName(leg.market_slug),
    time: formatTime(item?.created_at),
    size: leg.requested_size ? `${formatToken(leg.requested_size)} 股` : notional ? `${formatToken(notional)} pUSD` : "-",
    status: executionStatusLabel(item?.status),
    pillClass: executionEventPresentation(item).pillClass,
    outcome: leg.outcome_label ? ` / ${leg.outcome_label}` : "",
  };
}

function renderWatchIndicator(trading, watch, risk) {
  if (!armedIndicator || !armedIndicatorTitle || !armedIndicatorDetail) {
    return;
  }

  const armed = Boolean(trading?.armed);
  const watchRunning = Boolean(watch?.running);
  const watchStopped = !watchRunning;
  const nearCloseLiveEnabled = Boolean(risk?.near_close?.live_enabled);
  const shouldShow = armed || watchStopped;

  armedIndicator.classList.remove("stopped", "idle");
  armedIndicator.classList.toggle("hidden", !shouldShow);
  if (!shouldShow) {
    return;
  }

  if (watchStopped) {
    armedIndicator.classList.add("stopped");
    armedIndicatorTitle.textContent = TEXT.watchStoppedTitle;
    armedIndicatorDetail.textContent = watch?.message || TEXT.watchStoppedDetail;
    return;
  }

  if (!armed) {
    armedIndicator.classList.add("idle");
  }
  armedIndicatorTitle.textContent = armed ? (nearCloseLiveEnabled ? "實戰模式" : "系統已武裝") : TEXT.watchRunningTitle;
  armedIndicatorDetail.textContent = armed
    ? nearCloseLiveEnabled
      ? "watch 正在運行；Near-close maker 命中 live 條件時會送出 post-only GTD 真單。"
      : "watch 正在運行；Near-close maker 目前只收集 paper signal，不會送真單。"
    : TEXT.watchIdleDetail;
}

function walletStatusMessage(wallet) {
  if (!wallet?.configured) {
    return TEXT.walletMissing;
  }
  if (wallet?.message && !looksMojibake(wallet.message)) {
    return wallet.message;
  }
  return TEXT.walletLoaded;
}

function preflightCheckLabel(check) {
  const fallback = {
    kill_switch: "\u98a8\u63a7\u958b\u95dc",
    live_stack: "\u4ea4\u6613\u5806\u758a",
    private_key: "\u79c1\u9470",
    funder: "Funder \u5730\u5740",
    polygon_chain: "Polygon Chain ID",
    pol_balance: "POL Gas",
    collateral_ready: "\u4ea4\u6613\u62b5\u62bc\u54c1",
    pusd_balance: "pUSD",
    clock_drift: "\u7cfb\u7d71\u6642\u9593",
    exchange_allowance: "Legacy Exchange \u6388\u6b0a",
    conditional_allowance: "Outcome Token \u6388\u6b0a",
    clob_credentials: "CLOB API \u6191\u8b49",
  };
  if (check?.label && !looksMojibake(check.label)) {
    return check.label;
  }
  return fallback[check?.id] || check?.id || "-";
}

function preflightCheckMessage(check, collateralSymbol) {
  if (check?.message && !looksMojibake(check.message)) {
    return check.message;
  }
  const collateral = collateralSymbol || "pUSD";
  const value = check?.value;
  const threshold = check?.threshold;
  const fallback = {
    kill_switch: check?.status === "ok" ? "Kill switch \u672a\u555f\u52d5\u3002" : "Kill switch \u5df2\u555f\u52d5\u3002",
    live_stack: `\u76ee\u524d Polymarket Live \u4ea4\u6613\u9700\u4f7f\u7528 ${check?.value || "CLOB V2 / pUSD / py-clob-client-v2"}\u3002`,
    private_key: check?.status === "ok" ? "\u79c1\u9470\u683c\u5f0f\u53ef\u8b80\u53d6\u3002" : "\u79c1\u9470\u4e0d\u53ef\u7528\u3002",
    funder: `\u76ee\u524d funder\uff1a${check?.value || "-"}`,
    polygon_chain: value === 137 ? "RPC \u5df2\u9023\u5230 Polygon \u4e3b\u7db2\u3002" : `RPC chain id \u70ba ${value ?? "-"}\u3002`,
    pol_balance: `POL \u9918\u984d ${formatToken(value)}\u3002`,
    collateral_ready:
      check?.status === "ok"
        ? `${collateral} \u53ef\u7528\u9918\u984d ${formatToken(value)}\u3002`
        : `${collateral} \u53ef\u7528\u9918\u984d ${formatToken(value)} \u4f4e\u65bc\u6700\u4f4e\u9700\u6c42 ${formatToken(threshold)}\u3002`,
    pusd_balance: `\u76ee\u524d\u9322\u5305 pUSD \u9918\u984d ${formatToken(value || 0)}\u3002`,
    clock_drift: `\u672c\u6a5f\u6642\u9593\u504f\u5dee ${formatToken(value)} \u79d2\u3002`,
    exchange_allowance:
      check?.status === "ok"
        ? "Legacy CLOB \u5075\u6e2c\u5230\u4e3b\u8981 exchange allowance \u90fd\u5df2\u53ef\u7528\u3002"
        : "Exchange allowance \u5c1a\u672a\u9054\u5230\u6700\u4f4e\u9700\u6c42\u3002",
    conditional_allowance:
      "SELL \u817f\u6703\u5728\u9001\u55ae\u524d\u4f9d token \u5373\u6642\u9a57\u8b49 outcome token allowance\u3002",
    clob_credentials:
      "\u76ee\u524d\u4f7f\u7528\u5feb\u53d6\u7d50\u679c\uff0c\u5c1a\u672a\u91cd\u65b0\u9a57\u8b49 legacy CLOB API \u6191\u8b49\u3002",
  };
  return fallback[check?.id] || "\u76ee\u524d\u6c92\u6709\u66f4\u591a\u8aaa\u660e\u3002";
}

function setStatus(kind, label) {
  statusChip.classList.remove("hot", "warn");
  if (kind) {
    statusChip.classList.add(kind);
  }
  statusText.textContent = label;
}

function setControlBusy(isBusy) {
  liveTradingToggle.disabled = isBusy || liveTradingToggle.dataset.locked === "true";
  autoExecuteToggle.disabled = isBusy || autoExecuteToggle.dataset.locked === "true";
  killSwitchToggle.disabled = isBusy;
  watchToggle.disabled = isBusy || watchToggle.dataset.busy === "true";
  scanButton.disabled = isBusy || scanButton.dataset.busy === "true";
}

function syncScanButtonLabel() {
  scanButton.dataset.defaultLabel = latestTradingState.armed ? TEXT.scanAndExecute : TEXT.scanNow;
  scanButton.dataset.loadingLabel = latestTradingState.armed ? TEXT.scanningArmed : TEXT.scanning;
  if (scanButton.dataset.busy !== "true") {
    scanButton.textContent = scanButton.dataset.defaultLabel;
  }
}

function syncWatchButtonLabel() {
  if (!watchToggle) {
    return;
  }
  const watchEnabled = latestWatchState.running || latestWatchState.state === "starting" || latestWatchState.state === "stale";
  watchToggle.textContent = watchEnabled ? TEXT.watchStop : TEXT.watchStart;
  watchToggle.classList.toggle("active", watchEnabled);
}

function renderSummary(summary) {
  const cards = [
    {
      label: "\u5df2\u63a2\u7d22\u5e02\u5834",
      value: formatNumber(summary.total_markets || summary.latest_discovered_market_count),
      footnote: `\u6700\u8fd1\u63a2\u7d22\uff1a${formatTime(summary.latest_discovered_at)}`,
    },
    {
      label: "\u5373\u6642\u76e3\u770b\u4e2d",
      value: formatNumber(summary.latest_monitored_markets),
      footnote: `\u6700\u8fd1\u6383\u63cf\uff1a${formatTime(summary.latest_scan_at)}\uff0c\u8a02\u55ae\u7c3f ${formatNumber(summary.latest_book_count)} \u672c`,
    },
    {
      label: "\u5019\u9078 / \u53ef\u8b66\u793a",
      value: `${formatNumber(summary.latest_candidate_count)} / ${formatNumber(summary.latest_actionable_count)}`,
      footnote: `\u672c\u8f2a\u6a5f\u6703 ${formatNumber(summary.latest_scan_opportunities)} \u7b46\uff0c\u6700\u4f73\u6de8\u908a\u969b ${formatPercent(summary.best_net_edge)}`,
    },
    {
      label: "24H \u8b66\u793a / \u57f7\u884c",
      value: `${formatNumber(summary.alerts_24h)} / ${formatNumber(summary.execution_events_24h)}`,
      footnote: `\u6700\u8fd1\u4e8b\u4ef6\uff1a${formatTime(summary.latest_execution_event_at || summary.latest_alert_at)}`,
    },
  ];

  summaryGrid.innerHTML = cards
    .map(
      (card) => `
        <article class="metric-card">
          <span class="metric-label">${card.label}</span>
          <strong class="metric-value">${card.value}</strong>
          <p class="metric-footnote">${card.footnote}</p>
        </article>
      `,
    )
    .join("");
}

function renderCriteriaFunnel(summary) {
  if (!criteriaFunnel) {
    return;
  }

  const funnel = Array.isArray(summary?.near_close_funnel) ? summary.near_close_funnel : [];
  if (!funnel.length) {
    criteriaFunnel.innerHTML = `
      <article class="criteria-stage">
        <div>
          <span>等待掃描資料</span>
          <p>完成下一輪掃描後，這裡會依序顯示每一關剩下多少市場。</p>
        </div>
        <div class="criteria-count">
          <strong>-</strong>
          <small>尚未同步</small>
        </div>
      </article>
    `;
    return;
  }

  criteriaFunnel.innerHTML = funnel
    .map((stage, index) => {
      const count = Number(stage?.count || 0);
      const previous = index > 0 ? Number(funnel[index - 1]?.count || 0) : count;
      const dropped = Math.max(previous - count, 0);
      const dropLabel = index === 0 ? "起點" : `淘汰 ${formatNumber(dropped)}`;
      return `
        <article class="criteria-stage ${count === 0 ? "is-zero" : ""}">
          <div>
            <span>${escapeHtml(`${index + 1}. ${stage?.label || "-"}`)}</span>
            <p>${escapeHtml(stage?.description || "")}</p>
          </div>
          <div class="criteria-count">
            <strong>${formatNumber(count)}</strong>
            <small>${escapeHtml(dropLabel)}</small>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderStrategies(strategies) {
  if (!strategies?.length) {
    strategyStack.innerHTML = `<p class="empty-block">${TEXT.noStrategies}</p>`;
    return;
  }

  const maxCount = Math.max(...strategies.map((item) => Number(item.count) || 0), 1);
  strategyStack.innerHTML = strategies
    .map(
      (item) => `
        <article class="strategy-card">
          <header>
            <strong>${escapeHtml(strategyLabel(item.strategy_type))}</strong>
            <span>${formatNumber(item.count)} \u7b46</span>
          </header>
          <p>\u5e73\u5747\u6de8\u908a\u969b ${formatPercent(item.avg_net_edge)}\uff0c\u6700\u4f73 ${formatPercent(item.best_net_edge)}</p>
          <div class="strategy-bar"><span style="width:${Math.max((Number(item.count) / maxCount) * 100, 8)}%"></span></div>
        </article>
      `,
    )
    .join("");
}

function renderOpportunities(opportunities) {
  if (!opportunities?.length) {
    opportunityRows.innerHTML = `<tr><td colspan="6" class="empty-cell">${TEXT.noOpportunities}</td></tr>`;
    return;
  }

  opportunityRows.innerHTML = opportunities
    .slice(0, pageSize)
    .map((item) => {
      const links = (item.market_slugs || [])
        .map(
          (slug) =>
            `<a class="link-chip" href="${marketLink(slug)}" target="_blank" rel="noreferrer">${escapeHtml(slug)}</a>`,
        )
        .join("");
      const label = qualificationLabel(item);
      const summary = item.summary || cleanOpportunityTitle(item);
      const action = item.details?.suggested_action || item.details?.action || "\u8acb\u5148\u4eba\u5de5\u8986\u6838\u3002";
      return `
        <tr>
          <td><span class="strategy-chip">${escapeHtml(strategyLabel(item.strategy_type))}</span></td>
          <td>
            <div class="opportunity-title">[${escapeHtml(label)}] ${escapeHtml(cleanOpportunityTitle(item))}</div>
            <div>${escapeHtml(summary)}</div>
            <div class="opportunity-links">${links}</div>
          </td>
          <td>
            <div class="stat-strong stat-positive">${formatPercent(item.net_edge)}</div>
            <div>\u6bdb\u908a\u969b ${formatPercent(item.gross_edge)}</div>
          </td>
          <td>
            <div class="stat-strong">${formatNumber(item.available_liquidity)}</div>
            <div>\u5b89\u5168\u90e8\u4f4d ${formatNumber(item.max_safe_size)}</div>
          </td>
          <td>
            <div class="stat-strong">${formatPercent(item.confidence_score)}</div>
            <div>${formatTime(item.created_at)}</div>
          </td>
          <td><div class="suggestion">${escapeHtml(`${label}\uff1a${action}`)}</div></td>
        </tr>
      `;
    })
    .join("");
}

function renderAlerts(alerts) {
  if (!alerts?.length) {
    alertFeed.innerHTML = `<p class="empty-block">${TEXT.noAlerts}</p>`;
    return;
  }

  alertFeed.innerHTML = alerts
    .map(
      (item) => `
        <article class="feed-item">
          <header>
            <strong>${escapeHtml(item.channel)}</strong>
            <span class="feed-pill neutral">alert</span>
          </header>
          <p>${escapeHtml(item.message)}</p>
          <time>${formatTime(item.sent_at)}</time>
        </article>
      `,
    )
    .join("");
}

function renderExecutionEvents(events) {
  if (!events?.length) {
    executionFeed.innerHTML = `<p class="empty-block">${TEXT.noExecutions}</p>`;
    return;
  }

  executionFeed.innerHTML = events
    .map((item) => {
      const summary = executionEventSummary(item);
      return `
        <article class="feed-item execution-item">
          <div class="execution-main">
            <strong>${escapeHtml(summary.market)}${escapeHtml(summary.outcome)}</strong>
            <time>${escapeHtml(summary.time)}</time>
          </div>
          <div class="execution-meta">
            <span>${escapeHtml(summary.size)}</span>
            <span class="feed-pill ${summary.pillClass}">${escapeHtml(summary.status)}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderTradeJournal(positions, tradeJournal) {
  const groups = Array.isArray(positions) ? positions : [];
  const totalTrades = Number(tradeJournal?.trade_count_total || 0);
  const realizedPnl = Number(tradeJournal?.estimated_realized_pnl_total || 0);
  const todayPnl = Number(tradeJournal?.estimated_realized_pnl_today || 0);
  const openSize = Number(tradeJournal?.open_size_total || 0);
  const pusdBalanceDelta =
    tradeJournal?.pusd_balance_delta === null || tradeJournal?.pusd_balance_delta === undefined
      ? null
      : Number(tradeJournal.pusd_balance_delta);
  const pusdBaseline =
    tradeJournal?.pusd_pnl_baseline === null || tradeJournal?.pusd_pnl_baseline === undefined
      ? null
      : Number(tradeJournal.pusd_pnl_baseline);
  const hasTrades = totalTrades > 0 || groups.length > 0;

  if (!hasTrades) {
    tradePnlValue.textContent = "-";
    tradePnlValue.classList.remove("positive", "negative");
    tradePnlMeta.textContent = TEXT.noTradeRecords;
    tradePnlNote.textContent = TEXT.tradePnlEmptyNote;
    positionFeed.innerHTML = `<p class="empty-block">${TEXT.noTrades}</p>`;
    return;
  }

  const totalPnl = groups.reduce(
    (sum, group) => sum + Number(group.total_pnl ?? group.estimated_realized_pnl ?? 0),
    0,
  );
  const unrealizedPnl = groups.reduce(
    (sum, group) => sum + Number(group.unrealized_pnl ?? 0),
    0,
  );

  const headlinePnl = pusdBalanceDelta ?? totalPnl;
  tradePnlValue.textContent = `${formatSignedToken(headlinePnl)} pUSD`;
  tradePnlValue.classList.toggle("positive", headlinePnl > 0);
  tradePnlValue.classList.toggle("negative", headlinePnl < 0);
  tradePnlMeta.textContent =
    pusdBalanceDelta === null
      ? `\u4ea4\u6613\u5e33\u672c ${formatSignedToken(totalPnl)} pUSD\uff0c\u5df2\u5be6\u73fe ${formatSignedToken(realizedPnl)} pUSD`
      : `pUSD \u9918\u984d\u8b8a\u5316 ${formatSignedToken(pusdBalanceDelta)} pUSD\uff0c\u4ea4\u6613\u5e33\u672c ${formatSignedToken(totalPnl)} pUSD`;
  tradePnlNote.textContent =
    pusdBaseline === null
      ? `\u4eca\u65e5\u5df2\u5be6\u73fe ${formatSignedToken(todayPnl)} pUSD\uff0c\u7d2f\u8a08 ${formatNumber(totalTrades)} \u7b46\u6210\u4ea4\uff0c\u672a\u5e73\u5009 ${formatToken(openSize)} \u80a1\u3002`
      : `\u57fa\u6e96 ${formatToken(pusdBaseline)} pUSD\uff0c\u4eca\u65e5\u5df2\u5be6\u73fe ${formatSignedToken(todayPnl)} pUSD\uff0c\u672a\u5e73\u5009 ${formatToken(openSize)} \u80a1\u3002`;

  positionFeed.innerHTML = groups
    .map((group) => {
      const groupPnl = Number(group.total_pnl ?? group.estimated_realized_pnl ?? 0);
      const pnlClass = groupPnl > 0 ? "positive" : groupPnl < 0 ? "negative" : "neutral";
      const trades = Array.isArray(group.trades) ? group.trades : [];
      const isOpen = Number(group.open_size || 0) > 0;
      const statusLabel = isOpen ? "\u672a\u7d50\u5e33" : "\u5df2\u5b8c\u7d50";
      const valueLabel = isOpen ? "\u73fe\u5728\u4f30\u503c" : "\u8ce3\u51fa / redeem";
      const valueAmount = isOpen ? group.current_value : group.exit_notional;
      const priceLabel = isOpen
        ? group.current_price === null || group.current_price === undefined
          ? "\u73fe\u5728\u50f9\u683c\uff1a\u7f3a\u50f9\u683c"
          : `\u73fe\u5728\u50f9\u683c\uff1a${formatToken(group.current_price)}`
        : `\u5df2\u8ce3\u51fa ${formatToken(group.sell_size)} \u80a1 / redeem ${formatToken(group.redeemed_size)} \u80a1`;
      return `
        <article class="trade-group">
          <header class="trade-group__header">
            <div>
              <strong>${escapeHtml(group.market_slug || "-")}</strong>
              <p>${escapeHtml(group.outcome_label || "-")} \u00b7 ${statusLabel} \u00b7 \u72c0\u614b ${escapeHtml(group.latest_status || "-")}</p>
            </div>
            <div class="trade-group__pnl ${pnlClass}">
              <span>\u640d\u76ca</span>
              <strong>${formatSignedToken(groupPnl)} pUSD</strong>
            </div>
          </header>
          <div class="trade-metric-grid">
            <div>
              <span>\u958b\u5009\u7e3d\u91d1\u984d</span>
              <strong>${formatToken(group.entry_notional)} pUSD</strong>
            </div>
            <div>
              <span>${valueLabel}</span>
              <strong>${valueAmount === null || valueAmount === undefined ? "-" : `${formatToken(valueAmount)} pUSD`}</strong>
            </div>
            <div>
              <span>${isOpen ? "\u672a\u5be6\u73fe" : "\u5df2\u5be6\u73fe"}</span>
              <strong>${formatSignedToken(isOpen ? group.unrealized_pnl : group.estimated_realized_pnl)} pUSD</strong>
            </div>
            <div>
              <span>\u90e8\u4f4d</span>
              <strong>${formatToken(group.open_size)} \u80a1</strong>
            </div>
          </div>
          <div class="trade-group__summary">
            <span>${priceLabel}</span>
            <span>\u8cb7\u5165 ${formatToken(group.buy_size)} \u80a1</span>
            <span>\u8ce3\u51fa ${formatToken(group.sell_size)} \u80a1</span>
            <span>redeem ${formatToken(group.redeemed_size)} \u80a1</span>
          </div>
          <div class="trade-leg-list">
            ${trades
              .map((item) => {
                const action = String(item.action || "").toUpperCase();
                const actionClass = action === "BUY" ? "ok" : action === "SELL" ? "neutral" : "problem";
                return `
                  <div class="trade-leg">
                    <span class="feed-pill ${actionClass}">${escapeHtml(action || "-")}</span>
                    <span>${formatToken(item.size)} \u80a1 @ ${formatToken(item.price)}</span>
                    <span>${formatToken(item.notional)} pUSD</span>
                    <span>${escapeHtml(item.status || "-")}</span>
                    <time>${formatTime(item.created_at)}</time>
                  </div>
                `;
              })
              .join("")}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderMarkets(markets) {
  if (!markets?.length) {
    marketList.innerHTML = `<p class="empty-block">${TEXT.noMarkets}</p>`;
    return;
  }

  marketList.innerHTML = markets
    .map(
      (item) => `
        <article class="market-card">
          <header>
            <strong>${escapeHtml(item.question)}</strong>
            <span>${formatNumber(item.liquidity)}</span>
          </header>
          <p>${escapeHtml(item.slug)}</p>
          <div class="market-links">
            <a class="link-chip" href="${marketLink(item.slug)}" target="_blank" rel="noreferrer">\u6253\u958b\u5e02\u5834</a>
          </div>
          <span class="market-meta">\u6700\u8fd1\u63a2\u7d22\uff1a${formatTime(item.discovered_at)}</span>
        </article>
      `,
    )
    .join("");
}

function renderTrading(trading, wallet, risk, preflight, persistence, watch) {
  latestTradingState = trading || latestTradingState;
  latestWatchState = watch || latestWatchState;
  const walletReady = Boolean(wallet?.address);
  const preflightReady = Boolean(preflight?.ready);
  const liveEnabled = Boolean(latestTradingState.live_trading_enabled);
  const autoEnabled = Boolean(latestTradingState.auto_execute_enabled);
  const killSwitchEnabled = Boolean(latestTradingState.kill_switch_enabled);
  const armed = Boolean(latestTradingState.armed);
  const watchRunning = Boolean(latestWatchState.running);

  liveTradingToggle.dataset.locked = !liveEnabled && (!walletReady || killSwitchEnabled || !preflightReady) ? "true" : "false";
  autoExecuteToggle.dataset.locked =
    !autoEnabled && (!walletReady || killSwitchEnabled || !liveEnabled || !preflightReady) ? "true" : "false";

  liveTradingToggle.classList.toggle("active", liveEnabled);
  autoExecuteToggle.classList.toggle("active", autoEnabled);
  killSwitchToggle.classList.toggle("active", killSwitchEnabled);
  syncWatchButtonLabel();
  tradingCard.classList.toggle("armed", armed);
  body.classList.toggle("armed-state", armed);
  renderWatchIndicator(latestTradingState, latestWatchState, risk);

  liveTradingToggle.textContent = liveEnabled ? TEXT.liveOn : TEXT.liveOff;
  autoExecuteToggle.textContent = autoEnabled ? TEXT.autoOn : TEXT.autoOff;
  killSwitchToggle.textContent = killSwitchEnabled ? TEXT.killOn : TEXT.killOff;

  if (killSwitchEnabled) {
    tradingSummary.textContent = "Kill switch \u5df2\u555f\u7528\uff0cLive \u8207\u81ea\u52d5\u4e0b\u55ae\u90fd\u88ab\u9396\u4f4f\u3002";
  } else if (!walletReady) {
    tradingSummary.textContent = "\u5c1a\u672a\u8f38\u5165\u79c1\u9470\uff0c\u76ee\u524d\u53ea\u80fd\u4f7f\u7528\u7d14\u6383\u63cf\u6a21\u5f0f\u3002";
  } else if (!preflightReady) {
    const reason = preflight?.blocking_reasons?.[0] || "\u4ea4\u6613\u524d\u7f6e\u6aa2\u67e5\u5c1a\u672a\u901a\u904e\u3002";
    tradingSummary.textContent = `Live \u4ea4\u6613\u66ab\u6642\u9396\u5b9a\uff1a${reason}`;
  } else if (armed && !watchRunning) {
    tradingSummary.textContent = "Live \u8207\u81ea\u52d5\u4e0b\u55ae\u5df2\u6b66\u88dd\uff0c\u4f46 watch \u5df2\u505c\u6389\uff0c\u76ee\u524d\u4e0d\u6703\u81ea\u52d5\u6383\u63cf\u3002";
  } else if (armed && !risk?.near_close?.live_enabled) {
    tradingSummary.textContent = "Live 與自動下單已開啟，但 Near-close maker 仍是 paper-only，目前不會送真單。";
  } else if (armed) {
    tradingSummary.textContent = "實戰模式：Live 與自動下單已開啟；Near-close maker 命中時會送出 post-only GTD 真單。";
  } else if (liveEnabled) {
    tradingSummary.textContent = "Live \u6a21\u5f0f\u5df2\u555f\u7528\uff0c\u4f46\u81ea\u52d5\u4e0b\u55ae\u5c1a\u672a\u958b\u555f\u3002";
  } else {
    tradingSummary.textContent = "\u76ee\u524d\u662f\u7d14\u6383\u63cf\u6a21\u5f0f\uff0c\u5c1a\u672a\u555f\u7528 Live \u4ea4\u6613\u3002";
  }

  riskSummaryNote.textContent = killSwitchEnabled
    ? "Kill switch \u5df2\u555f\u7528\uff0c\u6240\u6709 Live \u9001\u55ae\u90fd\u6703\u88ab\u963b\u64cb\u3002"
    : `\u7d19\u4e0a ${formatToken(risk?.paper_notional_today)} / ${formatToken(risk?.max_daily_paper_notional)}\uff0cLive ${formatToken(risk?.live_notional_today)} / ${formatToken(risk?.max_daily_live_notional)}\uff0c\u55ae\u7b46\u4e0a\u9650 ${formatToken(risk?.max_notional_per_plan)}\u3002 Near-close maker\uff1a${formatNumber(risk?.near_close?.signal_count || 0)} / ${formatNumber(risk?.near_close?.paper_required || 100)} paper signals\uff0c\u66dd\u96aa ${formatToken(risk?.near_close?.live_exposure || 0)} / ${formatToken(risk?.near_close?.max_total_exposure || 3)} pUSD\u3002`;

  const banners = [];
  if (killSwitchEnabled) {
    banners.push("\u7dca\u6025\u505c\u6b62\u5df2\u555f\u7528\uff1aLive \u8207\u81ea\u52d5\u4e0b\u55ae\u90fd\u6703\u88ab\u963b\u64cb\u3002");
  }
  if (!watchRunning) {
    banners.push(latestWatchState.message || TEXT.watchStoppedDetail);
  }
  if (armed && risk?.near_close?.live_enabled) {
    banners.push("實戰模式：Near-close maker 命中 live 條件時會送出 post-only GTD 真單。");
  } else if (armed) {
    banners.push("系統已武裝，但 Near-close maker 目前是 paper-only，不會送真單。");
  }
  if (persistence?.cloud_warning) {
    banners.push("\u76ee\u524d\u4ecd\u7528 SQLite \u90e8\u7f72\uff1b\u5728\u9577\u6642\u9593\u9ad8\u983b\u5beb\u5165\u4e0b\uff0c\u7a69\u5b9a\u6027\u4ecd\u4e0d\u5982 PostgreSQL / Cloud SQL\u3002");
  }
  if (risk?.near_close && !risk.near_close.live_enabled) {
    banners.push("Near-close maker \u76ee\u524d\u53ea\u505a paper \u89c0\u5bdf\uff0c\u672a\u555f\u7528\u771f\u55ae\u3002");
  }

  if (banners.length > 0) {
    riskBanner.classList.remove("hidden");
    riskBanner.textContent = banners.join(" ");
  } else {
    riskBanner.classList.add("hidden");
    riskBanner.textContent = "";
  }

  setControlBusy(false);
  syncScanButtonLabel();
}

function renderPreflight(preflight) {
  if (!preflight) {
    preflightSummary.textContent = TEXT.preflightMissing;
    preflightList.innerHTML = `<p class="empty-block">${TEXT.preflightWaiting}</p>`;
    return;
  }

  const blockingCount = preflight.blocking_reasons?.length || 0;
  preflightSummary.textContent = preflight.stale
    ? "\u524d\u7f6e\u6aa2\u67e5\u9019\u6b21\u8d85\u6642\uff0c\u76ee\u524d\u986f\u793a\u5feb\u53d6\u6216\u4e0b\u6b21\u91cd\u8a66\u72c0\u614b\u3002"
    : preflight.ready
    ? `${TEXT.preflightReady}${preflight.collateral_symbol || "-"}\u3002`
    : `${TEXT.preflightBlockedPrefix}${blockingCount}${TEXT.preflightBlockedSuffix}`;

  const checks = Array.isArray(preflight.checks) ? preflight.checks : [];
  if (!checks.length) {
    preflightList.innerHTML = `<p class="empty-block">${TEXT.preflightNoChecks}</p>`;
    return;
  }

  const statusLabel = {
    ok: TEXT.statusOk,
    warning: TEXT.statusProblem,
    critical: TEXT.statusProblem,
  };

  preflightList.innerHTML = checks
    .map((check) => {
      const stateClass = check.status === "ok" ? "ok" : preflight.stale ? "stale" : "problem";
      const label = preflightCheckLabel(check);
      const message = preflightCheckMessage(check, preflight.collateral_symbol);
      return `
        <article class="preflight-item ${stateClass}" title="${escapeHtml(message)}">
          <div>
            <strong>${escapeHtml(label)}</strong>
            <p>${escapeHtml(message)}</p>
          </div>
          <span>${escapeHtml(preflight.stale ? TEXT.statusStale : statusLabel[check.status] || check.status)}</span>
        </article>
      `;
    })
    .join("");
}

function renderWallet(wallet) {
  walletStatusNote.textContent = walletStatusMessage(wallet);
  if (!wallet?.configured) {
    walletAddress.textContent = "-";
    walletPol.textContent = "-";
    walletUsdc.textContent = "-";
    walletPusd.textContent = "-";
    return;
  }

  const balances = Object.fromEntries((wallet.balances || []).map((item) => [item.symbol, item]));
  walletAddress.textContent = wallet.address || "-";
  walletPol.textContent = formatToken(balances.POL?.amount);
  walletUsdc.textContent = formatToken(balances.USDC?.amount);
  walletPusd.textContent = formatToken(balances.pUSD?.amount);
}

function renderPersistence(persistence) {
  const backend = persistence?.backend === "postgresql" ? "PostgreSQL" : "SQLite";
  persistenceLabel.textContent = backend;
  if (persistence?.cloud_warning) {
    persistenceNote.textContent = TEXT.persistenceCloudWarning;
    return;
  }
  persistenceNote.textContent = backend === "PostgreSQL" ? TEXT.persistencePostgres : TEXT.persistenceSqlite;
}

async function toggleWatch() {
  if (watchToggle.dataset.busy === "true") {
    return;
  }
  watchToggle.dataset.busy = "true";
  watchToggle.disabled = true;
  setControlBusy(true);
  setStatus("", latestWatchState.running ? TEXT.watchStopping : TEXT.watchStarting);
  try {
    const response = await fetch("/api/actions/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!response.ok) {
      throw new Error("watch_toggle_failed");
    }
    const body = await response.json();
    applyDashboardPayload(body.payload);
    await loadDashboard({ force: true });
    setStatus("hot", TEXT.watchUpdated);
  } catch (_error) {
    setStatus("warn", TEXT.updateFailed);
  } finally {
    watchToggle.dataset.busy = "false";
    watchToggle.disabled = false;
    setControlBusy(false);
  }
}

function applyDashboardPayload(payload) {
  renderSummary(payload.summary || {});
  renderCriteriaFunnel(payload.summary || {});
  renderStrategies(payload.strategies || []);
  renderOpportunities(payload.opportunities || []);
  renderAlerts(payload.alerts || []);
  renderExecutionEvents(payload.execution_events || []);
  renderTradeJournal(payload.trade_groups || payload.positions || [], payload.trade_journal || payload.pnl || {});
  renderMarkets(payload.markets || []);
  renderTrading(
    payload.trading || latestTradingState,
    payload.wallet || {},
    payload.risk || {},
    payload.preflight || {},
    payload.persistence || {},
    payload.watch || latestWatchState,
  );
  renderWallet(payload.wallet || {});
  renderPreflight(payload.preflight || null);
  renderPersistence(payload.persistence || {});

  lastUpdatedLabel.textContent = formatTime(
    payload.summary?.latest_snapshot_at ||
      payload.summary?.latest_scan_at ||
      payload.summary?.latest_discovered_at,
  );

  if (payload.scan_in_progress) {
    setStatus("", latestTradingState.armed ? TEXT.armedScanning : TEXT.scanning);
  } else if (!latestWatchState.running) {
    const stoppedLabel =
      latestTradingState.armed
        ? TEXT.armedWatchStopped
        : latestWatchState.state === "starting"
          ? TEXT.watchStartingState
        : latestWatchState.state === "stale"
          ? TEXT.watchStale
          : TEXT.watchStopped;
    setStatus("warn", stoppedLabel);
  } else if (latestTradingState.armed) {
    setStatus("hot", TEXT.armedWatching);
  } else if (payload.summary?.latest_snapshot_at || payload.summary?.latest_scan_at) {
    setStatus("hot", TEXT.scanDone);
  } else {
    setStatus("warn", TEXT.noData);
  }

  resetDashboardSyncCountdown();
}

function tryApplyDashboardPayload(payload) {
  try {
    applyDashboardPayload(payload);
    return true;
  } catch (error) {
    console.error("Failed to apply dashboard payload", error, payload);
    return false;
  }
}

async function loadDashboard() {
  dashboardLoadInFlight = true;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) {
      throw new Error("dashboard_fetch_failed");
    }
    const payload = await response.json();
    if (!tryApplyDashboardPayload(payload)) {
      throw new Error("dashboard_render_failed");
    }
  } finally {
    dashboardLoadInFlight = false;
  }
}

async function toggleAction(path, loadingText, successText) {
  setControlBusy(true);
  setStatus("", loadingText);
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!response.ok) {
      if (response.status === 409) {
        const error = await response.json();
        const detail = error.detail;
        const code = typeof detail === "object" ? detail.code : detail;
        if (code === "preflight_failed") {
          const reason = detail?.reasons?.[0] || "\u4ea4\u6613\u524d\u7f6e\u6aa2\u67e5\u672a\u901a\u904e";
          await loadDashboard();
          setStatus("warn", reason);
          return;
        }
        if (code === "private_key_not_configured") {
          await loadDashboard();
          setStatus("warn", TEXT.privateKeyMissing);
          return;
        }
        if (code === "live_trading_not_enabled") {
          await loadDashboard();
          setStatus("warn", "\u8acb\u5148\u555f\u7528 Live \u6a21\u5f0f");
          return;
        }
        if (code === "kill_switch_enabled") {
          await loadDashboard();
          setStatus("warn", "Kill switch \u5df2\u555f\u7528\uff0c\u8acb\u5148\u89e3\u9664\u7dca\u6025\u505c\u6b62");
          return;
        }
      }
      throw new Error("toggle_failed");
    }
    const body = await response.json();
    applyDashboardPayload(body.payload);
    setStatus("hot", body.payload?.trading?.armed ? TEXT.armedSuccess : successText);
  } catch (_error) {
    setStatus("warn", TEXT.updateFailed);
  } finally {
    setControlBusy(false);
  }
}

async function triggerScan() {
  scanButton.dataset.busy = "true";
  scanButton.disabled = true;
  scanButton.textContent = scanButton.dataset.loadingLabel || TEXT.scanning;
  setControlBusy(true);
  setStatus("", latestTradingState.armed ? TEXT.scanningArmed : TEXT.scanning);
  try {
    const response = await fetch("/api/actions/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!response.ok) {
      throw new Error("scan_failed");
    }
    const body = await response.json();
    if (!tryApplyDashboardPayload(body.payload)) {
      await loadDashboard();
    }
    const submittedCount = body.execution_summary?.submitted_count || 0;
    const items = body.execution_summary?.items || [];
    const partialFailureCount = items.filter((item) => item.status === "partial_failure").length;
    const blockedCount = items.filter((item) => item.status === "risk_blocked").length;
    const preflightBlockedCount = items.filter((item) => item.status === "preflight_blocked").length;

    if (partialFailureCount > 0) {
      setStatus("warn", "\u6383\u63cf\u5b8c\u6210\uff0c\u4f46 Live \u767c\u751f partial failure\uff0c\u7cfb\u7d71\u5df2\u81ea\u52d5\u505c\u7528\u3002");
    } else if (submittedCount > 0) {
      setStatus("hot", `\u5df2\u9001\u51fa ${submittedCount} \u7b46 Live \u59d4\u8a17`);
    } else if (preflightBlockedCount > 0) {
      setStatus("warn", "\u6383\u63cf\u5b8c\u6210\uff0c\u4f46\u524d\u7f6e\u6aa2\u67e5\u672a\u901a\u904e\uff0c\u6c92\u6709\u9001\u55ae\u3002");
    } else if (blockedCount > 0) {
      setStatus("warn", `\u6383\u63cf\u5b8c\u6210\uff0c\u4f46\u6709 ${blockedCount} \u7b46\u6a5f\u6703\u88ab\u98a8\u63a7\u64cb\u4e0b\u3002`);
    } else {
      setStatus("hot", latestTradingState.armed ? TEXT.armedWatching : TEXT.scanDone);
    }
  } catch (_error) {
    try {
      await loadDashboard();
      setStatus("warn", TEXT.scanDone);
      return;
    } catch (_refreshError) {
      console.error("Failed to recover dashboard after scan error", _error, _refreshError);
    }
    setStatus("warn", TEXT.scanFailed);
  } finally {
    scanButton.dataset.busy = "false";
    scanButton.disabled = false;
    scanButton.textContent = scanButton.dataset.defaultLabel || TEXT.scanNow;
    setControlBusy(false);
  }
}

function startDashboardSyncLoop() {
  renderSyncCountdown();
  window.setInterval(() => {
    renderSyncCountdown();
    if (dashboardLoadInFlight) {
      return;
    }
    if (Date.now() < nextDashboardSyncAt) {
      return;
    }
    void loadDashboard().catch(() => {
      setStatus("warn", TEXT.scanFailed);
      resetDashboardSyncCountdown();
    });
  }, 200);
}

scanButton.addEventListener("click", () => {
  void triggerScan();
});

liveTradingToggle.addEventListener("click", () => {
  void toggleAction("/api/actions/trading/live", "\u66f4\u65b0 Live \u6a21\u5f0f\u4e2d", "Live \u6a21\u5f0f\u5df2\u66f4\u65b0");
});

autoExecuteToggle.addEventListener("click", () => {
  void toggleAction("/api/actions/trading/auto", "\u66f4\u65b0\u81ea\u52d5\u4e0b\u55ae\u4e2d", "\u81ea\u52d5\u4e0b\u55ae\u5df2\u66f4\u65b0");
});

killSwitchToggle.addEventListener("click", () => {
  void toggleAction("/api/actions/risk/kill-switch", "\u66f4\u65b0\u7dca\u6025\u505c\u6b62\u4e2d", "\u98a8\u63a7\u958b\u95dc\u5df2\u66f4\u65b0");
});

watchToggle.addEventListener("click", () => {
  void toggleWatch();
});

resetDashboardSyncCountdown();
void loadDashboard().catch(() => {
  setStatus("warn", TEXT.scanFailed);
  resetDashboardSyncCountdown();
});
startDashboardSyncLoop();
