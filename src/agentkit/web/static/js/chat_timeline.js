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

  function element(tagName, className = "", text = "") {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function messageNode(message, handlers, roleOverride = "") {
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
    return node;
  }

  function thinkingNode(attempt) {
    const node = element("div", "ak-thinking");
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
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
    const title = preview.title || preview.summary || preview.card_text || "";
    if (title) container.appendChild(element("p", "ak-action-preview", String(title)));
    const skills = Array.isArray(action?.skills) ? action.skills.filter(Boolean) : [];
    if (skills.length) {
      container.appendChild(element("p", "ak-action-skills", `涉及能力：${skills.join("、")}`));
    }
  }

  function renderAction(action, handlers) {
    const node = element("section", "ak-timeline-action");
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

  function splitReviewMessages(messages) {
    const revisions = messages.filter((message) => message.kind === "assistant_revision");
    if (!revisions.length) return { regularMessages: messages, latestRevision: null, olderRevisions: [] };
    const revisionIds = new Set(revisions.map((message) => Number(message.id)));
    for (const revision of revisions) {
      if (revision.supersedes_message_id != null) {
        revisionIds.add(Number(revision.supersedes_message_id));
      }
    }
    const reviewMessages = messages.filter((message) => revisionIds.has(Number(message.id)));
    return {
      regularMessages: messages.filter((message) => !revisionIds.has(Number(message.id))),
      latestRevision: reviewMessages.at(-1) || revisions.at(-1),
      olderRevisions: reviewMessages.slice(0, -1),
    };
  }

  function appendAttemptContent(container, turn, attempt, handlers) {
    const messages = Array.isArray(attempt.messages) ? attempt.messages : [];
    const { regularMessages, latestRevision, olderRevisions } = splitReviewMessages(messages);
    for (const message of regularMessages) {
      container.appendChild(messageNode(message, handlers));
    }
    if (latestRevision) container.appendChild(messageNode(latestRevision, handlers));
    if (olderRevisions.length) {
      const revisions = element("details", "ak-revision-disclosure");
      revisions.appendChild(element("summary", "", `查看较早的 ${olderRevisions.length} 个版本`));
      const revisionList = element("div", "ak-revision-list");
      for (const message of olderRevisions) {
        revisionList.appendChild(messageNode(message, handlers));
      }
      revisions.appendChild(revisionList);
      container.appendChild(revisions);
    }

    const actions = Array.isArray(attempt.actions) ? attempt.actions : [];
    const latestAction = actions.at(-1);
    if (latestAction) container.appendChild(renderAction(latestAction, handlers));
    if (actions.length > 1) {
      const olderActions = element("details", "ak-revision-disclosure ak-action-history");
      olderActions.appendChild(element("summary", "", `查看较早的 ${actions.length - 1} 次审批`));
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
      const disclosure = element("details", "ak-attempt-disclosure");
      disclosure.dataset.attemptStatus = status;
      disclosure.appendChild(element(
        "summary",
        "",
        `第 ${Number(attempt.attempt_no || 1)} 次尝试 ${statusLabel}`,
      ));
      const content = element("div", "ak-attempt-content");
      appendAttemptContent(content, turn, attempt, handlers);
      disclosure.appendChild(content);
      return disclosure;
    }
    const node = element("section", "ak-attempt");
    node.dataset.attemptStatus = status;
    node.setAttribute("aria-label", `第 ${Number(attempt.attempt_no || 1)} 次尝试 ${statusLabel}`);
    appendAttemptContent(node, turn, attempt, handlers);
    return node;
  }

  function render(root, timeline, handlers = {}) {
    if (!root) return;
    const fragment = document.createDocumentFragment();
    for (const turn of timelineTurns(timeline)) {
      const turnNode = element("section", "ak-timeline-turn");
      turnNode.dataset.turnId = String(turn.id || "");
      // 用户消息属于 Turn，只渲染一次，绝不复制到各个 Attempt。
      if (turn.user_message) {
        turnNode.appendChild(messageNode(turn.user_message, handlers, "user"));
      }
      for (const attempt of turn.attempts || []) {
        turnNode.appendChild(renderAttempt(turn, attempt, handlers));
      }
      fragment.appendChild(turnNode);
    }
    root.replaceChildren(fragment);
    root.scrollTop = root.scrollHeight;
  }

  window.AgentKitChatTimeline = Object.freeze({
    thinkingLabel,
    hasActiveAttempt,
    latestRetryableAttempt,
    render,
  });
})(window);
