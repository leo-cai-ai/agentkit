(() => {
  const root = document.querySelector("[data-agent-network]");
  if (!root) return;
  const canvas = root.querySelector("[data-network-canvas]");
  const detail = root.querySelector("[data-network-detail]");
  const list = root.querySelector("[data-network-list]");
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
    const edges = (data.relationships || []).filter((edge) => byId.has(edge.source) && byId.has(edge.target));
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
    const rings = { agent: 190, skill: 300, tool: 405 };
    for (const [type, items] of Object.entries(groups)) {
      items.forEach((node, index) => {
        const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(1, items.length);
        node.x = width / 2 + Math.cos(angle) * rings[type];
        node.y = height / 2 + Math.sin(angle) * rings[type] * 0.72;
      });
    }
  }

  function render() {
    if (!graph) return;
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
    for (const edge of graph.edges) {
      const source = graph.byId.get(edge.source);
      const target = graph.byId.get(edge.target);
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.dataset.source = source.id;
      path.dataset.target = target.id;
      path.dataset.edgeType = edge.type;
      path.setAttribute("d", edgePath(source, target));
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
  }

  function edgePath(source, target) {
    const mx = (source.x + target.x) / 2;
    const my = (source.y + target.y) / 2 - Math.min(55, Math.abs(source.x - target.x) * 0.08);
    return `M ${source.x} ${source.y} Q ${mx} ${my} ${target.x} ${target.y}`;
  }

  function createNode(svg, node) {
    const group = document.createElementNS(svg.namespaceURI, "g");
    group.classList.add("ak-network-node", `is-${node.type}`);
    if (node.id === "general_agent") group.classList.add("is-general");
    group.dataset.nodeId = node.id;
    group.dataset.nodeType = node.type;
    group.setAttribute("transform", `translate(${node.x} ${node.y})`);
    group.setAttribute("tabindex", "0");
    group.setAttribute("role", "button");
    group.setAttribute("aria-label", `${node.type}: ${node.label}`);
    const halo = document.createElementNS(svg.namespaceURI, "circle");
    halo.setAttribute("r", node.id === "general_agent" ? "58" : "42");
    halo.classList.add("ak-node-halo");
    const body = document.createElementNS(svg.namespaceURI, "circle");
    body.setAttribute("r", node.id === "general_agent" ? "45" : "32");
    body.classList.add("ak-node-body");
    const title = document.createElementNS(svg.namespaceURI, "text");
    title.classList.add("ak-node-title");
    title.setAttribute("text-anchor", "middle");
    title.setAttribute("y", node.id === "general_agent" ? "4" : "3");
    title.textContent = node.label.length > 17 ? `${node.label.slice(0, 15)}…` : node.label;
    const type = document.createElementNS(svg.namespaceURI, "text");
    type.classList.add("ak-node-type");
    type.setAttribute("text-anchor", "middle");
    type.setAttribute("y", node.id === "general_agent" ? "21" : "17");
    type.textContent = node.type.toUpperCase();
    group.append(halo, body, title, type);
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

  function selectNode(nodeId) {
    const node = graph.byId.get(nodeId);
    canvas.querySelectorAll(".ak-network-node").forEach((element) => {
      element.classList.toggle("is-selected", element.dataset.nodeId === nodeId);
    });
    canvas.querySelectorAll(".ak-network-edges path").forEach((edge) => {
      edge.classList.toggle("is-highlighted", edge.dataset.source === nodeId || edge.dataset.target === nodeId);
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
  }

  function applyScale() {
    const viewport = canvas.querySelector("[data-viewport]");
    if (viewport) viewport.setAttribute("transform", `translate(${width * (1 - scale) / 2} ${height * (1 - scale) / 2}) scale(${scale})`);
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
    const rows = graph.nodes.map((node) => `<li><strong>${escapeHtml(node.label)}</strong><span>${escapeHtml(node.type)} · ${escapeHtml(node.data?.description || node.id)}</span></li>`).join("");
    list.innerHTML = `<ul>${rows}</ul>`;
  }

  root.querySelectorAll("[data-network-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.networkFilter;
      root.querySelectorAll("[data-network-filter]").forEach((item) => item.classList.toggle("active", item === button));
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

  fetch("/api/registry")
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((data) => {
      graph = buildGraph(data);
      render();
      selectNode(graph.byId.has("general_agent") ? "general_agent" : graph.nodes[0]?.id);
    })
    .catch((error) => {
      canvas.innerHTML = `<div class="ak-network-error">无法加载 Agent Network：${escapeHtml(error.message)}</div>`;
    });
})();
