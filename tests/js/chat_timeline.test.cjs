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
    this.clientHeight = 0;
    this.parentNode = null;
    this.open = false;
  }

  append(...nodes) {
    nodes.forEach((node) => this.appendChild(node));
  }

  appendChild(node) {
    if (node.tagName === "#fragment") {
      node.children.forEach((child) => {
        child.parentNode = this;
        this.children.push(child);
      });
    } else {
      node.parentNode = this;
      this.children.push(node);
    }
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  contains(node) {
    return this === node || this.children.some((child) => child.contains(node));
  }

  focus() {
    document.activeElement = this;
  }

  querySelectorAll(selector) {
    return descendants(this).filter((node) => {
      if (selector === "[data-timeline-key]") {
        return Object.hasOwn(node.dataset, "timelineKey");
      }
      if (selector === "details[open][data-timeline-key]") {
        return node.tagName === "details" && node.open && Object.hasOwn(node.dataset, "timelineKey");
      }
      return false;
    });
  }
}

const document = {
  createElement: (tagName) => new FakeNode(tagName),
  createDocumentFragment: () => new FakeNode("#fragment"),
  activeElement: null,
};
const context = { AbortController, document, window: {} };
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

function singleClass(root, className) {
  const matches = withClass(root, className);
  assert.equal(matches.length, 1, `expected one .${className}`);
  return matches[0];
}

function withDataset(root, key) {
  return descendants(root).filter((node) => Object.hasOwn(node.dataset, key));
}

function singleDataset(root, key) {
  const matches = withDataset(root, key);
  assert.equal(matches.length, 1, `expected one [data-${key}]`);
  return matches[0];
}

assert.equal(timelineUi.thinkingLabel("publishing"), "正在发布内容");
assert.equal(timelineUi.thinkingLabel("untrusted-stage"), "正在处理");

assert.equal(typeof timelineUi.createHydrationGuard, "function", "renderer 公开 hydration guard");
const hydrationGuard = timelineUi.createHydrationGuard();
const queuedRequest = hydrationGuard.begin("conversation-1");
const succeededRequest = hydrationGuard.begin("conversation-1");
assert.equal(queuedRequest.signal.aborted, true, "新 hydration 会中止同会话旧请求");
assert.equal(hydrationGuard.commit(succeededRequest, 2), true);
assert.equal(hydrationGuard.commit(queuedRequest, 1), false, "慢到达的 queued 响应不能覆盖 succeeded");
const regressedVersion = hydrationGuard.begin("conversation-1");
assert.equal(hydrationGuard.commit(regressedVersion, 1), false, "Timeline version 只能单调前进");

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
      actions: [{
        id: "action-1",
        status: "pending",
        version: 4,
        preview: {
          title: "发布预览",
          summary: "这是摘要",
          card_text: "这是审批前必须看见的正文核心",
          media_strategy: "xhs_text_image",
          card_style: "notebook",
          media_preview_urls: [
            "https://example.test/1.png",
            "https://example.test/2.png",
            "https://example.test/3.png",
            "https://example.test/4.png",
            "https://example.test/5.png",
            "javascript:alert(1)",
          ],
        },
      }],
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
assert.equal(singleClass(root, "ak-action-preview-title").textContent, "发布预览");
assert.equal(singleClass(root, "ak-action-preview-summary").textContent, "这是摘要");
assert.equal(singleClass(root, "ak-action-preview-body").textContent, "这是审批前必须看见的正文核心");
assert.equal(singleClass(root, "ak-action-media-summary").textContent.includes("5"), true);
const mediaLinks = withClass(root, "ak-action-media-link");
const mediaImages = withClass(root, "ak-action-media-thumbnail");
assert.equal(mediaLinks.length, 4, "安全缩略图最多显示四个");
assert.equal(mediaImages.length, 4);
for (let index = 0; index < mediaLinks.length; index += 1) {
  assert.equal(mediaLinks[index].attributes.target, "_blank");
  assert.equal(mediaLinks[index].attributes.rel, "noopener noreferrer");
  assert.equal(mediaImages[index].attributes.loading, "lazy");
  assert.equal(mediaImages[index].attributes.alt, `媒体预览 ${index + 1}`);
  assert.equal(mediaImages[index].attributes.width, "160");
  assert.equal(mediaImages[index].attributes.height, "120");
}
assert.equal(singleClass(root, "ak-action-media-more").textContent.includes("1"), true);

const buttons = descendants(root).filter((node) => node.tagName === "button");
const approve = buttons.find((node) => node.dataset.timelineDecision === "approved");
approve.listeners.click();
assert.equal(decisions[0].actionId, "action-1");
assert.equal(decisions[0].decision, "approved");
assert.equal(decisions[0].expectedVersion, 4);

const rolloverTimeline = structuredClone(timeline);
rolloverTimeline.turns[0].attempts[1].actions[0].status = "completed";
rolloverTimeline.turns[0].attempts[1].actions[0].decision = "approved";
rolloverTimeline.turns[0].attempts[1].actions.push({
  id: "action-2",
  status: "pending",
  version: 1,
  preview: { title: "第二版" },
});
approve.focus();
timelineUi.render(root, rolloverTimeline, { focusFallback: new FakeNode("textarea") });
assert.equal(document.activeElement.dataset.timelineKey, "action:action-1");
let focusedParent = document.activeElement.parentNode;
while (focusedParent && !focusedParent.className.includes("ak-action-history")) {
  focusedParent = focusedParent.parentNode;
}
assert.equal(focusedParent?.open, true, "fallback focus 所在 disclosure 必须展开");

timeline.turns[0].attempts[1].status = "running";
timeline.turns[0].attempts[1].stage = "publishing";
timeline.turns[0].attempts[1].actions[0].status = "approved";
timeline.turns[0].attempts[1].actions[0].decision = "approved";
const composer = new FakeNode("textarea");
timelineUi.render(root, timeline, { focusFallback: composer });

assert.equal(withClass(root, "ak-timeline-action-buttons").length, 0, "非 pending Action 不显示按钮");
assert.equal(withClass(root, "ak-thinking").length, 1, "运行中 Attempt 显示 Thinking");
assert.equal(withClass(root, "ak-thinking-bars")[0].children.length, 4, "Thinking 使用四根动效条");
assert.equal(document.activeElement.dataset.timelineKey, "action:action-1", "decision 消失后聚焦同 Action 状态");

timeline.turns[0].attempts[1].actions = [];
timelineUi.render(root, timeline, { focusFallback: composer });
assert.equal(document.activeElement.dataset.timelineKey, "timeline-live", "Action 消失后聚焦 latest live status");

const focusedMessage = withClass(root, "assistant")[0];
focusedMessage.focus();
timelineUi.render(root, { turns: [] }, { focusFallback: composer });
assert.equal(document.activeElement, composer, "状态也消失时回到 Composer");

timeline.turns[0].attempts[1].actions = [{
  id: "action-1",
  status: "approved",
  decision: "approved",
  version: 5,
  preview: {},
}];

timeline.turns[0].attempts[1].status = "failed";
timeline.turns[0].attempts[1].error_summary = "批准后发布失败";
timelineUi.render(root, timeline);

assert.equal(withClass(root, "ak-action-resolution")[0].textContent, "已批准");
assert.equal(withClass(root, "ak-attempt-retry").length, 1, "只有 latest 失败 Attempt 保留 retry 控件");

const orderedTimeline = {
  turns: [{
    id: "turn-order",
    user_message: { role: "user", content: "检查顺序" },
    attempts: [{
      id: "attempt-order",
      attempt_no: 1,
      status: "succeeded",
      collapsed: false,
      actions: [],
      messages: [
        { id: 10, role: "assistant", kind: "assistant_output", content: "初稿" },
        { id: 11, role: "assistant", kind: "assistant_revision", supersedes_message_id: 10, content: "最新审核稿" },
        { id: 12, role: "assistant", kind: "assistant_output", supersedes_message_id: 11, content: "发布完成" },
      ],
    }],
  }],
};
timelineUi.render(root, orderedTimeline);
const orderedMessages = withClass(root, "assistant").map((node) => (
  descendants(node).find((child) => child.tagName === "p")?.textContent
));
assert.deepEqual(orderedMessages, ["最新审核稿", "初稿", "发布完成"], "revision disclosure 必须留在原始位置");

const stableTimeline = structuredClone(orderedTimeline);
stableTimeline.turns[0].attempts.unshift({
  id: "attempt-old",
  attempt_no: 0,
  status: "failed",
  collapsed: true,
  actions: [],
  messages: [],
});
root.scrollHeight = 1000;
root.clientHeight = 300;
root.scrollTop = 120;
timelineUi.render(root, stableTimeline);
const contentBefore = singleDataset(root, "timelineContent");
const disclosureBefore = withClass(root, "ak-attempt-disclosure")[0];
disclosureBefore.open = true;
const focusBefore = disclosureBefore.children[0];
focusBefore.focus();

stableTimeline.version = 2;
stableTimeline.turns[0].attempts[1].version = 2;
timelineUi.render(root, stableTimeline);
const contentAfter = singleDataset(root, "timelineContent");
const liveAfter = singleClass(root, "ak-timeline-live");
const disclosureAfter = withClass(root, "ak-attempt-disclosure")[0];
assert.equal(contentAfter, contentBefore, "Timeline 内容容器必须保持稳定");
assert.equal(disclosureAfter.open, true, "hydration 后保持 disclosure 展开状态");
assert.equal(root.scrollTop, 120, "用户离底部较远时不得强制滚底");
assert.equal(root.contains(document.activeElement), true, "hydration 后保持键盘焦点");
assert.equal(withClass(root, "ak-timeline-live").length, 1, "只有独立 latest 状态 live region");

root.scrollTop = 710;
root.scrollHeight = 1000;
root.clientHeight = 300;
timelineUi.render(root, stableTimeline);
assert.equal(root.scrollTop, root.scrollHeight, "接近底部时跟随最新状态");
assert.equal(singleClass(root, "ak-timeline-live"), liveAfter, "live region 节点保持稳定，避免重播历史");

root.scrollTop = 100;
timelineUi.render(root, stableTimeline, { forceScroll: true });
assert.equal(root.scrollTop, root.scrollHeight, "当前命令自身更新时跟随到底部");

assert.equal(typeof timelineUi.setNotice, "function");
assert.equal(typeof timelineUi.clearNotice, "function");
root.scrollHeight = 1000;
root.clientHeight = 300;
root.scrollTop = 100;
timelineUi.setNotice(root, "网络连接中断");
assert.equal(root.scrollTop, 100, "错误 notice 不打断正在阅读历史的用户");
const noticeBefore = singleClass(root, "ak-timeline-notice");
assert.equal(noticeBefore.textContent, "网络连接中断");
assert.equal(noticeBefore.hidden, false);

timelineUi.render(root, stableTimeline);
const noticeAfter = singleClass(root, "ak-timeline-notice");
assert.equal(noticeAfter, noticeBefore, "notice 使用稳定 keyed 节点");
assert.equal(noticeAfter.hidden, true, "成功 hydration 清除 notice");
assert.equal(noticeAfter.textContent, "");

root.scrollTop = 710;
timelineUi.setNotice(root, "网络连接中断");
assert.equal(root.scrollTop, root.scrollHeight, "原本接近底部时 notice 跟随到底部");

console.log("chat_timeline: ok");
