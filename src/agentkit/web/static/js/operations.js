// Run 360 Inspector：Tab 切换、Run Browser 服务端分页与受控 Artifact Viewer。
// 只渲染页面，只使用 textContent / createElement 输出动态内容，禁止 innerHTML
// 注入 Payload。加载失败时显示权限 / 资源 / 后端错误提示。

(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Tab 切换
  // ------------------------------------------------------------------
  function bindRunTabs() {
    const root = document.querySelector("[data-run-tabs]");
    if (!root) return;
    const tablist = root.querySelector("[data-run-tablist]");
    const tabs = Array.from(root.querySelectorAll("[data-run-tab]"));
    const panels = Array.from(root.querySelectorAll("[data-run-panel]"));
    const defaultTab = document.querySelector("[data-default-run-tab]")?.value || "overview";

    const activate = (name, focus = false) => {
      let active = tabs.find((tab) => tab.dataset.runTab === name);
      if (!active) active = tabs[0];
      for (const tab of tabs) {
        const selected = tab === active;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
      }
      for (const panel of panels) {
        panel.hidden = panel.dataset.runPanel !== active.dataset.runTab;
      }
      if (focus) active.focus();
    };

    for (const tab of tabs) {
      tab.addEventListener("click", () => activate(tab.dataset.runTab, true));
      tab.addEventListener("keydown", (event) => {
        const index = tabs.indexOf(tab);
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          event.preventDefault();
          activate(tabs[(index + 1) % tabs.length].dataset.runTab, true);
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          event.preventDefault();
          activate(tabs[(index - 1 + tabs.length) % tabs.length].dataset.runTab, true);
        } else if (event.key === "Home") {
          event.preventDefault();
          activate(tabs[0].dataset.runTab, true);
        } else if (event.key === "End") {
          event.preventDefault();
          activate(tabs[tabs.length - 1].dataset.runTab, true);
        }
      });
    }
    activate(defaultTab);
  }

  // ------------------------------------------------------------------
  // Run Browser：服务端游标分页（加载更多）
  // ------------------------------------------------------------------
  function bindRunLoadMore() {
    const button = document.querySelector("[data-run-load-more]");
    if (!button) return;
    const list = document.querySelector("[data-run-list]");
    const count = document.querySelector("[data-run-filter-count]");
    if (!list) return;

    button.addEventListener("click", async () => {
      const cursor = button.dataset.nextCursor;
      if (!cursor) return;
      button.disabled = true;
      const originalLabel = button.textContent;
      button.textContent = "加载中…";
      try {
        const response = await fetch(`/api/runs?limit=50&cursor=${encodeURIComponent(cursor)}`, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = await response.json();
        for (const item of body.items || []) {
          list.appendChild(buildRunRow(item));
        }
        if (count) count.textContent = String(list.querySelectorAll("[data-run-row]").length);
        if (body.next_cursor) {
          button.dataset.nextCursor = body.next_cursor;
          button.disabled = false;
          button.textContent = "加载更多";
        } else {
          button.hidden = true;
        }
      } catch {
        // 失败后允许点击重试，而不是永久禁用。
        button.disabled = false;
        button.textContent = "加载失败，点击重试";
        button.setAttribute("data-retry-label", originalLabel);
      }
    });
  }

  function buildRunRow(item) {
    const link = document.createElement("a");
    link.className = "ak-run-list-item";
    link.href = `/operations?run_id=${encodeURIComponent(item.run_id)}#run-detail`;
    link.dataset.runRow = "";
    link.dataset.runStatus = item.status || "";
    link.dataset.runAgent = item.agent_id || "";
    link.dataset.runText = `${item.text || ""} ${item.user_id || ""}`;

    const heading = document.createElement("div");
    const pill = document.createElement("span");
    pill.className = `status-pill status-${String(item.status || "unknown").toLowerCase()}`;
    pill.textContent = String(item.status || "unknown").replace(/_/g, " ");
    heading.appendChild(pill);
    const time = document.createElement("time");
    time.textContent = item.started_at ? formatTs(item.started_at) : "";
    heading.appendChild(time);
    link.appendChild(heading);

    const strong = document.createElement("strong");
    strong.textContent = item.text || "";
    link.appendChild(strong);

    const meta = document.createElement("span");
    meta.textContent = `${item.agent_id || "未记录 Agent"} · ${item.user_id || ""}`;
    link.appendChild(meta);
    return link;
  }

  function formatTs(value) {
    if (!value) return "";
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (n) => String(n).padStart(2, "0");
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    );
  }

  // ------------------------------------------------------------------
  // Artifact Viewer：受控 API，textContent 渲染
  // ------------------------------------------------------------------
  function bindArtifactViewer() {
    const output = document.querySelector("[data-artifact-output]");
    const payloadNode = document.querySelector("[data-artifact-payload]");
    const errorNode = document.querySelector("[data-artifact-error]");
    const titleNode = document.querySelector("[data-artifact-title]");
    const closeButton = document.querySelector("[data-artifact-close]");
    const buttons = Array.from(document.querySelectorAll("[data-artifact-viewer]"));
    if (!output || !payloadNode || !errorNode) return;

    const showError = (message) => {
      errorNode.textContent = message;
      errorNode.hidden = false;
      payloadNode.textContent = "";
    };

    const close = () => {
      output.hidden = true;
      payloadNode.textContent = "";
      errorNode.textContent = "";
      errorNode.hidden = true;
    };

    for (const button of buttons) {
      button.addEventListener("click", async () => {
        close();
        const runId = button.dataset.runId;
        const artifactId = button.dataset.artifactId;
        if (!runId || !artifactId) return;
        output.hidden = false;
        if (titleNode) titleNode.textContent = `Artifact Payload · ${button.dataset.artifactKind || artifactId}`;
        try {
          const response = await fetch(
            `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
            { headers: { Accept: "application/json" } }
          );
          if (response.status === 403) {
            showError("你没有读取 Artifact Payload 的权限（需要 runs:artifact:read）。");
            return;
          }
          if (response.status === 404) {
            showError("Artifact 不存在或不属于当前租户。");
            return;
          }
          if (response.status === 503) {
            showError("观测后端暂不可用，请稍后重试。");
            return;
          }
          if (!response.ok) {
            showError("加载 Artifact Payload 失败。");
            return;
          }
          const body = await response.json();
          if (body.payload_too_large) {
            showError("Payload 超过 256 KiB，不内联展示，仅提供元数据。");
            return;
          }
          if (body.binary_payload) {
            showError("该 Payload 是二进制 / Base64 / 图片内容，不内联展示。");
            return;
          }
          if (body.artifact_payload_unavailable) {
            showError("该 Payload 无法序列化展示。");
            return;
          }
          payloadNode.textContent = JSON.stringify(body.payload, null, 2);
        } catch {
          showError("请求失败，请稍后重试。");
        }
      });
    }
    if (closeButton) closeButton.addEventListener("click", close);
  }

  // ------------------------------------------------------------------
  // Run 选择：无刷新加载详情（局部替换 + 高亮 + URL 同步）
  // ------------------------------------------------------------------
  async function selectRun(runId, rowLink) {
    const list = document.querySelector("[data-run-list]");
    // 1) 高亮选中行（点击时行已可见，不做滚动，避免页面跳动）
    list?.querySelectorAll("[data-run-row]").forEach((el) => el.removeAttribute("aria-current"));
    if (rowLink) {
      rowLink.setAttribute("aria-current", "location");
    } else if (list) {
      const match = Array.from(list.querySelectorAll("[data-run-row]")).find(
        (el) => new URL(el.href, window.location.href).searchParams.get("run_id") === runId
      );
      if (match) match.setAttribute("aria-current", "location");
    }
    // 2) 局部加载详情片段
    // 替换详情区可能触发浏览器 scroll anchoring / 文档高度变化，导致
    // 页面或列表滚动位置漂移；替换后显式恢复原位置（无动画）。
    const savedWinY = window.scrollY;
    const listEl = document.querySelector("[data-run-list]");
    const savedListTop = listEl ? listEl.scrollTop : 0;
    try {
      const response = await fetch(`/operations/run/${encodeURIComponent(runId)}/partial`, {
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      const inspector = document.querySelector("[data-run-detail]");
      if (inspector) {
        inspector.outerHTML = html;
        bindRunTabs();
        bindArtifactViewer();
      }
      history.replaceState(null, "", `/operations?run_id=${encodeURIComponent(runId)}`);
      if (Math.abs(window.scrollY - savedWinY) > 0) window.scrollTo(0, savedWinY);
      if (listEl) listEl.scrollTop = savedListTop;
    } catch {
      // 局部加载失败（后端旧版等）：回退整页导航，保证可用。
      window.location.href = `/operations?run_id=${encodeURIComponent(runId)}`;
    }
  }

  function bindRunSelect() {
    // 鼠标按下时不聚焦链接：Chrome 对 focus 会隐式滚动到可视区，导致点击
    // 时列表/页面位置跳动。键盘 Tab+Enter 仍正常（不走 mousedown）。
    document.addEventListener(
      "mousedown",
      (event) => {
        const link = event.target.closest('a[href*="run_id="]');
        if (!link) return;
        if (!link.closest("[data-run-list]") && !link.closest("[data-run-detail]")) return;
        if (link.target && link.target !== "_self") return;
        event.preventDefault();
      },
      true
    );
    document.addEventListener("click", (event) => {
      const link = event.target.closest('a[href*="run_id="]');
      if (!link) return;
      if (link.target && link.target !== "_self") return;
      if (link.getAttribute("download") != null) return;
      const runId = new URL(link.href, window.location.href).searchParams.get("run_id");
      if (!runId) return;
      // 只拦截运行选择链接（列表行与父子链路），不影响外部链接/表单。
      if (!link.closest("[data-run-list]") && !link.closest("[data-run-detail]")) return;
      event.preventDefault();
      void selectRun(runId, link);
    });
  }

  // URL 带 run_id 时定位到选中行（刷新 / 回退 / 新标签打开场景）。
  function scrollToSelectedRun() {
    const urlRunId = new URL(window.location.href).searchParams.get("run_id");
    if (!urlRunId) return;
    const list = document.querySelector("[data-run-list]");
    if (!list) return;
    const match = Array.from(list.querySelectorAll("[data-run-row]")).find(
      (el) => new URL(el.href, window.location.href).searchParams.get("run_id") === urlRunId
    );
    if (match) {
      match.scrollIntoView({ block: "nearest" });
    } else {
      // 选中运行不在当前第一页：提示用户可加载更多定位。
      const hint = document.querySelector("[data-run-filter-empty]");
      if (hint) {
        hint.textContent = `选中的运行（${urlRunId.slice(0, 8)}…）在更早的记录中，点击「加载更多」可继续查找。`;
        hint.hidden = false;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindRunTabs();
    bindRunLoadMore();
    bindArtifactViewer();
    bindRunSelect();
    scrollToSelectedRun();
  });
})();
