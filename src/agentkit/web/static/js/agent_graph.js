(() => {
  const root = document.querySelector("[data-agent-network]");
  if (!root) return;

  const canvas = root.querySelector("[data-network-canvas]");
  const detail = root.querySelector("[data-network-detail]");
  const list = root.querySelector("[data-network-list]");
  const stage = root.querySelector(".ak-network-stage");
  const statusMessage = root.querySelector("[data-network-status-message]");
  const retryButton = root.querySelector("[data-network-retry]");
  const width = 1200;
  const height = 760;
  let scale = 1;
  let activeFilter = "all";
  let graph = null;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  function setNetworkState(state, message = "") {
    root.dataset.state = state;
    stage?.setAttribute("aria-busy", String(state === "loading"));
    if (statusMessage) statusMessage.textContent = message;
    if (retryButton) retryButton.hidden = state !== "error";
  }

  function buildGraph(data) {
    const nodes = [];
    const byId = new Map();
    const add = (id, type, value) => {
      if (byId.has(id)) return byId.get(id);
      const node = { id, type, ...value };
      byId.set(id, node);
      nodes.push(node);
      return node;
    };
    for (const agent of data.agents || []) {
      add(agent.name, "agent", { label: agent.label || agent.name, data: agent });
    }
    for (const skill of data.skills || []) {
      add(skill.name, "skill", { label: skill.name, data: skill });
    }
    for (const tool of data.tools || []) {
      add(tool.name, "tool", { label: tool.name, data: tool });
    }
    const edges = (data.relationships || [])
      .filter((relationship) => byId.has(relationship.source) && byId.has(relationship.target))
      .map((relationship) => ({
        ...relationship,
        active: relationship.active === true,
      }));
    return { nodes, edges, byId };
  }

  function placeNodes(nodes) {
    const groups = {
      agent: nodes.filter((node) => node.type === "agent" && node.id !== "general_agent"),
      skill: nodes.filter((node) => node.type === "skill"),
      tool: nodes.filter((node) => node.type === "tool"),
    };
    const general = nodes.find((node) => node.id === "general_agent");
    if (general) Object.assign(general, { x: width / 2, y: height / 2 });
    const rings = { agent: 190, skill: 305, tool: 420 };
    for (const [type, items] of Object.entries(groups)) {
      items.forEach((node, index) => {
        const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(1, items.length);
        node.x = width / 2 + Math.cos(angle) * rings[type];
        node.y = height / 2 + Math.sin(angle) * rings[type] * 0.7;
      });
    }
  }

  function nodeGeometry(node) {
    if (node.id === "general_agent") return { width: 160, height: 76, radius: 16 };
    if (node.type === "agent") return { width: 138, height: 62, radius: 14 };
    if (node.type === "skill") return { width: 118, height: 52, radius: 10 };
    return { width: 108, height: 44, radius: 8 };
  }

  function nodeIcon(node) {
    if (node.type === "agent") return "message-circle";
    if (node.type === "skill") return "topology-star";
    return "activity";
  }

  function truncateNodeLabel(label, geometry) {
    const availableWidth = geometry.width - 48;
    const maxCharacters = Math.max(6, Math.floor(availableWidth / 6.4));
    return label.length > maxCharacters
      ? `${label.slice(0, maxCharacters - 1)}…`
      : label;
  }

  function edgePath(source, target) {
    const mx = (source.x + target.x) / 2;
    const my = (source.y + target.y) / 2 - Math.min(55, Math.abs(source.x - target.x) * 0.08);
    return `M ${source.x} ${source.y} Q ${mx} ${my} ${target.x} ${target.y}`;
  }

  function createNode(svg, node) {
    const geometry = nodeGeometry(node);
    const group = document.createElementNS(svg.namespaceURI, "g");
    group.classList.add("ak-network-node", `is-${node.type}`);
    if (node.id === "general_agent") group.classList.add("is-general");
    group.dataset.nodeId = node.id;
    group.dataset.nodeType = node.type;
    group.setAttribute("transform", `translate(${node.x} ${node.y})`);
    group.setAttribute("tabindex", "0");
    group.setAttribute("role", "button");
    group.setAttribute("aria-label", `${node.type}: ${node.label}`);
    const fullTitle = document.createElementNS(svg.namespaceURI, "title");
    fullTitle.textContent = node.label;

    const halo = document.createElementNS(svg.namespaceURI, "rect");
    halo.setAttribute("x", String(-geometry.width / 2 - 6));
    halo.setAttribute("y", String(-geometry.height / 2 - 6));
    halo.setAttribute("width", String(geometry.width + 12));
    halo.setAttribute("height", String(geometry.height + 12));
    halo.setAttribute("rx", String(geometry.radius + 4));
    halo.classList.add("ak-node-halo");

    const body = document.createElementNS(svg.namespaceURI, "rect");
    body.setAttribute("x", String(-geometry.width / 2));
    body.setAttribute("y", String(-geometry.height / 2));
    body.setAttribute("width", String(geometry.width));
    body.setAttribute("height", String(geometry.height));
    body.setAttribute("rx", String(geometry.radius));
    body.classList.add("ak-node-body");

    const icon = document.createElementNS(svg.namespaceURI, "use");
    icon.setAttribute("href", `/static/icons/tabler-sprite.svg#icon-${nodeIcon(node)}`);
    icon.setAttribute("x", String(-geometry.width / 2 + 12));
    icon.setAttribute("y", "-8");
    icon.setAttribute("width", "16");
    icon.setAttribute("height", "16");
    icon.classList.add("ak-node-icon");

    const title = document.createElementNS(svg.namespaceURI, "text");
    title.classList.add("ak-node-title");
    title.setAttribute("text-anchor", "start");
    title.setAttribute("x", String(-geometry.width / 2 + 34));
    title.setAttribute("y", "-2");
    title.textContent = truncateNodeLabel(node.label, geometry);

    const type = document.createElementNS(svg.namespaceURI, "text");
    type.classList.add("ak-node-type");
    type.setAttribute("text-anchor", "middle");
    type.setAttribute("x", "8");
    type.setAttribute("y", "16");
    type.textContent = node.type.toUpperCase();
    group.append(fullTitle, halo, body, icon, title, type);

    const select = () => selectNode(node.id);
    group.addEventListener("click", select);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
    return group;
  }

  function render() {
    if (!graph) return;
    if (!graph.nodes.length) {
      canvas.replaceChildren();
      renderList();
      detail.innerHTML = '<span class="ak-eyebrow">空状态</span><h2>尚未注册节点</h2><p>请先注册 Agent、Skill 或 Tool。</p>';
      setNetworkState("empty", "Registry 中还没有可展示的节点。");
      return;
    }

    placeNodes(graph.nodes);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "General Agent、业务 Agent、Skills 和 Tools 的关系图");
    const viewport = document.createElementNS(svg.namespaceURI, "g");
    viewport.dataset.viewport = "true";
    svg.appendChild(viewport);

    const edgeLayer = document.createElementNS(svg.namespaceURI, "g");
    edgeLayer.classList.add("ak-network-edges");
    for (const relationship of graph.edges) {
      const source = graph.byId.get(relationship.source);
      const target = graph.byId.get(relationship.target);
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.dataset.source = source.id;
      path.dataset.target = target.id;
      path.dataset.edgeType = relationship.type;
      path.setAttribute("d", edgePath(source, target));
      path.classList.toggle("is-active-run", relationship.active === true);
      edgeLayer.appendChild(path);
    }
    viewport.appendChild(edgeLayer);

    const nodeLayer = document.createElementNS(svg.namespaceURI, "g");
    nodeLayer.classList.add("ak-network-nodes");
    for (const node of graph.nodes) nodeLayer.appendChild(createNode(svg, node));
    viewport.appendChild(nodeLayer);
    canvas.replaceChildren(svg);
    applyScale();
    applyFilter();
    bindDragging(svg);
    renderList();
    setNetworkState("ready");
    selectNode(graph.byId.has("general_agent") ? "general_agent" : graph.nodes[0].id);
  }

  function selectNode(nodeId) {
    const node = graph?.byId.get(nodeId);
    if (!node) return;
    canvas.querySelectorAll(".ak-network-node").forEach((element) => {
      element.classList.toggle("is-selected", element.dataset.nodeId === nodeId);
    });
    canvas.querySelectorAll(".ak-network-edges path").forEach((edge) => {
      edge.classList.toggle("is-highlighted", edge.dataset.source === nodeId || edge.dataset.target === nodeId);
    });
    list.querySelectorAll("[data-network-list-node]").forEach((button) => {
      button.setAttribute("aria-current", button.dataset.networkListNode === nodeId ? "true" : "false");
    });
    const data = node.data || {};
    const facts = node.type === "agent"
      ? [["Domain", data.domain], ["Skills", (data.skills || []).join("、")], ["策略", (data.allowed_strategies || []).join("、")]]
      : node.type === "skill"
        ? [["Domain", data.domain], ["Reasoning", data.reasoning], ["Orchestration", data.orchestration], ["Tools", (data.tools || []).join("、")]]
        : [["Domain", data.domain], ["Provider", data.provider], ["Risk", data.risk]];
    detail.innerHTML = `
      <span class="ak-eyebrow">${escapeHtml(node.type)}</span>
      <h2>${escapeHtml(node.label)}</h2>
      <p>${escapeHtml(data.description || "已注册的运行时能力节点。")}</p>
      <dl>${facts.filter(([, value]) => value).map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>
    `;
  }

  function bindDragging(svg) {
    let drag = null;
    svg.addEventListener("pointerdown", (event) => {
      const element = event.target.closest(".ak-network-node");
      if (!element) return;
      const node = graph.byId.get(element.dataset.nodeId);
      drag = { node, element };
      element.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const rect = svg.getBoundingClientRect();
      drag.node.x = ((event.clientX - rect.left) / rect.width) * width;
      drag.node.y = ((event.clientY - rect.top) / rect.height) * height;
      drag.element.setAttribute("transform", `translate(${drag.node.x} ${drag.node.y})`);
      canvas.querySelectorAll(".ak-network-edges path").forEach((path) => {
        const source = graph.byId.get(path.dataset.source);
        const target = graph.byId.get(path.dataset.target);
        path.setAttribute("d", edgePath(source, target));
      });
    });
    svg.addEventListener("pointerup", () => { drag = null; });
    svg.addEventListener("pointercancel", () => { drag = null; });
  }

  function applyScale() {
    const viewport = canvas.querySelector("[data-viewport]");
    if (viewport) {
      viewport.setAttribute("transform", `translate(${width * (1 - scale) / 2} ${height * (1 - scale) / 2}) scale(${scale})`);
    }
  }

  function applyFilter() {
    if (!graph) return;
    canvas.querySelectorAll(".ak-network-node").forEach((node) => {
      const visible = activeFilter === "all" || node.dataset.nodeType === activeFilter || node.dataset.nodeId === "general_agent";
      node.classList.toggle("is-filtered", !visible);
    });
    canvas.querySelectorAll(".ak-network-edges path").forEach((edge) => {
      const source = graph.byId.get(edge.dataset.source);
      const target = graph.byId.get(edge.dataset.target);
      const visible = activeFilter === "all" || source.type === activeFilter || target.type === activeFilter;
      edge.classList.toggle("is-filtered", !visible);
    });
  }

  function renderList() {
    if (!graph) return;
    const labels = { agent: "Agents", skill: "Skills", tool: "Tools" };
    list.innerHTML = ["agent", "skill", "tool"].map((type) => {
      const nodes = graph.nodes.filter((node) => node.type === type);
      if (!nodes.length) return "";
      const buttons = nodes.map((node) => `
        <button type="button" data-network-list-node="${escapeHtml(node.id)}" aria-current="false">
          <strong>${escapeHtml(node.label)}</strong>
          <span>${escapeHtml(node.data?.description || node.id)}</span>
        </button>
      `).join("");
      return `<section><h3>${labels[type]}</h3><div>${buttons}</div></section>`;
    }).join("");
  }

  async function loadNetwork() {
    setNetworkState("loading", "正在加载 Agent Network…");
    try {
      const response = await fetch("/api/registry");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      graph = buildGraph(await response.json());
      render();
    } catch {
      graph = null;
      canvas.replaceChildren();
      setNetworkState("error", "无法加载 Agent Network。请检查 Registry 后重试。");
    }
  }

  root.querySelectorAll("[data-network-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.networkFilter;
      root.querySelectorAll("[data-network-filter]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      applyFilter();
    });
  });
  root.querySelectorAll("[data-network-zoom]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.networkZoom;
      scale = action === "reset" ? 1 : Math.max(0.65, Math.min(1.55, scale + (action === "in" ? 0.1 : -0.1)));
      applyScale();
    });
  });
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-network-list-node]");
    if (button) selectNode(button.dataset.networkListNode);
  });
  retryButton?.addEventListener("click", loadNetwork);
  loadNetwork();
})();
