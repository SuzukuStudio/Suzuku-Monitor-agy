import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';

interface InvocationMetric {
  stepIndex: number;
  createdAt: string;
  model: string;
  inputTokens: number;
  cacheReadTokens: number;
  outputTokens: number;
  contextTokens: number;
  cacheHitRate: number;
  toolCalls: string[];
  hasToolCalls: boolean;
  compacted: boolean;
  cacheMiss: boolean;
  modelSwitch?: boolean;
  handoffRebuild?: boolean;
  deltaFromSameModel?: number;
}

interface LastCompactionInfo {
  stepIndex: number;
  createdAt: string;
  beforeTokens: number;
  summaryTokens: number;
}

interface FreshnessTarget {
  model: string;
  role: string;
  contextTokens: number;
  lastActivityAt: string;
  idleSeconds: number;
}

interface ModelCacheEntry {
  model: string;
  role?: 'planner' | 'executor' | 'primary';
  calls: number;
  cacheMisses: number;
  contextTokens: number;
  inputTokens: number;
  cacheReadTokens: number;
  cacheHitRate: number;
  lastActivityAt: string;
  idleSeconds: number;
  stepIndex: number;
  contextLimit: number;
  isCurrent: boolean;
  staleAfterCompact?: boolean;
}

interface SessionSummary {
  conversationId: string;
  title: string;
  model: string;
  contextLimit: number;
  contextTokens: number;
  cacheReadTokens: number;
  inputTokens: number;
  cacheHitRate: number;
  idleSeconds: number;
  lastActivityAt: string;
  userTurns: number;
  modelCalls: number;
  isOrchestrator?: boolean;
  isSwarm?: boolean;
  swarmWorkers?: number;
  justCompacted?: boolean;
  isActive: boolean;
  isMostRecent: boolean;
  mtimeMs: number;
}

interface SwarmWorkerInfo {
  name: string;
  role: string;
  model: string;
  status: string;
  calls: number;
  contextTokens: number;
  uncachedInput: number;
  cacheRead: number;
  output: number;
}

interface SwarmSummary {
  title: string;
  workerCount: number;
  workingCount: number;
  boardMessages: number;
  totals: {
    calls: number;
    uncachedInput: number;
    cacheRead: number;
    output: number;
  };
  workers: SwarmWorkerInfo[];
}

interface SessionDetail extends SessionSummary {
  deltaTokens: number;
  latest: InvocationMetric | null;
  modelCaches?: ModelCacheEntry[];
  freshnessTarget?: FreshnessTarget | null;
  swarm?: SwarmSummary | null;
  compactionCount?: number;
  lastCompaction?: LastCompactionInfo | null;
  totals: {
    uncachedInput: number;
    cacheRead: number;
    output: number;
  };
  history: InvocationMetric[];
}

interface MonitorSettings {
  warnTokens: number;
  dangerTokens: number;
  cacheTtlSec: number;
  pollIntervalMs: number;
  desktopNotifyEnabled: boolean;
  notifyOnTurnEnd: boolean;
  notifyBeforeExpiry: boolean;
  remoteWebhookUrl: string;
  remoteWebhookToken: string;
  chatWarningOnDanger: boolean;
}

interface SnapshotPayload {
  generatedAt: string;
  activeConversationId: string | null;
  selectedConversationId: string | null;
  sessions: SessionSummary[];
  selected: SessionDetail | null;
  settings: MonitorSettings;
}

const DEFAULT_SETTINGS: MonitorSettings = {
  warnTokens: 80000,
  dangerTokens: 150000,
  cacheTtlSec: 300,
  pollIntervalMs: 2000,
  desktopNotifyEnabled: true,
  notifyOnTurnEnd: false,
  notifyBeforeExpiry: true,
  remoteWebhookUrl: '',
  remoteWebhookToken: '',
  chatWarningOnDanger: false,
};

let snapshot: SnapshotPayload | null = null;
let selectedMode: 'auto' | string = 'auto';
let inFlight = false;
let pollTimer: number | null = null;
let settingsOpen = false;
let settingsFeedback = '';
let lastSyncTimeText = '--:--:--';
let lastSyncError = '';
let lastSessionsSignature = '';
let lastFetchedMs = Date.now();

const rootEl = document.getElementById('app') as HTMLElement;
let monitorBodyEl: HTMLElement;
let settingsRootEl: HTMLElement;
let sessionSelectEl: HTMLSelectElement;
let syncBadgeEl: HTMLElement;

function esc(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtK(tokens: number): string {
  const n = Math.round(tokens);
  if (Math.abs(n) >= 1000) {
    return `${(n / 1000).toFixed(1)}k`;
  }
  return String(n);
}

function fmtSignedK(tokens: number): string {
  const prefix = tokens > 0 ? '+' : '';
  return `${prefix}${fmtK(tokens)}`;
}

function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  if (hrs > 0) {
    return `${hrs}:${pad(mins)}:${pad(secs)}`;
  }
  return `${pad(mins)}:${pad(secs)}`;
}

function fmtLocalTime(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function computeIdleFromIso(iso: string | undefined, fallbackSec: number): number {
  const parsedMs = iso ? Date.parse(iso) : NaN;
  if (!Number.isNaN(parsedMs)) {
    return Math.max(0, Math.floor((Date.now() - parsedMs) / 1000));
  }
  const extra = Math.max(0, Math.floor((Date.now() - lastFetchedMs) / 1000));
  return fallbackSec + extra;
}

function renderShell(): void {
  rootEl.innerHTML = `
    <div class="container">
      <div class="header">
        <div class="title-row">
          <h1 class="title">Suzuku Monitor</h1>
          <span id="sync-badge" class="sync-badge">
            <span class="sync-dot"></span>
            <span>接続中…</span>
          </span>
        </div>
        <div class="controls">
          <select id="session-select" class="select" aria-label="セッション選択">
            <option value="auto">自動追従 (アクティブ / 最新)</option>
          </select>
          <button id="btn-refresh" class="btn" type="button" title="今すぐ再読み込み">更新</button>
        </div>
      </div>
      <div id="monitor-body"></div>
      <div id="settings-root"></div>
    </div>
  `;

  monitorBodyEl = document.getElementById('monitor-body') as HTMLElement;
  settingsRootEl = document.getElementById('settings-root') as HTMLElement;
  sessionSelectEl = document.getElementById('session-select') as HTMLSelectElement;
  syncBadgeEl = document.getElementById('sync-badge') as HTMLElement;

  sessionSelectEl.addEventListener('change', () => {
    selectedMode = sessionSelectEl.value || 'auto';
    void refreshSnapshot();
  });

  document.getElementById('btn-refresh')?.addEventListener('click', () => {
    void refreshSnapshot();
  });
}

function updateSyncBadge(): void {
  if (!syncBadgeEl) return;
  if (lastSyncError) {
    syncBadgeEl.innerHTML = `<span class="sync-dot err"></span><span>${esc(lastSyncError)}</span>`;
  } else {
    syncBadgeEl.innerHTML = `<span class="sync-dot"></span><span>${esc(lastSyncTimeText)}</span>`;
  }
}

function updateSessionSelect(): void {
  if (!sessionSelectEl || !snapshot) return;
  if (document.activeElement === sessionSelectEl) return;

  const sessions = snapshot.sessions ?? [];
  const sig = `${selectedMode}|` + sessions.map((s) => `${s.conversationId}:${s.title}:${s.contextTokens}:${Boolean(s.justCompacted)}:${Boolean(s.isSwarm)}:${s.swarmWorkers ?? 0}:${s.isActive}`).join(',');
  if (sig === lastSessionsSignature) return;
  lastSessionsSignature = sig;

  const options = [
    `<option value="auto" ${selectedMode === 'auto' ? 'selected' : ''}>自動追従 (アクティブ / 最新)</option>`,
    ...sessions.map((s) => {
      const tag = s.isActive ? '● ' : '';
      const swarm = s.isSwarm ? '🐝 ' : '';
      const orch = s.isOrchestrator ? '⚡' : '';
      const ctxLabel = s.justCompacted ? `(${fmtK(s.contextTokens)} 要約済)` : `(${fmtK(s.contextTokens)})`;
      const label = `${tag}${swarm}${orch}${s.title} ${ctxLabel}`;
      const isSel = selectedMode === s.conversationId;
      return `<option value="${esc(s.conversationId)}" ${isSel ? 'selected' : ''}>${esc(label)}</option>`;
    }),
  ].join('');
  sessionSelectEl.innerHTML = options;
}

function renderChart(history: InvocationMetric[]): string {
  const items = history.slice(-30);
  if (items.length === 0) {
    return `<div class="status-note">まだモデル呼び出し履歴がありません</div>`;
  }

  const width = 460;
  const height = 112;
  const padTop = 14;
  const padBottom = 14;
  const plotH = height - padTop - padBottom;
  const maxCtx = Math.max(10000, ...items.map((d) => d.contextTokens));
  const slotW = width / items.length;
  const barW = Math.max(4, Math.min(18, slotW * 0.72));

  const bars = items
    .map((item, idx) => {
      const x = idx * slotW + (slotW - barW) / 2;
      const cachedH = (item.cacheReadTokens / maxCtx) * plotH;
      const uncachedH = (item.inputTokens / maxCtx) * plotH;
      const totalH = cachedH + uncachedH;
      const cachedY = padTop + plotH - cachedH;
      const uncachedY = cachedY - uncachedH;
      const topY = padTop + plotH - totalH;
      const hitPct = Math.round(item.cacheHitRate * 100);
      const timeLabel = item.createdAt ? item.createdAt.replace('T', ' ').slice(11, 19) : '';
      const tags = [
        item.cacheMiss ? 'Cache Miss' : '',
        item.compacted ? 'Compacted' : '',
        item.handoffRebuild ? 'Switch (ターン切替再構築)' : item.modelSwitch ? 'Switch' : '',
      ]
        .filter(Boolean)
        .join(', ');
      const tip = `Step ${item.stepIndex}${timeLabel ? ` (${timeLabel} UTC)` : ''} [${item.model || 'unknown'}]: ${fmtK(item.contextTokens)} (Cache ${hitPct}% / New ${fmtK(item.inputTokens)})${tags ? ` [${tags}]` : ''}`;

      let marker = '';
      const cx = (x + barW / 2).toFixed(1);
      const cy = Math.max(6, topY - 5).toFixed(1);
      if (item.cacheMiss) {
        marker = `<circle cx="${cx}" cy="${cy}" r="3.2" fill="#ef4444" />`;
      } else if (item.compacted) {
        marker = `<circle cx="${cx}" cy="${cy}" r="3.2" fill="#22c55e" />`;
      } else if (item.modelSwitch) {
        marker = `<circle cx="${cx}" cy="${cy}" r="2.8" fill="none" stroke="#60a5fa" stroke-width="1.6" />`;
      }

      return `<g>
        <title>${esc(tip)}</title>
        <rect x="${x.toFixed(1)}" y="${cachedY.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, cachedH).toFixed(1)}" rx="1.5" fill="#3b82f6" />
        <rect x="${x.toFixed(1)}" y="${uncachedY.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, uncachedH).toFixed(1)}" rx="1.5" fill="#f59e0b" />
        ${marker}
      </g>`;
    })
    .join('');

  return `<div class="chart-wrap">
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="コンテキスト推移">
      <line x1="0" y1="${padTop + plotH}" x2="${width}" y2="${padTop + plotH}" stroke="currentColor" stroke-opacity="0.18" stroke-width="1" />
      ${bars}
    </svg>
  </div>`;
}

function renderOrchestratorCard(caches: ModelCacheEntry[], settings: MonitorSettings): string {
  const ttl = Math.max(30, settings.cacheTtlSec);
  const rows = caches
    .map((m, idx) => {
      const roleBadge =
        m.role === 'planner'
          ? `<span class="pill pill-info">Planner</span>`
          : m.role === 'executor'
            ? `<span class="pill pill-neutral">Executor</span>`
            : '';

      if (m.staleAfterCompact) {
        return `<div class="orch-item orch-item-stale">
          <div class="orch-top">
            <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;">
              <span class="orch-model">${esc(m.model)}</span>
              ${roleBadge}
            </div>
            <div style="display:flex;gap:5px;align-items:center;">
              <span class="status-note">Compact済</span>
              <span class="pill pill-neutral">Compact でリセット済</span>
            </div>
          </div>
          <div class="bar-track bar-track-sm">
            <div class="bar-fresh-ok" style="width:0%"></div>
          </div>
          <div class="orch-meta">
            <span>旧 Context: <strong>${esc(fmtK(m.contextTokens))}</strong> · 次回呼出で新キャッシュ構築</span>
            <span>${m.calls} calls</span>
          </div>
        </div>`;
      }

      const idle = computeIdleFromIso(m.lastActivityAt, m.idleSeconds);
      const ratio = Math.min(1, idle / ttl);
      const barClass = ratio >= 1 ? 'bar-fresh-danger' : ratio >= 0.7 ? 'bar-fresh-warn' : 'bar-fresh-ok';
      const hitPct = Math.round(m.cacheHitRate * 100);
      const badge = m.isCurrent
        ? `<span class="pill pill-info">実行中</span>`
        : ratio >= 1
          ? `<span class="pill pill-danger">期限目安超過</span>`
          : `<span class="pill pill-neutral">待機中</span>`;

      return `<div class="orch-item">
        <div class="orch-top">
          <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;">
            <span class="orch-model">${esc(m.model)}</span>
            ${roleBadge}
          </div>
          <div style="display:flex;gap:5px;align-items:center;">
            <span class="status-note" id="orch-idle-${idx}">${esc(fmtClock(idle))} 経過</span>
            ${badge}
          </div>
        </div>
        <div class="bar-track bar-track-sm">
          <div id="orch-bar-${idx}" class="${barClass}" style="width:${Math.round(ratio * 100)}%"></div>
        </div>
        <div class="orch-meta">
          <span>Context: <strong>${esc(fmtK(m.contextTokens))}</strong> · Cache: <strong>${hitPct}%</strong></span>
          <span>${m.calls} calls${m.cacheMisses > 0 ? ` · ${m.cacheMisses} miss` : ''}</span>
        </div>
      </div>`;
    })
    .join('');

  return `<div class="card">
    <div class="card-header">
      <span class="card-label">Orchestrator モデル別キャッシュ</span>
      <span class="pill pill-info">${caches.length} models</span>
    </div>
    <div class="orch-list">${rows}</div>
  </div>`;
}

function updateLiveFreshnessDom(): void {
  const selected = snapshot?.selected;
  if (!selected) return;
  const settings = snapshot?.settings || DEFAULT_SETTINGS;
  const ttl = Math.max(30, settings.cacheTtlSec);

  if (!selected.justCompacted) {
    const ft = selected.freshnessTarget;
    const idle = computeIdleFromIso(ft?.lastActivityAt ?? selected.lastActivityAt, ft?.idleSeconds ?? selected.idleSeconds);
    const ratio = Math.min(1, idle / ttl);

    const timerEl = document.getElementById('live-idle-timer');
    const barEl = document.getElementById('live-fresh-bar');
    const pillEl = document.getElementById('live-fresh-pill');
    const staleBannerEl = document.getElementById('live-stale-banner');

    if (timerEl) timerEl.textContent = `${fmtClock(idle)} 経過`;
    if (barEl) {
      barEl.style.width = `${Math.round(ratio * 100)}%`;
      barEl.className = ratio >= 1 ? 'bar-fresh-danger' : ratio >= 0.7 ? 'bar-fresh-warn' : 'bar-fresh-ok';
    }
    if (pillEl) {
      if (ratio >= 1) {
        pillEl.className = 'pill pill-danger';
        pillEl.textContent = 'TTL 超過目安';
      } else if (ratio >= 0.7) {
        pillEl.className = 'pill pill-warn';
        pillEl.textContent = 'まもなく期限';
      } else {
        pillEl.className = 'pill pill-ok';
        pillEl.textContent = 'キャッシュ新鮮';
      }
    }
    if (staleBannerEl) {
      const ctx = ft?.contextTokens ?? selected.latest?.contextTokens ?? 0;
      staleBannerEl.style.display = idle >= ttl && ctx >= settings.warnTokens * 0.6 ? 'block' : 'none';
    }
  }

  const caches = selected.modelCaches ?? [];
  caches.forEach((m, idx) => {
    if (m.staleAfterCompact) return;
    const mIdle = computeIdleFromIso(m.lastActivityAt, m.idleSeconds);
    const mRatio = Math.min(1, mIdle / ttl);
    const mTimerEl = document.getElementById(`orch-idle-${idx}`);
    const mBarEl = document.getElementById(`orch-bar-${idx}`);
    if (mTimerEl) mTimerEl.textContent = `${fmtClock(mIdle)} 経過`;
    if (mBarEl) {
      mBarEl.style.width = `${Math.round(mRatio * 100)}%`;
      mBarEl.className = mRatio >= 1 ? 'bar-fresh-danger' : mRatio >= 0.7 ? 'bar-fresh-warn' : 'bar-fresh-ok';
    }
  });
}

function renderSwarmCard(swarm: SwarmSummary): string {
  const rows = swarm.workers
    .map((w) => {
      const isEnded = w.status === 'killed' || w.status === 'stopped';
      const statusPill =
        w.status === 'working'
          ? `<span class="pill pill-info">実行中</span>`
          : w.status === 'waiting'
            ? `<span class="pill pill-neutral">待機中</span>`
            : `<span class="pill pill-neutral">終了</span>`;
      return `<div class="orch-item${isEnded ? ' orch-item-stale' : ''}">
        <div class="orch-top">
          <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;">
            <span class="orch-model">@${esc(w.name)}</span>
            <span class="pill pill-neutral">${esc(w.model)}</span>
          </div>
          ${statusPill}
        </div>
        <div class="orch-meta">
          <span>Context: <strong>${esc(fmtK(w.contextTokens))}</strong> · New ${esc(fmtK(w.uncachedInput))}</span>
          <span>${w.calls} calls</span>
        </div>
      </div>`;
    })
    .join('');

  return `<div class="card">
    <div class="card-header">
      <span class="card-label">Swarm${swarm.title ? ` · ${esc(swarm.title)}` : ''}</span>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
        <span class="pill ${swarm.workingCount > 0 ? 'pill-info' : 'pill-neutral'}">${swarm.workingCount}/${swarm.workerCount} active</span>
        <span class="pill pill-neutral">${swarm.boardMessages} msgs</span>
      </div>
    </div>
    <div class="orch-meta">
      <span>Workers合計: <strong>${swarm.totals.calls}</strong> calls · New <strong>${esc(fmtK(swarm.totals.uncachedInput))}</strong> · Cache <strong>${esc(fmtK(swarm.totals.cacheRead))}</strong> · Out <strong>${esc(fmtK(swarm.totals.output))}</strong></span>
    </div>
    ${rows ? `<div class="orch-list">${rows}</div>` : `<div class="status-note">ワーカー未追加</div>`}
  </div>`;
}

function renderMonitorBody(): void {
  if (!monitorBodyEl) return;
  const settings = snapshot?.settings ?? DEFAULT_SETTINGS;
  const selected = snapshot?.selected ?? null;

  if (!snapshot) {
    monitorBodyEl.innerHTML = `<div class="card empty-state">セッション情報を読み込み中…</div>`;
    return;
  }
  const isJustCompacted = Boolean(selected?.justCompacted && selected?.lastCompaction);
  if (!selected || (!selected.latest && !isJustCompacted)) {
    monitorBodyEl.innerHTML = `<div class="card empty-state">まだ計測可能なモデル呼び出しがありません。<br />会話で1ターン実行するとトークン数とキャッシュ状況が表示されます。</div>`;
    return;
  }

  const latest = selected.latest;
  const lc = selected.lastCompaction;
  const limit = selected.contextLimit || 1_000_000;
  const ttl = Math.max(30, settings.cacheTtlSec);
  const modelLabel = selected.model || latest?.model || '';
  const caches = selected.modelCaches ?? [];
  const orchHtml = selected.isOrchestrator && caches.length >= 2 ? renderOrchestratorCard(caches, settings) : '';
  const swarmHtml = selected.isSwarm && selected.swarm ? renderSwarmCard(selected.swarm) : '';

  let contextCardHtml = '';
  let freshnessCardHtml = '';
  let cacheTileVal = '--';
  let cacheTileSub = '次回発言で再計測';
  let deltaTileVal = '-';
  let deltaTileSub = 'Compact 完了';

  if (isJustCompacted && lc) {
    const compactGaugeMax = Math.max(settings.dangerTokens, lc.summaryTokens);
    const compactBarPct = Math.min(100, (lc.summaryTokens / compactGaugeMax) * 100).toFixed(1);
    const savedTokens = Math.max(0, lc.beforeTokens - lc.summaryTokens);
    deltaTileVal = savedTokens > 0 ? `-${fmtK(savedTokens)}` : '0';
    deltaTileSub = `要約 ${fmtK(lc.summaryTokens)}`;

    contextCardHtml = `
      <div class="card">
        <div class="card-header">
          <span class="card-label">Context Usage</span>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            ${selected.isSwarm ? `<span class="pill pill-info">Swarm</span>` : ''}
            ${selected.isOrchestrator ? `<span class="pill pill-info">Orchestrator</span>` : ''}
            ${modelLabel ? `<span class="pill pill-neutral">${esc(modelLabel)}</span>` : ''}
            <span class="pill pill-ok">Compact 完了</span>
          </div>
        </div>
        <div class="hero-row">
          <span class="hero-value">~${esc(fmtK(lc.summaryTokens))} <span style="font-size:13px;font-weight:500;">tokens</span></span>
          <span class="hero-sub">旧 ${esc(fmtK(lc.beforeTokens))} → 要約 ${esc(fmtK(lc.summaryTokens))} · 次回発言で再計測</span>
        </div>
        <div class="bar-track" title="Compact 要約サイズ目安">
          <div class="bar-fresh-ok" style="width:${compactBarPct}%"></div>
        </div>
        <div class="legend">
          <div class="legend-items">
            <span><span class="dot" style="background:#22c55e"></span>要約サイズ目安: ${esc(fmtK(lc.summaryTokens))}</span>
            <span>圧縮前: ${esc(fmtK(lc.beforeTokens))}</span>
          </div>
          <span>${selected.isActive ? '● 現在の会話' : '履歴セッション'}</span>
        </div>
      </div>
    `;

    freshnessCardHtml = `
      <div class="card">
        <div class="card-header">
          <span class="card-label">Cache Freshness (放置タイマー)</span>
          <span class="pill pill-info">次回再構築</span>
        </div>
        <div class="hero-row">
          <span class="hero-value" style="font-size:20px;">Compact 完了</span>
          <span class="hero-sub">次の発言で新キャッシュを作成</span>
        </div>
        <div class="bar-track">
          <div class="bar-fresh-ok" style="width:0%"></div>
        </div>
      </div>
    `;
  } else if (latest) {
    const ctx = latest.contextTokens;
    const limitPct = ((ctx / limit) * 100).toFixed(1);
    const gaugeMax = Math.max(settings.dangerTokens, ctx);
    const cachedPctBar = ((latest.cacheReadTokens / gaugeMax) * 100).toFixed(1);
    const uncachedPctBar = ((latest.inputTokens / gaugeMax) * 100).toFixed(1);

    const statusBadge =
      ctx >= settings.dangerTokens
        ? `<span class="pill pill-danger">Compact 推奨</span>`
        : ctx >= settings.warnTokens
          ? `<span class="pill pill-warn">注意</span>`
          : `<span class="pill pill-ok">安全</span>`;

    const ft = selected.freshnessTarget;
    const idle = computeIdleFromIso(ft?.lastActivityAt ?? selected.lastActivityAt, ft?.idleSeconds ?? selected.idleSeconds);
    const freshnessCtx = ft?.contextTokens ?? ctx;
    const freshRatio = Math.min(1, idle / ttl);
    const freshBarClass = freshRatio >= 1 ? 'bar-fresh-danger' : freshRatio >= 0.7 ? 'bar-fresh-warn' : 'bar-fresh-ok';
    const freshPill =
      freshRatio >= 1
        ? `<span id="live-fresh-pill" class="pill pill-danger">TTL 超過目安</span>`
        : freshRatio >= 0.7
          ? `<span id="live-fresh-pill" class="pill pill-warn">まもなく期限</span>`
          : `<span id="live-fresh-pill" class="pill pill-ok">キャッシュ新鮮</span>`;

    const showStaleBanner = idle >= ttl && freshnessCtx >= settings.warnTokens * 0.6;
    const freshnessTargetLabel =
      selected.isOrchestrator && ft?.model
        ? `基準: ${ft.role === 'planner' ? 'Planner · ' : ''}${ft.model} · `
        : '';
    const cachePct = Math.round(latest.cacheHitRate * 100);
    cacheTileVal = `${cachePct}%`;
    cacheTileSub = `${fmtK(latest.cacheReadTokens)} / ${fmtK(ctx)}`;
    deltaTileVal = fmtSignedK(selected.deltaTokens);
    deltaTileSub = `out ${fmtK(latest.outputTokens)}`;

    contextCardHtml = `
      <div class="card">
        <div class="card-header">
          <span class="card-label">Context Usage</span>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            ${selected.isSwarm ? `<span class="pill pill-info">Swarm</span>` : ''}
            ${selected.isOrchestrator ? `<span class="pill pill-info">Orchestrator</span>` : ''}
            <span class="pill pill-neutral">${esc(modelLabel || 'unknown')}</span>
            ${statusBadge}
          </div>
        </div>
        <div class="hero-row">
          <span class="hero-value">${esc(fmtK(ctx))} <span style="font-size:13px;font-weight:500;">tokens</span></span>
          <span class="hero-sub">モデル上限 ${esc(fmtK(limit))} の ${limitPct}% · 推奨上限 ${esc(fmtK(settings.dangerTokens))}</span>
        </div>
        <div class="bar-track" title="青: キャッシュヒット / 橙: 新規入力">
          <div class="bar-cached" style="width:${cachedPctBar}%"></div>
          <div class="bar-uncached" style="width:${uncachedPctBar}%"></div>
        </div>
        <div class="legend">
          <div class="legend-items">
            <span><span class="dot" style="background:#3b82f6"></span>Cache Hit: ${esc(fmtK(latest.cacheReadTokens))}</span>
            <span><span class="dot" style="background:#f59e0b"></span>Uncached: ${esc(fmtK(latest.inputTokens))}</span>
          </div>
          <span>${selected.isActive ? '● 現在の会話' : '履歴セッション'}</span>
        </div>
      </div>
    `;

    freshnessCardHtml = `
      <div class="card">
        <div class="card-header">
          <span class="card-label">Cache Freshness (放置タイマー)</span>
          ${freshPill}
        </div>
        <div class="hero-row">
          <span id="live-idle-timer" class="hero-value" style="font-size:20px;">${esc(fmtClock(idle))} 経過</span>
          <span class="hero-sub">${esc(freshnessTargetLabel)}TTL 目安: ${esc(fmtClock(ttl))}</span>
        </div>
        <div class="bar-track">
          <div id="live-fresh-bar" class="${freshBarClass}" style="width:${Math.round(freshRatio * 100)}%"></div>
        </div>
        ${
          latest.cacheMiss
            ? `<div class="banner banner-danger">🔥 直前ステップで Cache Miss を検知しました。続ける前に <code>/compact</code> を検討してください。</div>`
            : ''
        }
        <div id="live-stale-banner" class="banner banner-warn" style="display:${showStaleBanner ? 'block' : 'none'};">
          ⏱️ キャッシュ切れの可能性があります（${esc(fmtK(freshnessCtx))}）。次の発言前に <code>/compact</code> または新規会話で枠を節約できます。
        </div>
      </div>
    `;
  }

  monitorBodyEl.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:10px;">
      ${contextCardHtml}
      ${freshnessCardHtml}
      ${orchHtml}
      ${swarmHtml}

      <div class="metrics-grid">
        <div class="metric-tile">
          <span class="metric-title">キャッシュ率</span>
          <span class="metric-val">${esc(cacheTileVal)}</span>
          <span class="metric-sub">${esc(cacheTileSub)}</span>
        </div>
        <div class="metric-tile">
          <span class="metric-title">同モデル増分</span>
          <span class="metric-val">${esc(deltaTileVal)}</span>
          <span class="metric-sub">${esc(deltaTileSub)}</span>
        </div>
        <div class="metric-tile">
          <span class="metric-title">セッション累計</span>
          <span class="metric-val">${esc(fmtK(selected.totals.uncachedInput))}</span>
          <span class="metric-sub">cache節約 ${esc(fmtK(selected.totals.cacheRead))}</span>
        </div>
        <div class="metric-tile">
          <span class="metric-title">やり取り数</span>
          <span class="metric-val">${selected.userTurns} turns</span>
          <span class="metric-sub">${selected.modelCalls} model calls</span>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-label">Context 推移 (直近 ${Math.min(30, selected.history.length)} 呼出)</span>
          <div class="legend-items" style="font-size:11px;">
            <span><span class="dot" style="background:#ef4444"></span>Miss</span>
            <span><span class="dot" style="background:#22c55e"></span>Compact</span>
            <span><span class="dot" style="border:1.5px solid #60a5fa;background:transparent"></span>Switch</span>
          </div>
        </div>
        ${renderChart(selected.history)}
      </div>
    </div>
  `;
}

function renderSettings(): void {
  if (!settingsRootEl) return;
  const settings = snapshot?.settings ?? DEFAULT_SETTINGS;

  settingsRootEl.innerHTML = `
    <div class="card">
      <button id="btn-toggle-settings" class="settings-toggle" type="button">
        <span class="card-label">設定 (しきい値 · デスクトップ通知 · スマホ連携)</span>
        <span class="status-note">${settingsOpen ? '▲ 閉じる' : '▼ 開く'}</span>
      </button>
      ${
        settingsOpen
          ? `<form id="settings-form" class="settings-grid">
              <div class="settings-divider">通知 &amp; Remote Control 連携</div>
              <label class="checkbox-row">
                <input id="set-desktop" type="checkbox" ${settings.desktopNotifyEnabled ? 'checked' : ''} />
                デスクトップ通知を有効にする (Linux / macOS / Windows)
              </label>
              <label class="checkbox-row">
                <input id="set-pre-expiry" type="checkbox" ${settings.notifyBeforeExpiry ? 'checked' : ''} />
                キャッシュ切れ約1分前に事前アラートを出す (Orchestrator 各モデル対応)
              </label>
              <label class="checkbox-row">
                <input id="set-turn-end" type="checkbox" ${settings.notifyOnTurnEnd ? 'checked' : ''} />
                ターン完了ごとにも通知する (低優先度)
              </label>
              <div class="field field-full">
                <label for="set-webhook-url">スマホ / Remote Control 通知 Webhook URL (ntfy / Discord / Slack / JSON)</label>
                <input id="set-webhook-url" type="url" placeholder="https://ntfy.sh/your-secret-topic" value="${esc(settings.remoteWebhookUrl)}" />
                <span class="status-note">PC はデスクトップ通知、スマホは ntfy.sh（ブラウザ Web Push / PWA 対応）や Discord / Slack Webhook で受信できます</span>
              </div>
              <div class="field field-full">
                <label for="set-webhook-token">Webhook Bearer Token (任意)</label>
                <input id="set-webhook-token" type="password" placeholder="認証が必要な場合のみ入力" value="${esc(settings.remoteWebhookToken)}" />
              </div>
              <label class="checkbox-row">
                <input id="set-chat-warn" type="checkbox" ${settings.chatWarningOnDanger ? 'checked' : ''} />
                Remote Control 画面向けチャット緊急警告 (Cache Miss / Compact推奨時のみ・約20トークン消費)
              </label>

              <div class="settings-divider">しきい値 &amp; ポーリング</div>
              <div class="field">
                <label for="set-warn">注意しきい値 (k tokens)</label>
                <input id="set-warn" type="number" min="5" max="900" step="5" value="${Math.round(settings.warnTokens / 1000)}" />
              </div>
              <div class="field">
                <label for="set-danger">Compact推奨 (k tokens)</label>
                <input id="set-danger" type="number" min="10" max="950" step="5" value="${Math.round(settings.dangerTokens / 1000)}" />
              </div>
              <div class="field">
                <label for="set-ttl">キャッシュ TTL 目安 (秒)</label>
                <input id="set-ttl" type="number" min="30" max="86400" step="30" value="${settings.cacheTtlSec}" />
              </div>
              <div class="field">
                <label for="set-poll">自動更新間隔 (ミリ秒)</label>
                <input id="set-poll" type="number" min="1000" max="60000" step="500" value="${settings.pollIntervalMs}" />
              </div>

              <div class="settings-footer">
                <span id="settings-feedback" class="status-note">${esc(settingsFeedback || 'トークン消費ゼロで監視します')}</span>
                <div class="settings-btns">
                  <button id="btn-test-notify" class="btn" type="button">テスト通知を送信</button>
                  <button class="btn btn-primary" type="submit">保存</button>
                </div>
              </div>
            </form>`
          : ''
      }
    </div>
  `;

  bindSettingsEvents();
}

function bindSettingsEvents(): void {
  document.getElementById('btn-toggle-settings')?.addEventListener('click', () => {
    settingsOpen = !settingsOpen;
    settingsFeedback = '';
    renderSettings();
  });

  const form = document.getElementById('settings-form') as HTMLFormElement | null;
  form?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    await saveCurrentFormSettings('設定を保存しました');
  });

  document.getElementById('btn-test-notify')?.addEventListener('click', async () => {
    const fb = document.getElementById('settings-feedback');
    if (fb) fb.textContent = '送信テスト中…';
    await saveCurrentFormSettings('');
    try {
      const res = await callToolWithTimeout('app_test_notification', {}, 8000);
      const sc = res.structuredContent as
        | { desktop?: { attempted?: boolean; sent?: boolean; detail?: string }; webhook?: { attempted?: boolean; sent?: boolean; detail?: string } }
        | undefined;
      if (sc) {
        const parts: string[] = [];
        if (sc.desktop?.attempted) parts.push(`PC: ${sc.desktop.sent ? 'OK' : sc.desktop.detail}`);
        if (sc.webhook?.attempted) parts.push(`Webhook: ${sc.webhook.sent ? 'OK' : sc.webhook.detail}`);
        settingsFeedback = parts.join(' / ') || '通知チャンネルが有効になっていません';
      } else {
        settingsFeedback = 'テスト送信を実行しました';
      }
    } catch {
      settingsFeedback = 'テスト通知の送信に失敗しました';
    }
    const fbAfter = document.getElementById('settings-feedback');
    if (fbAfter) fbAfter.textContent = settingsFeedback;
  });
}

async function saveCurrentFormSettings(successText: string): Promise<void> {
  const desktopEl = document.getElementById('set-desktop') as HTMLInputElement | null;
  if (!desktopEl) return;
  const preExpiryEl = document.getElementById('set-pre-expiry') as HTMLInputElement;
  const turnEndEl = document.getElementById('set-turn-end') as HTMLInputElement;
  const webhookUrlEl = document.getElementById('set-webhook-url') as HTMLInputElement;
  const webhookTokenEl = document.getElementById('set-webhook-token') as HTMLInputElement;
  const chatWarnEl = document.getElementById('set-chat-warn') as HTMLInputElement;
  const warnEl = document.getElementById('set-warn') as HTMLInputElement;
  const dangerEl = document.getElementById('set-danger') as HTMLInputElement;
  const ttlEl = document.getElementById('set-ttl') as HTMLInputElement;
  const pollEl = document.getElementById('set-poll') as HTMLInputElement;

  try {
    const res = await callToolWithTimeout(
      'app_update_settings',
      {
        desktopNotifyEnabled: desktopEl.checked,
        notifyBeforeExpiry: preExpiryEl.checked,
        notifyOnTurnEnd: turnEndEl.checked,
        remoteWebhookUrl: webhookUrlEl.value.trim(),
        remoteWebhookToken: webhookTokenEl.value.trim(),
        chatWarningOnDanger: chatWarnEl.checked,
        warnTokens: Math.round(Number(warnEl.value) * 1000),
        dangerTokens: Math.round(Number(dangerEl.value) * 1000),
        cacheTtlSec: Math.round(Number(ttlEl.value)),
        pollIntervalMs: Math.round(Number(pollEl.value)),
      },
      8000,
    );
    if (res.structuredContent) {
      applySnapshot(res.structuredContent as unknown as SnapshotPayload, false);
    }
    restartPolling();
    if (successText) {
      settingsFeedback = successText;
      const fb = document.getElementById('settings-feedback');
      if (fb) fb.textContent = successText;
    }
  } catch {
    settingsFeedback = '設定の保存に失敗しました';
    const fb = document.getElementById('settings-feedback');
    if (fb) fb.textContent = settingsFeedback;
  }
}

function applySnapshot(next: SnapshotPayload, rerenderSettings = false): void {
  snapshot = next;
  lastFetchedMs = Date.now();
  lastSyncTimeText = fmtLocalTime(new Date());
  lastSyncError = '';
  updateSyncBadge();
  updateSessionSelect();
  renderMonitorBody();
  if (rerenderSettings) {
    renderSettings();
  }
}

async function callToolWithTimeout(
  name: string,
  args: Record<string, unknown>,
  timeoutMs: number,
): Promise<Awaited<ReturnType<App['callServerTool']>>> {
  return await Promise.race([
    app.callServerTool({ name, arguments: args }),
    new Promise<never>((_, reject) =>
      window.setTimeout(() => reject(new Error('timeout')), timeoutMs),
    ),
  ]);
}

async function refreshSnapshot(): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  try {
    const args: Record<string, unknown> = {};
    if (selectedMode !== 'auto') {
      args.conversationId = selectedMode;
    }
    const res = await callToolWithTimeout('app_get_context_snapshot', args, 8000);
    if (res.structuredContent) {
      applySnapshot(res.structuredContent as unknown as SnapshotPayload, false);
    }
  } catch (err) {
    lastSyncError = err instanceof Error && err.message === 'timeout' ? '応答タイムアウト' : '更新エラー';
    updateSyncBadge();
  } finally {
    inFlight = false;
  }
}

function restartPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
  }
  const interval = Math.max(1000, snapshot?.settings?.pollIntervalMs ?? DEFAULT_SETTINGS.pollIntervalMs);
  pollTimer = window.setInterval(() => {
    void refreshSnapshot();
  }, interval);
}

const app = new App({ name: 'Suzuku Monitor', version: '1.2.0' }, {});

// Register handlers BEFORE connect() so initial tool payloads are never missed.
app.ontoolinput = ({ arguments: args }) => {
  if (args && typeof args.conversationId === 'string' && args.conversationId) {
    selectedMode = args.conversationId;
  }
};

app.ontoolresult = (result) => {
  if (result?.structuredContent) {
    applySnapshot(result.structuredContent as unknown as SnapshotPayload, !settingsOpen);
    restartPolling();
  }
};

app.onhostcontextchanged = (ctx) => {
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
};

async function init(): Promise<void> {
  renderShell();
  renderMonitorBody();
  renderSettings();

  await app.connect();

  const ctx = app.getHostContext();
  if (ctx?.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx?.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }

  if (!snapshot) {
    await refreshSnapshot();
    renderSettings();
  } else {
    renderMonitorBody();
  }
  restartPolling();

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      void refreshSnapshot();
    }
  });
  window.addEventListener('focus', () => {
    void refreshSnapshot();
  });

  window.setInterval(() => {
    updateLiveFreshnessDom();
  }, 1000);
}

void init();
