const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.scrollHeight = 0;
    this.scrollTop = 0;
  }

  append(...nodes) {
    nodes.forEach((node) => this.appendChild(node));
  }

  appendChild(node) {
    if (node.tagName === "#fragment") this.children.push(...node.children);
    else this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }
}

const document = {
  createElement: (tagName) => new FakeNode(tagName),
  createDocumentFragment: () => new FakeNode("#fragment"),
};
const context = { document, window: {} };
vm.createContext(context);
const source = fs.readFileSync(
  path.join(__dirname, "../../src/agentkit/web/static/js/chat_timeline.js"),
  "utf8",
);
vm.runInContext(source, context);
const timelineUi = context.window.AgentKitChatTimeline;

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

function withClass(root, className) {
  return descendants(root).filter((node) => node.className.split(" ").includes(className));
}

assert.equal(timelineUi.thinkingLabel("publishing"), "正在发布内容");
assert.equal(timelineUi.thinkingLabel("untrusted-stage"), "正在处理");

const timeline = {
  turns: [{
    id: "turn-1",
    user_message: { role: "user", content: "请发布这篇内容" },
    attempts: [{
      id: "attempt-1",
      attempt_no: 1,
      status: "failed",
      collapsed: true,
      error_summary: "发布失败",
      messages: [{ id: 1, role: "assistant", kind: "assistant_output", content: "第一版" }],
      actions: [],
    }, {
      id: "attempt-2",
      attempt_no: 2,
      status: "waiting_for_approval",
      stage: "awaiting_user_decision",
      collapsed: false,
      messages: [
        { id: 2, role: "assistant", kind: "assistant_output", content: "旧版" },
        { id: 3, role: "assistant", kind: "assistant_revision", supersedes_message_id: 2, content: "最新版" },
      ],
      actions: [{ id: "action-1", status: "pending", version: 4, preview: { title: "发布预览" } }],
    }],
  }],
};

assert.equal(timelineUi.hasActiveAttempt(timeline), true);
assert.equal(timelineUi.latestRetryableAttempt(timeline).attemptId, "attempt-1");

const decisions = [];
const root = new FakeNode("main");
timelineUi.render(root, timeline, {
  agentLabel: () => "小红书 Agent",
  onDecision: (command) => decisions.push(command),
});

assert.equal(withClass(root, "user").length, 1, "Turn 的用户消息只能出现一次");
assert.equal(withClass(root, "ak-attempt-disclosure").length, 1, "旧 Attempt 默认折叠");
assert.equal(withClass(root, "ak-attempt").length, 1, "latest Attempt 默认展开");
assert.equal(withClass(root, "ak-revision-disclosure").length, 1, "旧 revision 收进 disclosure");
assert.equal(withClass(root, "ak-timeline-action-buttons").length, 1, "pending Action 显示按钮");
assert.equal(withClass(root, "ak-thinking").length, 0, "等待审批不是循环 Thinking");

const buttons = descendants(root).filter((node) => node.tagName === "button");
const approve = buttons.find((node) => node.dataset.timelineDecision === "approved");
approve.listeners.click();
assert.equal(decisions[0].actionId, "action-1");
assert.equal(decisions[0].decision, "approved");
assert.equal(decisions[0].expectedVersion, 4);

timeline.turns[0].attempts[1].status = "running";
timeline.turns[0].attempts[1].stage = "publishing";
timeline.turns[0].attempts[1].actions[0].status = "approved";
timeline.turns[0].attempts[1].actions[0].decision = "approved";
timelineUi.render(root, timeline);

assert.equal(withClass(root, "ak-timeline-action-buttons").length, 0, "非 pending Action 不显示按钮");
assert.equal(withClass(root, "ak-thinking").length, 1, "运行中 Attempt 显示 Thinking");
assert.equal(withClass(root, "ak-thinking-bars")[0].children.length, 4, "Thinking 使用四根动效条");

timeline.turns[0].attempts[1].status = "failed";
timeline.turns[0].attempts[1].error_summary = "批准后发布失败";
timelineUi.render(root, timeline);

assert.equal(withClass(root, "ak-action-resolution")[0].textContent, "已批准");
assert.equal(withClass(root, "ak-attempt-retry").length, 1, "只有 latest 失败 Attempt 保留 retry 控件");

console.log("chat_timeline: ok");
