(function initChatTimeline(global) {
  "use strict";

  const ACTIVE_ATTEMPT_STATUSES = new Set([
    "queued",
    "running",
    "waiting_for_approval",
    "resuming",
  ]);
  const THINKING_ATTEMPT_STATUSES = new Set(["queued", "running", "resuming"]);
  const RETRYABLE_ATTEMPT_STATUSES = new Set([
    "failed",
    "interrupted",
    "rejected",
    "cancelled",
  ]);
  const STAGE_LABELS = Object.freeze({
    understanding_request: "正在理解你的需求",
    routing_agent: "正在选择合适的 Agent",
    executing_agent: "正在执行任务",
    preparing_approval: "正在准备审批内容",
    awaiting_user_decision: "等待你的确认",
    publishing: "正在发布内容",
    finalizing: "正在整理结果",
  });
  const ATTEMPT_STATUS_LABELS = Object.freeze({
    queued: "等待开始",
    running: "正在运行",
    waiting_for_approval: "等待审批",
    resuming: "正在恢复",
    succeeded: "已完成",
    failed: "执行失败",
    interrupted: "执行中断",
    rejected: "已拒绝",
    cancelled: "已取消",
  });
  const SAME_ORIGIN_MEDIA_PREFIXES = Object.freeze([
    "/api/xhs/publish-assets/",
  ]);
  const MEDIA_URL_VALIDATION_ORIGIN = "https://agentkit.invalid";

  function thinkingLabel(stage) {
    return STAGE_LABELS[String(stage || "")] || "正在处理";
  }

  function timelineTurns(timeline) {
    return Array.isArray(timeline?.turns) ? timeline.turns : [];
  }

  function hasActiveAttempt(timeline) {
    return timelineTurns(timeline).some((turn) => (
      (turn.attempts || []).some((attempt) => (
        ACTIVE_ATTEMPT_STATUSES.has(String(attempt.status || ""))
      ))
    ));
  }

  function latestRetryableAttempt(timeline) {
    const turns = timelineTurns(timeline);
    for (let turnIndex = turns.length - 1; turnIndex >= 0; turnIndex -= 1) {
      const turn = turns[turnIndex];
      const attempts = Array.isArray(turn.attempts) ? turn.attempts : [];
      for (let attemptIndex = attempts.length - 1; attemptIndex >= 0; attemptIndex -= 1) {
        const attempt = attempts[attemptIndex];
        if (RETRYABLE_ATTEMPT_STATUSES.has(String(attempt.status || ""))) {
          return {
            turnId: String(turn.id || ""),
            attemptId: String(attempt.id || ""),
            attempt,
          };
        }
      }
    }
    return null;
  }

  function createHydrationGuard() {
    const conversations = new Map();

    return Object.freeze({
      begin(conversationId) {
        const key = String(conversationId || "");
        const previous = conversations.get(key);
        previous?.controller.abort();
        const controller = new AbortController();
        const state = {
          controller,
          sequence: Number(previous?.sequence || 0) + 1,
          version: Number(previous?.version ?? -1),
        };
        conversations.set(key, state);
        return Object.freeze({
          conversationId: key,
          sequence: state.sequence,
          signal: controller.signal,
        });
      },

      commit(request, version) {
        const state = conversations.get(String(request?.conversationId || ""));
        const nextVersion = Number(version ?? -1);
        if (
          !state ||
          request?.signal?.aborted ||
          state.sequence !== request?.sequence ||
          !Number.isFinite(nextVersion) ||
          nextVersion < state.version
        ) {
          return false;
        }
        state.version = nextVersion;
        return true;
      },

      cancel(conversationId) {
        conversations.get(String(conversationId || ""))?.controller.abort();
      },
    });
  }

  function element(tagName, className = "", text = "") {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function withTimelineKey(node, key) {
    node.dataset.timelineKey = String(key);
    return node;
  }

  function safeMediaPreviewUrl(value) {
    const candidate = typeof value === "string" ? value : "";
    if (
      !candidate ||
      candidate !== candidate.trim() ||
      candidate.includes("\\") ||
      /[\u0000-\u001f\u007f]/.test(candidate)
    ) {
      return "";
    }

    try {
      if (candidate.startsWith("/") && !candidate.startsWith("//")) {
        const parsed = new URL(candidate, MEDIA_URL_VALIDATION_ORIGIN);
        if (
          parsed.origin === MEDIA_URL_VALIDATION_ORIGIN &&
          SAME_ORIGIN_MEDIA_PREFIXES.some((prefix) => parsed.pathname.startsWith(prefix))
        ) {
          return candidate;
        }
        return "";
      }

      if (!/^https?:\/\//i.test(candidate)) return "";
      const parsed = new URL(candidate);
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? candidate : "";
    } catch {
      return "";
    }
  }

  function messageNode(message, handlers, roleOverride = "", key = "") {
    const role = roleOverride || (message?.role === "user" ? "user" : "assistant");
    const node = element("div", `chat-message ${role}`);
    const agentName = String(message?.agent_id || "general_agent");
    const label = role === "user"
      ? "你"
      : handlers.agentLabel?.(agentName) || "Agent";
    node.appendChild(element("span", "", label));
    const body = element("div", "chat-body ak-timeline-message-body");
    body.appendChild(element("p", "", String(message.content || "")));
    node.appendChild(body);
    if (key) withTimelineKey(node, key);
    return node;
  }

  function thinkingNode(attempt) {
    const node = element("div", "ak-thinking");
    const bars = element("span", "ak-thinking-bars");
    bars.setAttribute("aria-hidden", "true");
    for (let index = 0; index < 4; index += 1) bars.appendChild(element("i"));
    const label = element("span", "", thinkingLabel(attempt?.stage));
    label.dataset.thinkingLabel = "";
    node.append(bars, label);
    return node;
  }

  function actionDecisionLabel(action) {
    if (action.decision === "approved" || action.status === "approved") return "已批准";
    if (action.decision === "rejected" || action.status === "rejected") return "已拒绝";
    if (action.status === "completed") return "审批已完成";
    if (action.status === "invalidated") return "审批后的执行未完成";
    return "";
  }

  function appendActionPreview(container, action) {
    const preview = action?.preview || {};
    if (preview.title != null && String(preview.title).trim()) {
      container.appendChild(element("p", "ak-action-preview-title", String(preview.title)));
    }
    if (preview.summary != null && String(preview.summary).trim()) {
      container.appendChild(element("p", "ak-action-preview-summary", String(preview.summary)));
    }
    if (preview.card_text != null && String(preview.card_text).trim()) {
      const bodySection = element("section", "ak-action-preview-content");
      bodySection.appendChild(element("strong", "ak-action-preview-label", "正文预览"));
      bodySection.appendChild(element("p", "ak-action-preview-body", String(preview.card_text)));
      container.appendChild(bodySection);
    }

    const metadata = [
      ["内容形式", preview.media_strategy],
      ["卡片样式", preview.card_style],
      ["分页方式", preview.pagination_source],
      ["媒体摘要", preview.media_summary],
    ];
    for (const [label, value] of metadata) {
      if (value == null || !String(value).trim()) continue;
      container.appendChild(element("p", "ak-action-preview-meta", `${label}：${value}`));
    }
    const safeMediaUrls = Array.isArray(preview.media_preview_urls)
      ? preview.media_preview_urls.map(safeMediaPreviewUrl).filter(Boolean)
      : [];
    if (safeMediaUrls.length) {
      container.appendChild(element(
        "p",
        "ak-action-media-summary",
        `媒体预览：${safeMediaUrls.length} 个文件`,
      ));
      const mediaGrid = element("div", "ak-action-media-grid");
      for (const [index, url] of safeMediaUrls.slice(0, 4).entries()) {
        const link = element("a", "ak-action-media-link");
        withTimelineKey(link, `media:${action.id}:${index}`);
        link.setAttribute("href", String(url));
        link.setAttribute("target", "_blank");
        link.setAttribute("rel", "noopener noreferrer");
        const image = element("img", "ak-action-media-thumbnail");
        image.setAttribute("src", String(url));
        image.setAttribute("loading", "lazy");
        image.setAttribute("decoding", "async");
        image.setAttribute("alt", `媒体预览 ${index + 1}`);
        image.setAttribute("width", "160");
        image.setAttribute("height", "120");
        link.appendChild(image);
        mediaGrid.appendChild(link);
      }
      container.appendChild(mediaGrid);
      if (safeMediaUrls.length > 4) {
        container.appendChild(element(
          "p",
          "ak-action-media-more",
          `另有 ${safeMediaUrls.length - 4} 个媒体文件`,
        ));
      }
    } else if (Number.isFinite(Number(preview.media_count)) && Number(preview.media_count) > 0) {
      container.appendChild(element(
        "p",
        "ak-action-media-summary",
        `媒体预览：${Number(preview.media_count)} 个文件`,
      ));
    }
    const skills = Array.isArray(action?.skills) ? action.skills.filter(Boolean) : [];
    if (skills.length) {
      container.appendChild(element("p", "ak-action-skills", `涉及能力：${skills.join("、")}`));
    }
  }

  function renderAction(action, handlers) {
    const node = withTimelineKey(
      element("section", "ak-timeline-action"),
      `action:${action.id}`,
    );
    node.setAttribute("tabindex", "-1");
    node.setAttribute("aria-label", "审批状态");
    node.dataset.actionStatus = String(action?.status || "");
    node.appendChild(element(
      "strong",
      "",
      action.status === "pending" ? "需要你的确认" : "审批记录",
    ));
    appendActionPreview(node, action);
    const resolution = actionDecisionLabel(action);
    if (resolution) node.appendChild(element("p", "ak-action-resolution", resolution));

    // Action 只有处于 pending 时才允许人工作出决定。
    if (action.status === "pending") {
      const actions = element("div", "ak-timeline-action-buttons");
      const reject = element("button", "secondary danger", "拒绝");
      const approve = element("button", "primary", "批准并继续");
      reject.type = approve.type = "button";
      reject.dataset.timelineDecision = "rejected";
      approve.dataset.timelineDecision = "approved";
      withTimelineKey(reject, `decision:${action.id}:rejected`);
      withTimelineKey(approve, `decision:${action.id}:approved`);
      reject.addEventListener("click", () => handlers.onDecision?.({
        actionId: String(action.id || ""),
        decision: "rejected",
        expectedVersion: Number(action.version || 0),
      }));
      approve.addEventListener("click", () => handlers.onDecision?.({
        actionId: String(action.id || ""),
        decision: "approved",
        expectedVersion: Number(action.version || 0),
      }));
      actions.append(reject, approve);
      node.appendChild(actions);
    }
    return node;
  }

  function reviewMessageLayout(messages) {
    const byId = new Map(messages.map((message) => [String(message.id), message]));
    const revisions = messages.filter((message) => message.kind === "assistant_revision");
    const supersededByRevision = new Set(
      revisions
        .filter((message) => message.supersedes_message_id != null)
        .map((message) => String(message.supersedes_message_id)),
    );
    const hiddenMessageIds = new Set();
    const olderByLatestId = new Map();

    for (const latestRevision of revisions) {
      if (supersededByRevision.has(String(latestRevision.id))) continue;
      const olderRevisions = [];
      const seen = new Set();
      let previousId = latestRevision.supersedes_message_id;
      while (previousId != null && !seen.has(String(previousId))) {
        seen.add(String(previousId));
        const previous = byId.get(String(previousId));
        if (!previous) break;
        olderRevisions.unshift(previous);
        hiddenMessageIds.add(String(previous.id));
        previousId = previous.supersedes_message_id;
      }
      if (olderRevisions.length) {
        olderByLatestId.set(String(latestRevision.id), olderRevisions);
      }
    }
    return { hiddenMessageIds, olderByLatestId };
  }

  function appendAttemptContent(container, turn, attempt, handlers) {
    const messages = Array.isArray(attempt.messages) ? attempt.messages : [];
    const { hiddenMessageIds, olderByLatestId } = reviewMessageLayout(messages);
    for (const message of messages) {
      if (hiddenMessageIds.has(String(message.id))) continue;
      container.appendChild(messageNode(
        message,
        handlers,
        "",
        `message:${attempt.id}:${message.id}`,
      ));
      const olderRevisions = olderByLatestId.get(String(message.id)) || [];
      if (olderRevisions.length) {
        const revisionKey = `revisions:${attempt.id}:${message.id}`;
        const revisions = withTimelineKey(
          element("details", "ak-revision-disclosure"),
          revisionKey,
        );
        revisions.appendChild(withTimelineKey(
          element("summary", "", `查看较早的 ${olderRevisions.length} 个版本`),
          `${revisionKey}:summary`,
        ));
        const revisionList = element("div", "ak-revision-list");
        for (const olderRevision of olderRevisions) {
          revisionList.appendChild(messageNode(
            olderRevision,
            handlers,
            "",
            `message:${attempt.id}:${olderRevision.id}`,
          ));
        }
        revisions.appendChild(revisionList);
        container.appendChild(revisions);
      }
    }

    const actions = Array.isArray(attempt.actions) ? attempt.actions : [];
    const latestAction = actions.at(-1);
    if (latestAction) container.appendChild(renderAction(latestAction, handlers));
    if (actions.length > 1) {
      const actionHistoryKey = `action-history:${attempt.id}`;
      const olderActions = withTimelineKey(
        element("details", "ak-revision-disclosure ak-action-history"),
        actionHistoryKey,
      );
      olderActions.appendChild(withTimelineKey(
        element("summary", "", `查看较早的 ${actions.length - 1} 次审批`),
        `${actionHistoryKey}:summary`,
      ));
      for (const action of actions.slice(0, -1)) {
        olderActions.appendChild(renderAction(action, handlers));
      }
      container.appendChild(olderActions);
    }

    if (THINKING_ATTEMPT_STATUSES.has(String(attempt.status || ""))) {
      container.appendChild(thinkingNode(attempt));
    }
    if (attempt.error_summary) {
      container.appendChild(element("p", "ak-attempt-error", String(attempt.error_summary)));
    }
    if (
      attempt.collapsed === false &&
      RETRYABLE_ATTEMPT_STATUSES.has(String(attempt.status || ""))
    ) {
      const retry = element("button", "secondary ak-attempt-retry", "重新尝试");
      retry.type = "button";
      retry.dataset.timelineRetry = String(attempt.id || "");
      withTimelineKey(retry, `retry:${attempt.id}`);
      retry.addEventListener("click", () => handlers.onRetry?.({
        turnId: String(turn.id || ""),
        attemptId: String(attempt.id || ""),
      }));
      container.appendChild(retry);
    }
  }

  function renderAttempt(turn, attempt, handlers) {
    const status = String(attempt.status || "");
    const statusLabel = ATTEMPT_STATUS_LABELS[status] || "状态未知";
    // 后端投影明确标记旧 Attempt；前端不自行推测 canonical 状态。
    const collapsed = attempt.collapsed !== false;
    if (collapsed) {
      const attemptKey = `attempt:${attempt.id}`;
      const disclosure = withTimelineKey(
        element("details", "ak-attempt-disclosure"),
        attemptKey,
      );
      disclosure.dataset.attemptStatus = status;
      disclosure.appendChild(withTimelineKey(
        element(
          "summary",
          "",
          `第 ${Number(attempt.attempt_no || 1)} 次尝试 ${statusLabel}`,
        ),
        `${attemptKey}:summary`,
      ));
      const content = element("div", "ak-attempt-content");
      appendAttemptContent(content, turn, attempt, handlers);
      disclosure.appendChild(content);
      return disclosure;
    }
    const node = withTimelineKey(element("section", "ak-attempt"), `attempt:${attempt.id}`);
    node.dataset.attemptStatus = status;
    node.setAttribute("aria-label", `第 ${Number(attempt.attempt_no || 1)} 次尝试 ${statusLabel}`);
    appendAttemptContent(node, turn, attempt, handlers);
    return node;
  }

  function latestAnnouncement(timeline) {
    const latestTurn = timelineTurns(timeline).at(-1);
    const attempt = latestTurn?.attempts?.at(-1);
    if (!attempt) return null;
    const status = String(attempt.status || "");
    const text = THINKING_ATTEMPT_STATUSES.has(status)
      ? thinkingLabel(attempt.stage)
      : status === "waiting_for_approval"
        ? "等待你的确认"
        : attempt.error_summary || ATTEMPT_STATUS_LABELS[status] || "状态已更新";
    return {
      key: `${attempt.id}:${status}:${attempt.stage || ""}`,
      text,
    };
  }

  function timelineShell(root) {
    const children = Array.from(root.children || []);
    let live = children.find((child) => child.dataset?.timelineLive != null);
    let content = children.find((child) => child.dataset?.timelineContent != null);
    let notice = children.find((child) => child.dataset?.timelineNotice != null);
    if (!live || !content) {
      const existingContent = children.filter((child) => child !== live && child !== notice);
      live = element("div", "ak-timeline-live");
      live.dataset.timelineLive = "";
      live.setAttribute("role", "status");
      live.setAttribute("aria-live", "polite");
      live.setAttribute("aria-atomic", "true");
      live.setAttribute("tabindex", "-1");
      withTimelineKey(live, "timeline-live");
      content = element("div", "ak-timeline-content");
      content.dataset.timelineContent = "";
      notice = element("p", "conversation-notice error ak-timeline-notice");
      notice.dataset.timelineNotice = "";
      notice.setAttribute("role", "status");
      notice.setAttribute("aria-live", "polite");
      notice.setAttribute("aria-atomic", "true");
      notice.hidden = true;
      withTimelineKey(notice, "timeline-notice");
      root.replaceChildren(live, content, notice);
      content.append(...existingContent);
    } else if (!notice) {
      notice = element("p", "conversation-notice error ak-timeline-notice");
      notice.dataset.timelineNotice = "";
      notice.setAttribute("role", "status");
      notice.setAttribute("aria-live", "polite");
      notice.setAttribute("aria-atomic", "true");
      notice.hidden = true;
      withTimelineKey(notice, "timeline-notice");
      root.appendChild(notice);
    }
    return { content, live, notice };
  }

  function clearNoticeNode(notice) {
    notice.textContent = "";
    notice.hidden = true;
  }

  function setNotice(root, message) {
    if (!root) return;
    const distanceFromBottom = root.scrollHeight - root.scrollTop - root.clientHeight;
    const previousScrollTop = root.scrollTop;
    const { notice } = timelineShell(root);
    notice.textContent = String(message || "");
    notice.hidden = !notice.textContent;
    root.scrollTop = distanceFromBottom <= 80 ? root.scrollHeight : previousScrollTop;
  }

  function clearNotice(root) {
    if (!root) return;
    clearNoticeNode(timelineShell(root).notice);
  }

  function captureRenderState(root, content, live, forceScroll) {
    const distanceFromBottom = root.scrollHeight - root.scrollTop - root.clientHeight;
    const active = document.activeElement;
    return {
      focusKey: content.contains(active)
        ? active?.dataset?.timelineKey || ""
        : live.contains(active)
          ? "timeline-live"
          : "",
      openKeys: new Set(
        Array.from(content.querySelectorAll("details[open][data-timeline-key]"))
          .map((node) => node.dataset.timelineKey),
      ),
      scrollToBottom: Boolean(forceScroll) || distanceFromBottom <= 80,
      scrollTop: root.scrollTop,
    };
  }

  function restoreRenderState(root, content, live, state, focusFallback) {
    const keyedNodes = Array.from(content.querySelectorAll("[data-timeline-key]"));
    for (const node of keyedNodes) {
      if (
        String(node.tagName).toLowerCase() === "details" &&
        state.openKeys.has(node.dataset.timelineKey)
      ) {
        node.open = true;
      }
    }
    let focusTarget = keyedNodes.find((node) => node.dataset.timelineKey === state.focusKey);
    if (!focusTarget && state.focusKey.startsWith("decision:")) {
      const actionId = state.focusKey.split(":")[1] || "";
      focusTarget = keyedNodes.find((node) => node.dataset.timelineKey === `action:${actionId}`);
    }
    if (!focusTarget && state.focusKey && live.textContent) focusTarget = live;
    if (!focusTarget && state.focusKey) focusTarget = focusFallback;
    let focusAncestor = focusTarget?.parentElement || focusTarget?.parentNode;
    while (focusAncestor && focusAncestor !== content) {
      if (String(focusAncestor.tagName).toLowerCase() === "details") {
        focusAncestor.open = true;
      }
      focusAncestor = focusAncestor.parentElement || focusAncestor.parentNode;
    }
    focusTarget?.focus({ preventScroll: true });
    root.scrollTop = state.scrollToBottom ? root.scrollHeight : state.scrollTop;
  }

  function render(root, timeline, handlers = {}) {
    if (!root) return;
    const { content, live, notice } = timelineShell(root);
    const state = captureRenderState(root, content, live, handlers.forceScroll);
    clearNoticeNode(notice);
    const fragment = document.createDocumentFragment();
    for (const turn of timelineTurns(timeline)) {
      const turnNode = element("section", "ak-timeline-turn");
      turnNode.dataset.turnId = String(turn.id || "");
      // 用户消息属于 Turn，只渲染一次，绝不复制到各个 Attempt。
      if (turn.user_message) {
        turnNode.appendChild(messageNode(
          turn.user_message,
          handlers,
          "user",
          `turn-message:${turn.id}`,
        ));
      }
      for (const attempt of turn.attempts || []) {
        turnNode.appendChild(renderAttempt(turn, attempt, handlers));
      }
      fragment.appendChild(turnNode);
    }
    content.replaceChildren(fragment);

    const announcement = latestAnnouncement(timeline);
    if (announcement && root.dataset.timelineAnnouncementKey !== announcement.key) {
      root.dataset.timelineAnnouncementKey = announcement.key;
      live.textContent = announcement.text;
    } else if (!announcement) {
      root.dataset.timelineAnnouncementKey = "";
      live.textContent = "";
    }
    restoreRenderState(root, content, live, state, handlers.focusFallback);
  }

  window.AgentKitChatTimeline = Object.freeze({
    clearNotice,
    createHydrationGuard,
    thinkingLabel,
    hasActiveAttempt,
    latestRetryableAttempt,
    render,
    setNotice,
  });
})(window);
