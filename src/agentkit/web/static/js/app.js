function readUiConfig() {
  const node = document.getElementById("ui-config");
  if (!node) return {};
  try {
    return JSON.parse(node.textContent || "{}");
  } catch {
    return {};
  }
}

const UI_CONFIG = readUiConfig();
const AGENT_DIRECTORY = (() => {
  const node = document.getElementById("agent-directory");
  if (!node) return [];
  try {
    return JSON.parse(node.textContent || "[]");
  } catch {
    return [];
  }
})();
const DEMO_PROMPT = UI_CONFIG.demo_prompt || "Rank the top 3 candidates for JOB-001 and explain why.";
let pendingApproval = null;
let pendingInput = null;
let currentConversationId = null;
let conversationCache = [];
let chatBusy = false;
const HISTORY_COLLAPSED_KEY = "agentkit:chat-history-collapsed";

// Progressive tab enhancement: without JavaScript the anchors still navigate
// to fully visible sections; once initialized, the same markup follows the
// ARIA tabs pattern with roving focus and URL-backed state.
function bindTabs() {
  document.querySelectorAll("[data-tabs]").forEach((root) => {
    if (root.dataset.tabsInitialized === "true") return;

    const tabList = root.querySelector("[data-tab-list]");
    const tabs = tabList ? Array.from(tabList.querySelectorAll("[data-tab]")) : [];
    const panels = Array.from(root.querySelectorAll("[data-tab-panel]"));
    const entries = tabs.map((tab) => {
      const href = tab.getAttribute("href") || "";
      const panel = href.startsWith("#") ? document.getElementById(href.slice(1)) : null;
      if (!panel || !root.contains(panel) || !panel.matches("[data-tab-panel]")) return null;
      return { tab, panel };
    });

    const uniquePanels = new Set(entries.filter(Boolean).map((entry) => entry.panel));
    if (
      !tabList ||
      !entries.length ||
      entries.some((entry) => !entry) ||
      entries.length !== panels.length ||
      uniquePanels.size !== panels.length
    ) {
      return;
    }

    const validEntries = entries;
    const entryFromHash = () => {
      if (!window.location.hash) return null;
      let targetId = "";
      try {
        targetId = decodeURIComponent(window.location.hash.slice(1));
      } catch {
        return null;
      }
      const target = document.getElementById(targetId);
      const panel = target?.matches("[data-tab-panel]")
        ? target
        : target?.closest("[data-tab-panel]");
      if (!panel || !root.contains(panel)) return null;
      const entry = validEntries.find((candidate) => candidate.panel === panel);
      return entry ? { entry, target } : null;
    };

    const activate = (entry, { focus = false, updateHash = false } = {}) => {
      validEntries.forEach((candidate) => {
        const selected = candidate === entry;
        candidate.tab.setAttribute("aria-selected", String(selected));
        candidate.tab.setAttribute("tabindex", selected ? "0" : "-1");
        candidate.panel.hidden = !selected;
      });
      root.dataset.activeTab = entry.panel.id;
      if (updateHash) {
        const url = new URL(window.location.href);
        url.hash = entry.panel.id;
        window.history.replaceState(null, "", url.toString());
      }
      if (focus) {
        entry.tab.focus();
        entry.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    };

    const activateHashState = (state) => {
      activate(state.entry);
      if (state.target && state.target !== state.entry.panel) {
        window.requestAnimationFrame(() => {
          state.target.scrollIntoView({ block: "start", inline: "nearest" });
        });
      }
    };

    tabList.setAttribute("role", "tablist");
    tabList.setAttribute("aria-orientation", "horizontal");
    validEntries.forEach(({ tab, panel }) => {
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", panel.id);
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
      panel.setAttribute("tabindex", "0");
    });

    root.dataset.tabsInitialized = "true";
    root.dataset.tabsEnhanced = "true";
    const hashState = entryFromHash();
    const defaultEntry =
      validEntries.find(({ panel }) => panel.id === root.dataset.tabsDefault) || validEntries[0];
    if (hashState) activateHashState(hashState);
    else activate(defaultEntry);

    validEntries.forEach((entry) => {
      entry.tab.addEventListener("click", (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        activate(entry, { updateHash: true });
      });
    });

    tabList.addEventListener("keydown", (event) => {
      const currentTab = event.target.closest("[data-tab]");
      const currentIndex = validEntries.findIndex(({ tab }) => tab === currentTab);
      if (currentIndex < 0) return;

      let nextIndex = -1;
      if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % validEntries.length;
      if (event.key === "ArrowLeft") {
        nextIndex = (currentIndex - 1 + validEntries.length) % validEntries.length;
      }
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = validEntries.length - 1;
      if (event.key === "Enter" || event.key === " ") nextIndex = currentIndex;
      if (nextIndex < 0) return;

      event.preventDefault();
      activate(validEntries[nextIndex], { focus: true, updateHash: true });
    });

    window.addEventListener("hashchange", () => {
      const nextState = entryFromHash();
      if (nextState) activateHashState(nextState);
    });
  });
}

function syncChatComposerState() {
  const form = document.getElementById("chat-form");
  if (!form) return;
  const input = form.querySelector("[data-chat-input]");
  const submit = form.querySelector('button[type="submit"]');
  const demo = form.querySelector("[data-chat-demo]");
  if (submit) submit.disabled = chatBusy || !input?.value.trim();
  if (demo) demo.disabled = chatBusy;
  form.setAttribute("aria-busy", String(chatBusy));
}

// Disable both actions while a turn is running so a previous turn cannot be
// re-triggered before it finishes. The textarea remains editable as a draft.
function setChatBusy(busy) {
  chatBusy = busy;
  syncChatComposerState();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// --- Lightweight, XSS-safe markdown rendering for assistant replies. ---
// All input is HTML-escaped first; formatting tags are then layered onto the
// already-escaped text via controlled regexes, so untrusted LLM/model output
// can never inject live HTML. Supports headings, lists, fenced/inline code,
// bold, italic, and http(s)/mailto links.
function renderMarkdownInline(escaped) {
  let out = escaped.replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`);
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    const trimmed = url.trim();
    const safe = /^(https?:|mailto:)/i.test(trimmed) ? trimmed : "#";
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>");
  return out;
}

function renderMarkdown(raw) {
  const lines = escapeHtml(String(raw ?? "")).split("\n");
  const html = [];
  let listType = null;
  const closeList = () => {
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
  };
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      closeList();
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1;
      html.push(`<pre class="md-code"><code>${code.join("\n")}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      html.push(`<h${level} class="md-h">${renderMarkdownInline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.*)$/);
    if (unordered) {
      if (listType !== "ul") {
        closeList();
        html.push('<ul class="md-list">');
        listType = "ul";
      }
      html.push(`<li>${renderMarkdownInline(unordered[1])}</li>`);
      i += 1;
      continue;
    }
    const ordered = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ordered) {
      if (listType !== "ol") {
        closeList();
        html.push('<ol class="md-list">');
        listType = "ol";
      }
      html.push(`<li>${renderMarkdownInline(ordered[1])}</li>`);
      i += 1;
      continue;
    }
    if (!line.trim()) {
      closeList();
      i += 1;
      continue;
    }
    closeList();
    const para = [line];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${para.map(renderMarkdownInline).join("<br>")}</p>`);
  }
  closeList();
  return html.join("");
}

function thinkBlockHtml(content) {
  return `<details class="think-block"><summary>Thinking</summary><div class="think-body">${renderMarkdown(content)}</div></details>`;
}

// Split an assistant reply into collapsible <think> blocks and markdown-rendered
// answer segments. Handles complete blocks as well as a trailing unclosed
// <think> (e.g. mid-stream or truncated output).
function renderAssistantHtml(raw) {
  const text = String(raw ?? "");
  const parts = [];
  const thinkRe = /<think\b[^>]*>([\s\S]*?)<\/think>/gi;
  let lastIndex = 0;
  let match;
  while ((match = thinkRe.exec(text)) !== null) {
    const before = text.slice(lastIndex, match.index);
    if (before.trim()) parts.push(renderMarkdown(before));
    if (match[1].trim()) parts.push(thinkBlockHtml(match[1]));
    lastIndex = thinkRe.lastIndex;
  }
  let rest = text.slice(lastIndex);
  // Some reasoning model chat templates can emit a stray closing tag on later
  // turns. Do not render that implementation artifact as user-visible text.
  rest = rest.replace(/^[\s\S]*?<\/think\s*>/i, "");
  const openMatch = rest.match(/<think\b[^>]*>/i);
  if (openMatch) {
    const before = rest.slice(0, openMatch.index);
    if (before.trim()) parts.push(renderMarkdown(before));
    const thinking = rest.slice(openMatch.index + openMatch[0].length);
    if (thinking.trim()) parts.push(thinkBlockHtml(thinking));
    rest = "";
  }
  if (rest.trim()) parts.push(renderMarkdown(rest));
  return parts.length ? parts.join("") : renderMarkdown(text);
}

// Replace a streaming bubble's plain-text <p> with the formatted answer
// (collapsible thinking + markdown) once the full reply is available.
function finalizeAssistantBubble(bubble, text) {
  if (!bubble || !bubble.p) return;
  const body = document.createElement("div");
  body.className = "chat-body";
  body.innerHTML = renderAssistantHtml(text);
  bubble.p.replaceWith(body);
  bubble.p = body;
}

// Canonical browser -> server shape: identity hint plus a single context object.
// The server owns trusted identity/RBAC and routes the selected agent to
// answer-only memory or the governed action graph.
function collectChatPayload(message, extraContext = {}) {
  const context = {
    agent: "general_agent",
    message,
    ...extraContext,
  };
  if (pendingInput?.agent === "general_agent") {
    context.skill = pendingInput.skill_name;
    context.skill_args = { ...(pendingInput.arguments || {}) };
  }
  if (currentConversationId) context.conversation_id = currentConversationId;
  return {
    user_id: UI_CONFIG.default_user_id || "",
    context,
  };
}

function getSelectedAgentName() {
  return "general_agent";
}

function getAgentCard(agentName) {
  return Array.from(document.querySelectorAll("[data-agent-card]")).find((card) => card.dataset.agentCard === agentName);
}

function getSelectedAgentLabel() {
  const selected = getSelectedAgentName();
  const card = getAgentCard(selected);
  return card?.querySelector("strong")?.textContent?.trim() || agentLabel(selected);
}

function bindPrimaryNavigation() {
  const toggle = document.querySelector("[data-mobile-navigation-toggle]");
  const navigation = document.getElementById("primary-navigation");
  if (!toggle || !navigation) return;

  const setOpen = (open, restoreFocus = false) => {
    document.body.classList.toggle("ak-mobile-nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (restoreFocus) toggle.focus();
  };

  toggle.addEventListener("click", () => {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });
  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false, true);
    }
  });
}

function agentLabel(agentName) {
  const entry = AGENT_DIRECTORY.find((agent) => agent.name === agentName);
  return entry?.label || String(agentName || "General Agent").replaceAll("_", " ");
}

function getSelectedAgentDemoPrompt() {
  const selected = getSelectedAgentName();
  const card = getAgentCard(selected);
  return card?.dataset.demoPrompt || UI_CONFIG.demo_prompts?.[selected] || DEMO_PROMPT;
}

function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

async function postChat(payload) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": getCsrfToken(),
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let message = `Request failed with ${response.status}`;
    try {
      const data = await response.json();
      message = data.error || message;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  return response.json();
}

function parseSseFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: {} };
  }
}

// Stream an SSE endpoint. Returns the parsed `final` payload (or null). Tokens
// are delivered to handlers.onToken as they arrive; handlers.onError captures a
// server-side error frame. Throws when the response is not a usable event
// stream so callers can fall back to the blocking JSON endpoint.
async function streamSse(url, payload, handlers = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
    body: JSON.stringify(payload),
  });
  const contentType = response.headers.get("Content-Type") || "";
  if (!response.ok || !contentType.includes("text/event-stream") || !response.body) {
    let message = `Request failed with ${response.status}`;
    try {
      const data = await response.json();
      message = data.error || message;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalData = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;
      if (parsed.event === "token") handlers.onToken?.(parsed.data.delta || "");
      else if (parsed.event === "final") {
        finalData = parsed.data;
        handlers.onFinal?.(parsed.data);
      } else if (parsed.event === "error") {
        handlers.onError?.(parsed.data.error || "stream error", parsed.data);
      }
    }
  }
  return finalData;
}

function scrollChatToBottom() {
  const thread = document.getElementById("chat-thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
}

// A streaming assistant bubble whose `<p>` text is appended to as tokens arrive.
function addLiveAssistantMessage(labelOverride = "") {
  const thread = document.getElementById("chat-thread");
  if (!thread) return null;
  thread.querySelector(".conversation-notice")?.remove();
  const node = document.createElement("div");
  node.className = "chat-message assistant";
  const span = document.createElement("span");
  span.textContent = labelOverride || getSelectedAgentLabel();
  const paragraph = document.createElement("p");
  node.appendChild(span);
  node.appendChild(paragraph);
  thread.appendChild(node);
  scrollChatToBottom();
  return { node, p: paragraph };
}

function resetChatThread(greeting) {
  const thread = document.getElementById("chat-thread");
  if (!thread) return;
  thread.innerHTML = "";
  if (greeting) {
    addChatMessage("assistant", greeting, getSelectedAgentLabel());
  }
}

function showConversationNotice(message, state = "empty") {
  const thread = document.getElementById("chat-thread");
  if (!thread) return;
  thread.innerHTML = "";
  const notice = document.createElement("div");
  notice.className = `conversation-notice ${state}`;
  notice.setAttribute("role", "status");
  notice.textContent = message;
  thread.appendChild(notice);
}

function formatRelativeTime(epochSeconds) {
  const ts = Number(epochSeconds);
  if (!ts) return "";
  const deltaMs = Date.now() - ts * 1000;
  if (deltaMs < 0) return "just now";
  const minutes = Math.floor(deltaMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function conversationTitle(conv) {
  if (!conv) return "新会话";
  return (conv.title || "").trim() || "未命名会话";
}

function conversationMeta(conv) {
  if (!conv) return "开始新的对话";
  const when = formatRelativeTime(conv.updated_at || conv.created_at);
  return when ? `更新于 ${when}` : "已保存会话";
}

function groupConversations(conversations, now = Date.now()) {
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const groups = { today: [], older: [] };
  for (const conversation of conversations) {
    const rawTimestamp = Number(conversation.updated_at || conversation.created_at || 0);
    const timestamp = rawTimestamp < 1e12 ? rawTimestamp * 1000 : rawTimestamp;
    const group = timestamp >= startOfToday.getTime() ? "today" : "older";
    groups[group].push(conversation);
  }
  return groups;
}

function renderConversationHistory() {
  const history = document.querySelector("[data-conversation-list]");
  if (!history) return;
  const groups = groupConversations(conversationCache);
  for (const groupName of ["today", "older"]) {
    const section = history.querySelector(`[data-conversation-group="${groupName}"]`);
    const items = section?.querySelector("[data-conversation-items]");
    if (!section || !items) continue;
    items.replaceChildren();
    for (const conversation of groups[groupName]) {
      const active = conversation.id === currentConversationId;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "conversation-item";
      button.dataset.conversationId = conversation.id;
      button.dataset.active = String(active);
      if (active) button.setAttribute("aria-current", "page");

      const title = document.createElement("span");
      title.className = "conversation-item-title";
      title.textContent = conversationTitle(conversation);
      const meta = document.createElement("span");
      meta.className = "conversation-item-meta";
      meta.textContent = conversationMeta(conversation);
      button.append(title, meta);
      items.appendChild(button);
    }
    section.hidden = groups[groupName].length === 0;
  }
  const empty = history.querySelector("[data-conversation-empty]");
  if (empty) empty.hidden = conversationCache.length > 0;
}

async function loadConversations(agent) {
  const history = document.querySelector("[data-conversation-list]");
  if (!history) return;
  try {
    const response = await fetch("/api/conversations");
    if (!response.ok) return;
    const data = await response.json();
    conversationCache = data.conversations || [];
  } catch {
    conversationCache = [];
  }
  renderConversationHistory();
}

async function loadConversationMessages(conversationId) {
  if (!conversationId) {
    resetChatThread("");
    return;
  }
  const requestedConversationId = conversationId;
  showConversationNotice("Loading conversation...", "loading");
  try {
    const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (currentConversationId !== requestedConversationId) return;
    const messages = data.messages || [];
    if (!messages.length) {
      showConversationNotice(
        "No messages were saved for this conversation. It may have stopped before execution completed."
      );
      return;
    }
    const thread = document.getElementById("chat-thread");
    if (thread) thread.innerHTML = "";
    for (const msg of messages) {
      addChatMessage(
        msg.role === "user" ? "user" : "assistant",
        msg.content,
        msg.role === "user" ? "你" : agentLabel(msg.agent_id || "general_agent"),
      );
    }
  } catch {
    if (currentConversationId === requestedConversationId) {
      showConversationNotice("Conversation messages could not be loaded.", "error");
    }
  }
}

async function startNewConversation() {
  currentConversationId = null;
  resetChatThread("New conversation started. How can I help?");
  renderConversationHistory();
}

function setExecutionState(label, activeIndex = -1, done = false) {
  const state = document.getElementById("execution-state");
  const steps = Array.from(document.querySelectorAll("#step-list li"));
  if (state) state.textContent = label;
  steps.forEach((step, index) => {
    step.classList.toggle("active", index === activeIndex);
    step.classList.toggle("done", done || index < activeIndex);
  });
}

function setAgentStatus(agentName, label) {
  const card = getAgentCard(agentName);
  if (card) card.dataset.state = label;
}

function applyAgentMode() {
  const bar = document.querySelector("[data-conversation-bar]");
  if (bar) bar.hidden = false;
}

function bindAgentSelector() {
  const radios = Array.from(document.querySelectorAll('input[name="agent"]'));
  const cards = Array.from(document.querySelectorAll("[data-agent-card]"));
  const update = async (isUserChange) => {
    if (!document.querySelector('input[name="agent"]:checked') && radios[0]) {
      radios[0].checked = true;
    }
    const selected = getSelectedAgentName();
    cards.forEach((card) => {
      card.classList.toggle("active", card.dataset.agentCard === selected);
    });
    setAgentStatus(selected, "selected");
    applyAgentMode();

    if (isUserChange) {
      // Switching agents starts a fresh local thread; chat agents may also load
      // persisted history from the backend.
      currentConversationId = null;
      clearPendingResult();
      resetChatThread(`Hi, I'm the ${getSelectedAgentLabel()}. How can I help?`);
      await loadConversations(selected);
    } else {
      await loadConversations(selected);
    }
  };
  radios.forEach((radio) => radio.addEventListener("change", () => update(true)));
  cards.forEach((card) => {
    const revealTooltip = () => delete card.dataset.tooltipHidden;
    card.addEventListener("pointerenter", revealTooltip);
    card.addEventListener("focusin", revealTooltip);
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      card.dataset.tooltipHidden = "true";
      event.stopPropagation();
    });
  });
  update(false);
}

function tableHtml(rows, label = "Data") {
  if (!rows || rows.length === 0) {
    return '<div class="empty-state ak-empty-state">No records to display.</div>';
  }
  const columns = Object.keys(rows[0]);
  const header = columns.map((column) => `<th scope="col">${escapeHtml(column)}</th>`).join("");
  const body = rows
    .map((row) => {
      const cells = columns
        .map((column) => {
          const value = row[column];
          const rendered = Array.isArray(value) ? value.join(", ") : value ?? "";
          return `<td>${escapeHtml(rendered)}</td>`;
        })
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  return `<div class="table-wrap ak-table-wrap"><table class="data-table ak-data-table" aria-label="${escapeHtml(label)}"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderBusinessOutput(final) {
  const rankedCandidates = final.ranked_candidates || [];
  if (rankedCandidates.length) {
    return tableHtml(rankedCandidates, "Ranked candidates");
  }

  const blocks = [];
  if (final.campaign_summary) {
    blocks.push(`<p class="response-text">${escapeHtml(final.campaign_summary)}</p>`);
  }
  if (final.growth_goal) {
    blocks.push(`
      <div class="metric-strip">
        <div><span>Platform</span><strong>${escapeHtml(final.platform || "business")}</strong></div>
        <div><span>Goal</span><strong>${escapeHtml(final.growth_goal.target_followers || "")} followers</strong></div>
        <div><span>Window</span><strong>${escapeHtml(final.growth_goal.days || "")} days</strong></div>
        <div><span>Cadence</span><strong>${escapeHtml(final.cadence || "")}</strong></div>
      </div>
    `);
  }
  if (final.agent_pipeline?.length) {
    blocks.push(`<h3 class="result-subtitle">Agent Pipeline</h3>${tableHtml(final.agent_pipeline, "Agent pipeline")}`);
  }
  if (final.top_cases?.length) {
    blocks.push(`<h3 class="result-subtitle">Top Cases</h3>${tableHtml(final.top_cases, "Top cases")}`);
  }
  if (final.comparison?.length) {
    blocks.push(`<h3 class="result-subtitle">Pattern Comparison</h3>${tableHtml(final.comparison, "Pattern comparison")}`);
  }
  if (final.article) {
    const outline = (final.article.outline || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    blocks.push(`
      <div class="article-draft">
        <h3>${escapeHtml(final.article.title || "Article Draft")}</h3>
        ${outline ? `<ol>${outline}</ol>` : ""}
        <pre>${escapeHtml(final.article.body || "")}</pre>
      </div>
    `);
  }
  if (final.publish) {
    blocks.push(`<h3 class="result-subtitle">Publish Package</h3>${tableHtml([final.publish], "Publish package")}`);
  }

  return blocks.length ? blocks.join("") : '<div class="empty-state ak-empty-state">No structured business output to display.</div>';
}

function summarizePayload(payload) {
  if (!payload || typeof payload !== "object") return String(payload ?? "");
  if (payload.node) return `node=${payload.node}`;
  if (payload.skill) return `skill=${payload.skill} ${payload.reason || payload.mode || payload.confidence || ""}`.trim();
  if (payload.steps) return `steps=${payload.steps.length}`;
  if (payload.text) return String(payload.text).slice(0, 120);
  if (Object.prototype.hasOwnProperty.call(payload, "has_error")) return `has_error=${payload.has_error}`;
  return JSON.stringify(payload).slice(0, 160);
}

function formatTimestamp(value) {
  if (value === null || value === undefined || value === "") return "";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const date = new Date(numeric * 1000);
  const pad = (part) => String(part).padStart(2, "0");
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
  ].join("-") + " " + [
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
  ].join(":");
}

function planRows(plan) {
  const rows = [];
  if (plan?.route) {
    rows.push({ Item: "Route", Value: plan.route.skill_name || "", Detail: plan.route.reason || "" });
    rows.push({ Item: "Confidence", Value: plan.route.confidence || "", Detail: "" });
  }
  for (const step of plan?.steps || []) {
    rows.push({
      Item: `Step ${step.step_id}`,
      Value: step.skill_name,
      Detail: `mode=${step.mode} depends_on=${JSON.stringify(step.depends_on || [])}`,
    });
  }
  return rows;
}

function auditRows(events) {
  return (events || []).map((event) => ({
    Time: formatTimestamp(event.ts),
    Event: event.type,
    Summary: summarizePayload(event.payload),
  }));
}

function approvalActionHtml(waitingForApproval, approval) {
  if (!waitingForApproval) return "";
  const skills = approval?.skills || [];
  const skillText = skills.length ? skills.join(", ") : "selected skill";
  const isPublication = approval?.phase === "post_execution";
  const description = isPublication
    ? "Approve the frozen content shown above and publish it directly to Xiaohongshu."
    : `Approve execution for <code>${escapeHtml(skillText)}</code>. The runtime will resume the paused task with an approval token in the request context.`;
  const approveLabel = isPublication ? "Approve & Publish" : "Approve & Run";
  const previewUrls = Array.isArray(approval?.preview?.media_preview_urls)
    ? approval.preview.media_preview_urls
    : [];
  const mediaPreview = previewUrls.length
    ? `<div class="approval-media">${previewUrls.map((url) => (
      `<img src="${escapeHtml(url)}" alt="Publication media preview">`
    )).join("")}</div>`
    : "";
  const preview = approval?.preview || {};
  const textImagePreview = preview.media_strategy === "xhs_text_image"
    ? `<div class="approval-text-image">
        <p><strong>Media</strong> Xiaohongshu text cards · <strong>Style</strong> ${escapeHtml(preview.card_style || "-")}</p>
        <blockquote>${escapeHtml(preview.card_text || "")}</blockquote>
      </div>`
    : "";
  return `
    <div class="approval-box">
      <div>
        <strong>${isPublication ? "Publication approval required" : "Human approval required"}</strong>
        <p>${description}</p>
        ${mediaPreview}
        ${textImagePreview}
      </div>
      <div class="approval-actions">
        <button class="secondary danger" type="button" data-reject-pending>Reject</button>
        <button class="primary" type="button" data-approve-pending>${approveLabel}</button>
      </div>
    </div>
  `;
}

// 统一 Runtime 在顶层返回 status/governance/thread_id；这里只构造前端展示模型。
function runtimeView(raw = {}) {
  if (!raw.status) return raw;
  return {
    ...raw,
    output: {
      ...(raw.output || {}),
      status: raw.status,
      governance: raw.governance || {},
      thread_id: raw.thread_id || "",
      final: raw.output || {},
    },
  };
}

function renderResult(payload, requestPayload = null, options = {}) {
  const region = document.getElementById("result-region");
  if (!region) return;
  const response = runtimeView(payload.response);
  const final = response.output?.final || {};
  const ranked = final.ranked_candidates || [];
  const outputStatus = response.output?.status || "";
  const waitingForApproval = outputStatus === "waiting_for_approval";
  const rejected = outputStatus === "rejected";
  const approval = response.output?.governance?.approval || response.output?.approval || final.approval || {};
  pendingApproval = waitingForApproval && requestPayload
    ? { request: { ...requestPayload }, skills: approval.skills || [], thread_id: response.output?.thread_id || "" }
    : null;
  const conversationMessage = final.message || response.output?.message || "";
  const rawPlan = escapeHtml(JSON.stringify(response.plan || {}, null, 2));
  const rawAudit = escapeHtml(JSON.stringify(response.audit_events || [], null, 2));
  const hidePrimaryPanel = options.hidePrimaryPanel === true;
  const primaryTitle = waitingForApproval
    ? "Approval Required"
    : rejected
      ? "Approval Rejected"
    : conversationMessage
      ? "Conversation Response"
      : "Decision Output";
  const primarySubtitle = waitingForApproval
    ? "Human-in-the-loop checkpoint"
    : rejected
      ? "Execution stopped"
    : conversationMessage
      ? "General answer"
      : final.job_title || final.job_id || "Completed";
  const primaryBody = conversationMessage
    ? `<p class="response-text">${escapeHtml(conversationMessage)}</p>${approvalActionHtml(waitingForApproval, approval)}`
    : renderBusinessOutput(final);

  region.hidden = false;
  region.innerHTML = `
    ${hidePrimaryPanel ? "" : `
      <article class="panel ak-panel result-card ak-result-card" aria-labelledby="result-primary-title">
        <div class="panel-head ak-panel-header">
          <h2 id="result-primary-title">${primaryTitle}</h2>
          <span>${escapeHtml(primarySubtitle)}</span>
        </div>
        ${primaryBody}
      </article>
    `}
    <section class="result-grid ak-result-grid">
      <article class="panel ak-panel ak-panel--table" aria-labelledby="execution-plan-title">
        <div class="panel-head ak-panel-header"><h2 id="execution-plan-title">Execution Plan</h2><span>LangGraph route</span></div>
        ${tableHtml(planRows(response.plan), "Execution plan")}
      </article>
      <article class="panel ak-panel ak-panel--table" aria-labelledby="audit-timeline-title">
        <div class="panel-head ak-panel-header"><h2 id="audit-timeline-title">Audit Timeline</h2><span>${(response.audit_events || []).length} events</span></div>
        ${tableHtml(auditRows(response.audit_events), "Audit timeline")}
      </article>
      <article class="panel ak-panel ak-panel--flush" aria-labelledby="raw-plan-title">
        <div class="json-panel">
          <h2 id="raw-plan-title" class="json-title">Raw Plan</h2>
          <pre class="json-pre">${rawPlan}</pre>
        </div>
      </article>
      <article class="panel ak-panel ak-panel--flush" aria-labelledby="raw-audit-title">
        <div class="json-panel">
          <h2 id="raw-audit-title" class="json-title">Raw Audit</h2>
          <pre class="json-pre">${rawAudit}</pre>
        </div>
      </article>
    </section>
  `;
  if (options.scroll !== false) {
    region.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function addChatMessage(role, text, labelOverride = "") {
  const thread = document.getElementById("chat-thread");
  if (!thread) return;
  thread.querySelector(".conversation-notice")?.remove();
  const node = document.createElement("div");
  node.className = `chat-message ${role}`;
  const label = labelOverride || (role === "user" ? "You" : getSelectedAgentLabel());
  const labelSpan = document.createElement("span");
  labelSpan.textContent = label;
  node.appendChild(labelSpan);
  const body = document.createElement("div");
  body.className = "chat-body";
  if (role === "user") {
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    body.appendChild(paragraph);
  } else {
    body.innerHTML = renderAssistantHtml(text);
  }
  node.appendChild(body);
  thread.appendChild(node);
  thread.scrollTop = thread.scrollHeight;
}

function addApprovalChatMessage(text, approval, labelOverride = "") {
  const thread = document.getElementById("chat-thread");
  if (!thread) return;
  const node = document.createElement("div");
  node.className = "chat-message assistant approval-message";
  node.innerHTML = `
    <span>${escapeHtml(labelOverride || getSelectedAgentLabel())}</span>
    <div class="chat-body">${renderAssistantHtml(text)}</div>
    ${approvalActionHtml(true, approval)}
  `;
  thread.appendChild(node);
  thread.scrollTop = thread.scrollHeight;
}

function addAssistantResponse(result, requestPayload) {
  const response = runtimeView(result.response);
  const final = response.output?.final || {};
  const approval = response.output?.governance?.approval || response.output?.approval || final.approval || {};
  const label = getSelectedAgentLabel();
  if (response.output?.status === "waiting_for_approval") {
    pendingApproval = { request: { ...requestPayload }, skills: approval.skills || [], thread_id: response.output?.thread_id || "" };
    addApprovalChatMessage(result.assistant_text, approval, label);
    return;
  }
  addChatMessage("assistant", result.assistant_text, label);
}

function resolveApprovalActions(label) {
  document.querySelectorAll("#chat-thread .approval-box").forEach((box) => {
    const actions = box.querySelector(".approval-actions");
    if (actions) {
      actions.innerHTML = `<span class="approval-resolution">${escapeHtml(label)}</span>`;
    }
  });
  document.querySelectorAll("#chat-thread .approval-message").forEach((message) => {
    message.dataset.approvalResolved = label.toLowerCase();
  });
}

function pruneDuplicateApprovalMessages() {
  const messages = Array.from(document.querySelectorAll("#chat-thread .chat-message.assistant"));
  let keptApprovalMessage = false;
  for (const message of messages) {
    const text = message.textContent || "";
    const isApprovalWait = text.includes("This run is waiting for human approval before execution.");
    if (!isApprovalWait) continue;
    if (message.classList.contains("approval-message") && !keptApprovalMessage) {
      keptApprovalMessage = true;
      continue;
    }
    message.remove();
  }
}

function clearPendingResult() {
  const region = document.getElementById("result-region");
  if (!region) return;
  region.innerHTML = "";
  region.hidden = true;
}

function bindRangeOutputs() {
  document.querySelectorAll('input[type="range"][data-range-output]').forEach((input) => {
    const output = document.getElementById(input.dataset.rangeOutput);
    const update = () => {
      if (output) output.value = input.value;
    };
    input.addEventListener("input", update);
    update();
  });
}

function finalizeActionResult(result, requestPayload, bubble, selectedAgent, streamed = "") {
  const response = runtimeView(result.response);
  const final = response.output?.final || {};
  const status = response.output?.status;
  const approval = response.output?.governance?.approval || response.output?.approval || final.approval || {};
  if (status === "waiting_for_approval") {
    if (bubble) bubble.node.remove();
    pendingApproval = {
      request: { ...requestPayload },
      skills: approval.skills || [],
      thread_id: response.output?.thread_id || "",
    };
    addApprovalChatMessage(
      result.assistant_text,
      approval,
      agentLabel(result.agent || "general_agent"),
    );
  } else if (bubble) {
    // Tokens emitted inside an action workflow may be an intermediate artifact
    // (for example, only the generated article body). Once the workflow ends,
    // prefer the server's complete evidence/report response.
    finalizeAssistantBubble(bubble, result.assistant_text || streamed || "");
    const label = bubble.node.querySelector(":scope > span");
    if (label) label.textContent = agentLabel(result.agent || "general_agent");
    appendAgentTrace(bubble.node, result.response || {});
  }
  if (status === "needs_clarification") {
    const resolution = response.output?.input_resolution || {};
    pendingInput = {
      agent: selectedAgent,
      skill_name: resolution.skill_name || "",
      arguments: { ...(resolution.arguments || {}) },
    };
  } else {
    pendingInput = null;
  }
  const waiting = status === "waiting_for_approval" || status === "needs_clarification";
  const stateLabel = status === "needs_clarification"
    ? "Needs input"
    : (status === "waiting_for_approval" ? "Waiting for approval" : "Completed");
  setExecutionState(stateLabel, waiting ? 2 : 5, !waiting);
  setAgentStatus(selectedAgent, waiting ? "waiting" : "completed");
  const intentType = response.output?.governance?.intent?.intent_type || final.intent_type || "";
  const hidePrimaryPanel = Boolean(final.conversation)
    || ["waiting_for_approval", "needs_clarification", "rejected"].includes(status)
    || ["platform_question", "chit_chat", "unknown"].includes(intentType);
  renderResult(result, requestPayload, { hidePrimaryPanel });
}

function appendAgentTrace(node, response) {
  if (!node || !response?.governance) return;
  const route = response.governance.route || {};
  const delegation = response.governance.delegation || {};
  if (!route.type && !delegation.child_run_id) return;
  const details = document.createElement("details");
  details.className = "ak-agent-trace";
  const summary = document.createElement("summary");
  summary.textContent = delegation.child_run_id ? "查看 Agent 委派追踪" : "查看 General 决策摘要";
  const list = document.createElement("dl");
  const rows = [
    ["路由", route.type || "general_answer"],
    ["执行者", agentLabel(response.agent || delegation.target_agent || "general_agent")],
    ["依据", route.reason || "General Agent 直接回答"],
    ["父运行", delegation.parent_run_id || response.run_id || ""],
    ["子运行", delegation.child_run_id || ""],
  ];
  for (const [name, value] of rows) {
    if (!value) continue;
    const term = document.createElement("dt");
    term.textContent = name;
    const description = document.createElement("dd");
    description.textContent = value;
    list.append(term, description);
  }
  details.append(summary, list);
  node.appendChild(details);
}

function bindMentionAutocomplete() {
  const input = document.querySelector("[data-chat-input]");
  const menu = document.querySelector("[data-agent-mention-menu]");
  if (!input || !menu) return;
  let activeIndex = 0;
  let visible = [];

  const mentionState = () => {
    const caret = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, caret);
    const match = before.match(/(?:^|\s)@([^\s@]*)$/);
    return match ? { query: match[1].toLocaleLowerCase(), start: caret - match[1].length - 1, caret } : null;
  };
  const close = () => {
    menu.hidden = true;
    menu.innerHTML = "";
    visible = [];
    input.setAttribute("aria-expanded", "false");
  };
  const render = () => {
    const state = mentionState();
    if (!state) {
      close();
      return;
    }
    visible = AGENT_DIRECTORY.filter((agent) => agent.name !== "general_agent").filter((agent) => {
      const haystack = [agent.name, agent.label, ...(agent.aliases || [])].join(" ").toLocaleLowerCase();
      return haystack.includes(state.query);
    });
    if (!visible.length) {
      close();
      return;
    }
    activeIndex = Math.min(activeIndex, visible.length - 1);
    menu.innerHTML = visible.map((agent, index) => {
      const alias = agent.aliases?.[0] || agent.label || agent.name;
      return `<button type="button" role="option" data-mention-index="${index}" aria-selected="${index === activeIndex}"><strong>@${escapeHtml(alias)}</strong><span>${escapeHtml(agent.mission || agent.description || agent.domain || "")}</span></button>`;
    }).join("");
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  const choose = (index) => {
    const state = mentionState();
    const agent = visible[index];
    if (!state || !agent) return;
    const alias = agent.aliases?.[0] || agent.label || agent.name;
    input.setRangeText(`@${alias} `, state.start, state.caret, "end");
    close();
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  };

  input.addEventListener("input", render);
  input.addEventListener("click", render);
  input.addEventListener("keydown", (event) => {
    if (menu.hidden || !visible.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      event.stopImmediatePropagation();
      activeIndex = (activeIndex + (event.key === "ArrowDown" ? 1 : -1) + visible.length) % visible.length;
      render();
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      event.stopImmediatePropagation();
      choose(activeIndex);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  }, true);
  menu.addEventListener("click", (event) => {
    const option = event.target.closest("[data-mention-index]");
    if (option) choose(Number(option.dataset.mentionIndex));
  });
  document.addEventListener("click", (event) => {
    if (event.target !== input && !menu.contains(event.target)) close();
  });
}

function agentFromRequestPayload(payload) {
  return payload?.context?.agent || payload?.agent || getSelectedAgentName();
}

function buildApprovalChatPayload(action) {
  const originalRequest = pendingApproval?.request || collectChatPayload("");
  const originalContext = originalRequest.context || {};
  const context = {
    agent: originalContext.agent || originalRequest.agent || getSelectedAgentName(),
    message: action === "approve" ? "Approve" : "Reject",
    approval: {
      action,
      thread_id: pendingApproval?.thread_id || "",
      skills: pendingApproval?.skills || [],
      request: originalRequest,
    },
  };
  if (currentConversationId) context.conversation_id = currentConversationId;
  return {
    user_id: originalRequest.user_id || UI_CONFIG.default_user_id || "",
    context,
  };
}

async function runUnifiedChatTurn(message, selectedAgent) {
  const isNewConversation = !currentConversationId;
  const requestPayload = collectChatPayload(message);
  const bubble = addLiveAssistantMessage(getSelectedAgentLabel());
  let streamed = "";
  let errored = null;
  let errorConversationId = null;
  try {
    const finalData = await streamSse("/api/chat/stream", requestPayload, {
      onToken: (delta) => {
        streamed += delta;
        if (bubble) bubble.p.textContent = streamed;
        scrollChatToBottom();
      },
      onError: (msg, details) => {
        errored = msg;
        errorConversationId = details?.conversation_id || null;
      },
    });
    if (errored && !finalData) throw new Error(errored);
    if (finalData) {
      if (finalData.response) {
        currentConversationId = finalData.conversation_id || currentConversationId;
        finalizeActionResult(finalData, requestPayload, bubble, selectedAgent, streamed);
        if (isNewConversation) await loadConversations(selectedAgent);
        return;
      } else {
        currentConversationId = finalData.conversation_id || currentConversationId;
        // The streamed text already equals the reply; fall back to the final
        // payload only when nothing streamed. Either way re-render the bubble
        // with collapsible thinking + markdown.
        finalizeAssistantBubble(bubble, streamed || finalData.assistant_text || "");
      }
    }
    setExecutionState("Completed", 5, true);
    setAgentStatus(selectedAgent, "completed");
    if (isNewConversation) await loadConversations(selectedAgent);
  } catch (error) {
    if (!streamed && !errored) {
      try {
        const result = await postChat(requestPayload);
        if (result.response) {
          currentConversationId = result.conversation_id || currentConversationId;
          finalizeActionResult(result, requestPayload, bubble, selectedAgent, "");
          if (isNewConversation) await loadConversations(selectedAgent);
        } else {
          currentConversationId = result.conversation_id || currentConversationId;
          finalizeAssistantBubble(bubble, result.assistant_text || "");
          setExecutionState("Completed", 5, true);
          setAgentStatus(selectedAgent, "completed");
          if (isNewConversation) await loadConversations(selectedAgent);
        }
        return;
      } catch (fallbackError) {
        error = fallbackError;
      }
    }
    if (bubble) bubble.p.textContent = error.message;
    if (errorConversationId) {
      currentConversationId = errorConversationId;
      await loadConversations(selectedAgent);
    }
    setExecutionState("Failed");
    setAgentStatus(selectedAgent, "failed");
  }
}

function bindChatForm() {
  const chatForm = document.getElementById("chat-form");
  if (!chatForm) return;
  const input = chatForm.querySelector("[data-chat-input]");
  const submit = chatForm.querySelector('button[type="submit"]');
  if (!input || !submit) return;
  let isComposing = false;
  const resizeInput = () => {
    input.style.height = "auto";
    const maxHeight = Number.parseFloat(getComputedStyle(input).maxHeight);
    const nextHeight = Number.isFinite(maxHeight)
      ? Math.min(input.scrollHeight, maxHeight)
      : input.scrollHeight;
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = Number.isFinite(maxHeight) && input.scrollHeight > maxHeight
      ? "auto"
      : "hidden";
  };
  const runChat = async (message) => {
    if (chatBusy) return;
    if (!message.trim()) return;
    addChatMessage("user", message);
    input.value = "";
    resizeInput();
    setChatBusy(true);
    const selectedAgent = getSelectedAgentName();
    setAgentStatus(selectedAgent, "running");
    setExecutionState("Processing", 0);
    try {
      await runUnifiedChatTurn(message, selectedAgent);
    } finally {
      setChatBusy(false);
    }
  };
  chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    runChat(input.value);
  });
  input.addEventListener("input", () => {
    resizeInput();
    syncChatComposerState();
  });
  input.addEventListener("compositionstart", () => {
    isComposing = true;
  });
  input.addEventListener("compositionend", () => {
    isComposing = false;
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.isComposing || isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!chatBusy && input.value.trim()) chatForm.requestSubmit(submit);
  });
  document.querySelector("[data-chat-demo]")?.addEventListener("click", () => runChat(getSelectedAgentDemoPrompt()));
  window.addEventListener("resize", resizeInput, { passive: true });
  resizeInput();
  syncChatComposerState();
}

function bindConversationHistory() {
  const sidebar = document.querySelector("[data-conversation-sidebar]");
  const history = document.querySelector("[data-conversation-list]");
  const collapseButton = document.querySelector("[data-conversation-sidebar-toggle]");
  const openButton = document.querySelector("[data-conversation-sidebar-open]");
  const mobileQuery = window.matchMedia("(max-width: 47.5rem)");

  const readCollapsedPreference = () => {
    try {
      return localStorage.getItem(HISTORY_COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  };

  const syncSidebarControls = () => {
    const expanded = mobileQuery.matches
      ? document.body.classList.contains("ak-history-drawer-open")
      : !document.body.classList.contains("ak-history-collapsed");
    collapseButton?.setAttribute("aria-expanded", String(expanded));
    openButton?.setAttribute("aria-expanded", String(expanded));
  };

  const setHistoryCollapsed = (collapsed, persist = true) => {
    document.body.classList.toggle("ak-history-collapsed", collapsed);
    if (persist) {
      try {
        localStorage.setItem(HISTORY_COLLAPSED_KEY, String(collapsed));
      } catch {
        // 隐私模式或存储策略禁止写入时，仍保留当前页面状态。
      }
    }
    syncSidebarControls();
  };

  const closeMobileDrawer = (restoreFocus = false) => {
    document.body.classList.remove("ak-history-drawer-open");
    syncSidebarControls();
    if (restoreFocus) openButton?.focus();
  };

  const openSidebar = () => {
    if (mobileQuery.matches) {
      document.body.classList.add("ak-history-drawer-open");
      syncSidebarControls();
      sidebar?.querySelector("[data-new-conversation]")?.focus();
      return;
    }
    setHistoryCollapsed(false);
  };

  setHistoryCollapsed(readCollapsedPreference(), false);

  document.querySelector("[data-new-conversation]")?.addEventListener("click", () => {
    startNewConversation();
    if (mobileQuery.matches) closeMobileDrawer();
  });

  history?.addEventListener("click", async (event) => {
    const item = event.target.closest("[data-conversation-id]");
    if (!item) return;
    currentConversationId = item.dataset.conversationId || null;
    renderConversationHistory();
    await loadConversationMessages(currentConversationId);
    if (mobileQuery.matches) closeMobileDrawer(true);
  });

  collapseButton?.addEventListener("click", () => {
    if (mobileQuery.matches) closeMobileDrawer(true);
    else setHistoryCollapsed(true);
  });
  openButton?.addEventListener("click", openSidebar);
  document.addEventListener("click", (event) => {
    if (
      mobileQuery.matches &&
      document.body.classList.contains("ak-history-drawer-open") &&
      !sidebar?.contains(event.target) &&
      !openButton?.contains(event.target)
    ) {
      closeMobileDrawer();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("ak-history-drawer-open")) {
      closeMobileDrawer(true);
    }
  });
  mobileQuery.addEventListener?.("change", () => {
    document.body.classList.remove("ak-history-drawer-open");
    syncSidebarControls();
  });
}

async function approvePendingTask() {
  if (!pendingApproval) return;
  const originalRequest = pendingApproval.request;
  const agentName = agentFromRequestPayload(originalRequest);
  const agentLabel = getSelectedAgentLabel();
  const buttons = document.querySelectorAll("[data-approve-pending], [data-reject-pending]");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  setExecutionState("Approved, executing", 3);
  setAgentStatus(agentName, "running");
  const approvedPayload = buildApprovalChatPayload("approve");
  const bubble = addLiveAssistantMessage(agentLabel);
  let streamed = "";
  let errored = null;
  let succeeded = false;
  try {
    const result = await streamSse("/api/chat/stream", approvedPayload, {
      onToken: (delta) => {
        streamed += delta;
        if (bubble) bubble.p.textContent = streamed;
        scrollChatToBottom();
      },
      onError: (msg) => {
        errored = msg;
      },
    });
    if (errored && !result) throw new Error(errored);
    const status = runtimeView(result.response).output?.status;
    setExecutionState(status === "waiting_for_approval" ? "Waiting for approval" : "Completed", status === "waiting_for_approval" ? 2 : 5, status !== "waiting_for_approval");
    setAgentStatus(agentName, status === "waiting_for_approval" ? "waiting" : "completed");
    resolveApprovalActions("Approved");
    pruneDuplicateApprovalMessages();
    pendingApproval = null;
    clearPendingResult();
    finalizeAssistantBubble(bubble, result.assistant_text || streamed || "");
    renderResult(result, originalRequest);
    succeeded = true;
  } catch (error) {
    setExecutionState("Failed");
    setAgentStatus(agentName, "failed");
    // Show the message (incl. the truncation fallback) in the chat bubble
    // rather than a jarring alert popup.
    if (bubble) finalizeAssistantBubble(bubble, error.message);
    else alert(error.message);
  } finally {
    if (!succeeded) {
      buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }
}

async function rejectPendingTask() {
  if (!pendingApproval) return;
  const originalRequest = pendingApproval.request;
  const agentName = agentFromRequestPayload(originalRequest);
  const agentLabel = getSelectedAgentLabel();
  const buttons = document.querySelectorAll("[data-approve-pending], [data-reject-pending]");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  setExecutionState("Rejected", 2);
  setAgentStatus(agentName, "rejected");
  const rejectedPayload = buildApprovalChatPayload("reject");
  const bubble = addLiveAssistantMessage(agentLabel);
  let streamed = "";
  let errored = null;
  let succeeded = false;
  try {
    const result = await streamSse("/api/chat/stream", rejectedPayload, {
      onToken: (delta) => {
        streamed += delta;
        if (bubble) bubble.p.textContent = streamed;
        scrollChatToBottom();
      },
      onError: (msg) => {
        errored = msg;
      },
    });
    if (errored && !result) throw new Error(errored);
    setExecutionState("Rejected", 2);
    resolveApprovalActions("Rejected");
    pruneDuplicateApprovalMessages();
    pendingApproval = null;
    clearPendingResult();
    // Rejected runs don't execute, so nothing streams -> show the rejection text.
    finalizeAssistantBubble(bubble, streamed || result.assistant_text || "");
    renderResult(result, originalRequest);
    succeeded = true;
  } catch (error) {
    setExecutionState("Failed");
    if (bubble) finalizeAssistantBubble(bubble, error.message);
    else alert(error.message);
  } finally {
    if (!succeeded) {
      buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }
}

function bindApprovalActions() {
  document.addEventListener("click", (event) => {
    const approveButton = event.target.closest("[data-approve-pending]");
    if (approveButton) {
      event.preventDefault();
      approvePendingTask();
      return;
    }

    const rejectButton = event.target.closest("[data-reject-pending]");
    if (rejectButton) {
      event.preventDefault();
      rejectPendingTask();
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindPrimaryNavigation();
  bindAgentSelector();
  bindRangeOutputs();
  bindChatForm();
  bindMentionAutocomplete();
  bindConversationHistory();
  bindApprovalActions();
  bindTabs();
});
