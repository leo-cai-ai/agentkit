/* 《红楼梦》Neo4j 知识图谱可视化（轻量力导向布局，无外部依赖）。 */
(function () {
  "use strict";

  const LABEL_COLORS = {
    Character: "#7048e8",
    Family: "#f59f00",
    Place: "#37b24d",
    Object: "#1c7ed6",
    Event: "#e64980",
  };
  const LABEL_ORDER = ["Character", "Family", "Place", "Object", "Event"];

  const svg = document.querySelector("[data-hlm-svg]");
  const canvas = document.querySelector("[data-hlm-canvas]");
  const empty = document.querySelector("[data-hlm-empty]");
  const status = document.querySelector("[data-hlm-status]");
  const searchInput = document.querySelector("[data-hlm-entity-search]");
  const resetBtn = document.querySelector("[data-hlm-reset]");
  const askForm = document.querySelector("[data-hlm-ask-form]");
  const question = document.querySelector("[data-hlm-question]");
  const askSubmit = document.querySelector("[data-hlm-ask-submit]");
  const answerBox = document.querySelector("[data-hlm-answer]");
  const answerText = document.querySelector("[data-hlm-answer-text]");
  const answerEvidence = document.querySelector("[data-hlm-answer-evidence]");
  const nodeInfo = document.querySelector("[data-hlm-node-info]");
  const nodeFields = document.querySelector("[data-hlm-node-fields]");
  const nodePlot = document.querySelector("[data-hlm-node-plot]");
  const edgeInfo = document.querySelector("[data-hlm-edge-info]");
  const edgeFields = document.querySelector("[data-hlm-edge-fields]");
  const edgePlot = document.querySelector("[data-hlm-edge-plot]");
  const pathInfo = document.querySelector("[data-hlm-path-info]");
  const pathFields = document.querySelector("[data-hlm-path-fields]");
  const pathText = document.querySelector("[data-hlm-path-text]");

  const state = {
    nodes: [],
    edges: [],
    byId: new Map(),
    adj: new Map(),
    width: 0,
    height: 0,
    selected: null,
    selectedEdge: null,
    multiPair: null, // 双节点多选：[id, id]；否则 null
    _path: null, // 双选时的最短路径节点 id 数组（前端 BFS 计算）
    zoom: { k: 1, tx: 0, ty: 0 }, // 视图缩放：k=倍率，tx/ty=平移（屏幕像素）
    running: false,
    settled: false,
  };

  // 拖拽标记：拖拽后抑制随后的 click 选中，避免拖完又触发选中
  let justDragged = false;
  const DRAG_THRESHOLD = 4; // 指针位移超过该像素才视为拖拽（否则是点击）

  const NS = "http://www.w3.org/2000/svg";
  const MAX_TICKS = 800; // 初始布局最大迭代（留足时间让节点铺开）
  // 力导向参数：封顶的斥力 + 限速，保证布局收敛且不震荡（避免闪烁/重叠）
  const REPULSION = 22000; // 斥力系数 (f = REPULSION / d²)
  const MAX_REPULSION_FORCE = 60; // 单对斥力上限（防近距离爆炸）
  const SPRING_K = 1.2; // 弹簧系数
  const IDEAL_EDGE = 130; // 理想边长（更大 = 节点间距更开、布局更通透）
  const GRAVITY = 0.001; // 中心引力（很弱，避免聚成一团）
  const DAMPING = 0.75; // 阻尼（足够收敛，又不至于过冲）
  const MAX_SPEED = 9; // 速度上限（防爆闪）
  const SETTLE_EPSILON = 2; // 能量低于该值视为接近静止
  const SETTLE_FRAMES = 12; // 连续多少帧低能量才判定收敛
  const COOLING_MIN = 0.45; // 冷却下限：随迭代推进力逐渐减弱，避免后期左右震荡
  const MIN_NODE_DIST = 32; // 任意两节点的最小间距（确定性消除拥挤）
  const NEIGHBORHOOD_DEPTH = 3; // 点选节点后保留几跳内的相关节点/边（多跳亮显）
  // 缩放/平移
  const MIN_ZOOM = 0.15;
  const MAX_ZOOM = 5;
  const ZOOM_FACTOR = 1.2; // 每次滚轮/按钮的缩放倍率
  // 流式揭示动画：节点逐个浮现，边随两端节点出现
  const REVEAL_STAGGER = 24; // 节点出现间隔 ms
  const REVEAL_NODE_MS = 260; // 单个节点淡入时长 ms
  const REVEAL_EDGE_MS = 180; // 单条边淡入时长 ms

  // ---------------------------------------------------------------- helpers
  function setStatus(text, ok) {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("ak-hlm-status-ok", Boolean(ok));
    status.classList.toggle("ak-hlm-status-err", ok === false);
  }

  function resize() {
    const rect = canvas ? canvas.getBoundingClientRect() : { width: 0, height: 0 };
    state.width = Math.max(rect.width, 320);
    state.height = Math.max(rect.height, 320);
    if (svg) {
      svg.setAttribute("viewBox", `0 0 ${state.width} ${state.height}`);
      svg.style.width = `${state.width}px`;
      svg.style.height = `${state.height}px`;
    }
  }

  // ------------------------------------------------------------- simulation
  function initPhysics() {
    const n = state.nodes.length;
    // 黄金角螺旋铺点：按画布面积均摊间距，初始即大致分散，避免挤在圆上
    const spacing = Math.max(24, Math.sqrt((state.width * state.height) / Math.max(n, 1)) * 0.55);
    state.nodes.forEach((node, i) => {
      if (node.x === undefined) {
        const angle = i * 2.399963229728653; // 黄金角
        const radius = spacing * Math.sqrt(i + 1) * 1.4;
        node.x = state.width / 2 + Math.cos(angle) * radius;
        node.y = state.height / 2 + Math.sin(angle) * radius;
      }
      node.vx = 0;
      node.vy = 0;
      node.fx = undefined;
      node.fy = undefined;
    });
  }

  function tick(iteration) {
    const nodes = state.nodes;
    // 冷却：迭代越靠后力越弱，让系统平滑静止而非左右震荡
    const cooling = 1 - (1 - COOLING_MIN) * Math.min(1, iteration / MAX_TICKS);
    let energy = 0;

    // 斥力（封顶，防止近距离爆炸导致震荡）
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 4) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          d2 = 4;
        }
        const d = Math.sqrt(d2);
        let f = (REPULSION / d2) * cooling;
        if (f > MAX_REPULSION_FORCE) f = MAX_REPULSION_FORCE;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    // 弹簧（沿边）
    for (const edge of state.edges) {
      const a = state.byId.get(edge.source);
      const b = state.byId.get(edge.target);
      if (!a || !b || a === b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01);
      const f = SPRING_K * (d - IDEAL_EDGE) * cooling;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    // 中心引力 + 阻尼 + 限速 + 积分 + 边界
    for (const node of nodes) {
      node.vx += (state.width / 2 - node.x) * GRAVITY;
      node.vy += (state.height / 2 - node.y) * GRAVITY;
      node.vx *= DAMPING;
      node.vy *= DAMPING;
      const vx = Math.max(-MAX_SPEED, Math.min(MAX_SPEED, node.vx));
      const vy = Math.max(-MAX_SPEED, Math.min(MAX_SPEED, node.vy));
      if (node.fx !== undefined) node.x = node.fx;
      else node.x += vx;
      if (node.fy !== undefined) node.y = node.fy;
      else node.y += vy;
      node.x = Math.max(18, Math.min(state.width - 18, node.x));
      node.y = Math.max(18, Math.min(state.height - 18, node.y));
      energy += vx * vx + vy * vy;
    }
    return energy;
  }

  // ------------------------------------------------------------------ render
  function nodeRadius(node) {
    return node.label === "Character" ? 13 : 9;
  }

  function buildEdgeLabel() {
    // 边过多时不显示关系文字，避免视觉噪音
    return state.edges.length <= 90;
  }

  // 元素缓存：布局只构建一次，之后渲染仅更新位置/样式，避免整图重建导致的闪烁。
  let edgeLayer = null;
  let nodeLayer = null;
  let viewport = null; // 缩放/平移的容器 <g>
  const edgeEls = new Map(); // "s|t|rel" -> {line, label, source, target}
  const nodeEls = new Map(); // id -> <g>

  function clearSvg() {
    if (svg) svg.replaceChildren();
    edgeLayer = null;
    nodeLayer = null;
    viewport = null;
    edgeEls.clear();
    nodeEls.clear();
  }

  function buildGraph() {
    clearSvg();
    const defs = document.createElementNS(NS, "defs");
    defs.appendChild(createArrow());
    svg.appendChild(defs);
    edgeLayer = document.createElementNS(NS, "g");
    nodeLayer = document.createElementNS(NS, "g");
    viewport = document.createElementNS(NS, "g");
    viewport.appendChild(edgeLayer);
    viewport.appendChild(nodeLayer);
    svg.appendChild(viewport);

    // 打乱出现顺序，制造“逐个冒出”的流式感
    const shuffled = state.nodes.slice();
    for (let i = shuffled.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    const delayByNode = new Map();
    shuffled.forEach((n, i) => delayByNode.set(n.id, i * REVEAL_STAGGER));

    const showRel = buildEdgeLabel();
    for (const edge of state.edges) {
      const a = state.byId.get(edge.source);
      const b = state.byId.get(edge.target);
      const line = document.createElementNS(NS, "line");
      line.setAttribute("marker-end", "url(#ak-hlm-arrow)");
      line.classList.add("ak-hlm-edge");
      line.setAttribute("x1", a ? a.x : 0);
      line.setAttribute("y1", a ? a.y : 0);
      line.setAttribute("x2", b ? b.x : 0);
      line.setAttribute("y2", b ? b.y : 0);
      const label = document.createElementNS(NS, "text");
      label.classList.add("ak-hlm-edge-label");
      label.setAttribute("x", 0);
      label.setAttribute("y", 0);
      if (!showRel) label.style.display = "none";
      // 边在两端节点都出现后再淡入（织网感）
      const edgeDelay = Math.max(
        delayByNode.get(edge.source) ?? 0,
        delayByNode.get(edge.target) ?? 0,
      );
      line.style.opacity = "0";
      line.style.animation = `ak-hlm-edge-in ${REVEAL_EDGE_MS}ms ease ${edgeDelay}ms both`;
      const group = document.createElementNS(NS, "g");
      group.classList.add("ak-hlm-edge-group");
      group.appendChild(line);
      group.appendChild(label);
      edgeLayer.appendChild(group);
      const els = { group, line, label, source: edge.source, target: edge.target, relation: edge.relation };
      edgeEls.set(`${edge.source}|${edge.target}|${edge.relation}`, els);
      bindEdgeEvents(els);
    }

    for (const node of state.nodes) {
      const g = document.createElementNS(NS, "g");
      g.setAttribute("data-node-id", node.id);
      g.classList.add("ak-hlm-node");
      g.style.cursor = "pointer";
      const inner = document.createElementNS(NS, "g");
      inner.classList.add("ak-hlm-node-inner");
      const circle = document.createElementNS(NS, "circle");
      circle.setAttribute("r", nodeRadius(node));
      circle.setAttribute("fill", LABEL_COLORS[node.label] || "#868e96");
      inner.appendChild(circle);
      const text = document.createElementNS(NS, "text");
      text.textContent = node.id;
      text.setAttribute("y", nodeRadius(node) + 14);
      text.classList.add("ak-hlm-node-label");
      inner.appendChild(text);
      // 流式揭示：从透明/缩小状态淡入放大
      const delay = delayByNode.get(node.id) ?? 0;
      inner.style.opacity = "0";
      inner.style.animation = `ak-hlm-pop ${REVEAL_NODE_MS}ms ease ${delay}ms both`;
      g.appendChild(inner);
      bindNodeEvents(g, node);
      nodeLayer.appendChild(g);
      nodeEls.set(node.id, g);
    }
  }

  // 视图缩放/平移：把整个图层组套上 transform，节点坐标保持布局坐标不变。
  function applyViewport() {
    if (!viewport) return;
    const z = state.zoom;
    viewport.setAttribute("transform", `translate(${z.tx}, ${z.ty}) scale(${z.k})`);
    const zoomLabel = document.querySelector("[data-hlm-zoom]");
    if (zoomLabel) zoomLabel.textContent = `${Math.round(z.k * 100)}%`;
  }

  // 以屏幕点 (px, py) 为中心缩放（保持光标下的图形位置不动）。
  function zoomAt(px, py, factor) {
    const z = state.zoom;
    const newK = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z.k * factor));
    const ratio = newK / z.k;
    z.tx = px - (px - z.tx) * ratio;
    z.ty = py - (py - z.ty) * ratio;
    z.k = newK;
    applyViewport();
  }

  // 多跳邻域：从 startId 出发，在邻接表上 BFS 到 depth 跳，返回可达节点集合。
  function reachableSet(startId, depth) {
    const seen = new Set([startId]);
    let frontier = [startId];
    for (let hop = 0; hop < depth; hop++) {
      const next = [];
      for (const id of frontier) {
        for (const nb of state.adj.get(id) || []) {
          if (!seen.has(nb)) {
            seen.add(nb);
            next.push(nb);
          }
        }
      }
      frontier = next;
      if (!frontier.length) break;
    }
    return seen;
  }

  function render() {
    if (!state.nodes.length) return;
    if (!nodeLayer) buildGraph();

    // 双选（2 个节点）优先：高亮两点间的最短路径；否则单选邻域；否则全亮
    const multi = state.multiPair && state.multiPair.length === 2 ? state.multiPair : null;
    const selected = multi ? null : state.selected;
    const path = multi ? state._path : null; // 前端 BFS 算出的最短路径（节点 id 数组）

    let nodeBright = null; // (id)=>bool；null = 全亮
    let edgeBright = null; // (s,t)=>bool；null = 全亮
    if (multi) {
      if (path) {
        const pset = new Set(path);
        nodeBright = (id) => pset.has(id);
        edgeBright = (s, t) => isPathEdge(s, t, path);
      } else {
        // 无路径：退化为两节点邻域并集
        const union = new Set([
          ...reachableSet(multi[0], NEIGHBORHOOD_DEPTH),
          ...reachableSet(multi[1], NEIGHBORHOOD_DEPTH),
        ]);
        nodeBright = (id) => union.has(id);
        edgeBright = (s, t) => union.has(s) && union.has(t);
      }
    } else if (selected) {
      const reachable = reachableSet(selected.id, NEIGHBORHOOD_DEPTH);
      nodeBright = (id) => reachable.has(id);
      edgeBright = (s, t) =>
        (reachable.has(s) && reachable.has(t)) || s === selected.id || t === selected.id;
    }

    for (const els of edgeEls.values()) {
      const a = state.byId.get(els.source);
      const b = state.byId.get(els.target);
      if (!a || !b) {
        els.line.style.display = "none";
        continue;
      }
      els.line.style.display = "";
      els.line.setAttribute("x1", a.x);
      els.line.setAttribute("y1", a.y);
      els.line.setAttribute("x2", b.x);
      els.line.setAttribute("y2", b.y);
      const connectedToSelected =
        selected !== null && (els.source === selected.id || els.target === selected.id);
      const inNeighborhood = edgeBright !== null && edgeBright(els.source, els.target);
      const onPath = multi !== null && path !== null && isPathEdge(els.source, els.target, path);
      const dimmed = edgeBright !== null && !inNeighborhood && !connectedToSelected;
      els.line.classList.toggle("ak-hlm-dim", dimmed);
      els.label.classList.toggle("ak-hlm-dim", dimmed);
      els.group.classList.toggle("ak-hlm-edge-connected", connectedToSelected);
      els.group.classList.toggle(
        "ak-hlm-edge-neighborhood",
        inNeighborhood && !connectedToSelected && !onPath,
      );
      els.group.classList.toggle("ak-hlm-edge-path", onPath);
      els.label.setAttribute("x", (a.x + b.x) / 2);
      els.label.setAttribute("y", (a.y + b.y) / 2 - 4);
      els.group.classList.toggle("ak-hlm-edge-selected", state.selectedEdge === els);
    }

    for (const node of state.nodes) {
      const g = nodeEls.get(node.id);
      if (!g) continue;
      g.style.transform = `translate(${node.x}px, ${node.y}px)`;
      const dimmed = nodeBright !== null && !nodeBright(node.id);
      g.classList.toggle("ak-hlm-dim", dimmed);
      const isSelectedNode =
        (selected !== null && node.id === selected.id) ||
        (multi !== null && (node.id === multi[0] || node.id === multi[1]));
      g.classList.toggle("ak-hlm-node-selected", isSelectedNode);
      g.classList.toggle(
        "ak-hlm-node-path",
        multi !== null && path !== null && path.includes(node.id),
      );
    }
    applyViewport();
  }

  // ------------------------------------------------------------ 双节点多选
  // 前端 BFS 求最短路径（无权图，用邻接表）。
  function shortestPathBfs(startId, targetId) {
    if (startId === targetId) return [startId];
    const prev = new Map([[startId, null]]);
    const queue = [startId];
    while (queue.length) {
      const cur = queue.shift();
      for (const nb of state.adj.get(cur) || []) {
        if (prev.has(nb)) continue;
        prev.set(nb, cur);
        if (nb === targetId) {
          const path = [];
          let n = nb;
          while (n !== null) {
            path.unshift(n);
            n = prev.get(n);
          }
          return path;
        }
        queue.push(nb);
      }
    }
    return null;
  }

  // 判断边 (source, target) 是否是路径上相邻两步之间的边。
  function isPathEdge(source, target, path) {
    for (let i = 0; i < path.length - 1; i++) {
      const a = path[i];
      const b = path[i + 1];
      if ((source === a && target === b) || (source === b && target === a)) return true;
    }
    return false;
  }

  function clearMulti() {
    state.multiPair = null;
    state._path = null;
    if (pathInfo) pathInfo.hidden = true;
  }

  // Ctrl/⌘ + 点击：加入/切换双节点多选。
  function toggleMultiSelect(node) {
    if (!state.multiPair || state.multiPair.length >= 2) {
      // 没有多选或已有两个：从当前节点重新开始
      state.multiPair = [node.id];
      state.selected = node;
      state.selectedEdge = null;
      clearMultiPanels();
      render();
      showNodeInfo(node);
      return;
    }
    if (state.multiPair[0] === node.id) {
      // 点回同一个节点：取消多选，恢复单选
      clearMulti();
      state.selected = node;
      render();
      showNodeInfo(node);
      return;
    }
    // 已有一个不同节点：补成两个，展示两者关系
    state.multiPair.push(node.id);
    state.selected = null;
    state.selectedEdge = null;
    if (nodeInfo) nodeInfo.hidden = true;
    if (edgeInfo) edgeInfo.hidden = true;
    render();
    loadPath(state.multiPair[0], state.multiPair[1]);
  }

  function clearMultiPanels() {
    if (nodeInfo) nodeInfo.hidden = false; // 单选时由 showNodeInfo 控制
    if (edgeInfo) edgeInfo.hidden = true;
    if (pathInfo) pathInfo.hidden = true;
  }

  // 拉取两点关系（直接关系 + 最短路径），同时用前端 BFS 高亮路径。
  async function loadPath(source, target) {
    if (!pathInfo) return;
    pathInfo.hidden = false;
    const p = shortestPathBfs(source, target);
    state._path = p;
    render();
    if (pathFields) {
      pathFields.replaceChildren();
      for (const [k, v] of [
        ["起点", source],
        ["终点", target],
      ]) {
        const div = document.createElement("div");
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.textContent = v;
        div.append(dt, dd);
        pathFields.appendChild(div);
      }
    }
    if (pathText) pathText.textContent = "查询中…";
    try {
      const response = await fetch("/api/hongloumeng/path", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
        body: JSON.stringify({ source, target }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "查询失败");
      renderPathPanel(source, target, body, p);
    } catch (error) {
      if (pathText) pathText.textContent = error.message || "查询失败";
    }
  }

  function renderPathPanel(source, target, body, localPath) {
    if (!pathText) return;
    const lines = [];
    const direct = body.direct || [];
    if (direct.length) {
      const rels = direct.map((d) => d.relation).join("、");
      lines.push(`直接关系：${rels}`);
    }
    const paths = body.paths || [];
    if (paths.length) {
      const p0 = paths[0];
      if (p0.rels && p0.rels.length) {
        lines.push("最短路径：" + formatPath(p0));
      } else if (p0.nodes && p0.nodes.length) {
        lines.push("最短路径：" + p0.nodes.join(" → "));
      }
    } else if (localPath && localPath.length > 1) {
      lines.push("本地路径：" + localPath.join(" → "));
    }
    if (!lines.length) lines.push("两节点间暂未找到直接关系或连通路径。");
    pathText.textContent = lines.join("\n");
  }

  function formatPath(pathObj) {
    const nodes = pathObj.nodes || [];
    const rels = pathObj.rels || [];
    let out = "";
    for (let i = 0; i < nodes.length; i++) {
      out += nodes[i];
      if (i < rels.length) out += ` —(${rels[i]})→ `;
    }
    return out;
  }

  // 拖拽时只更新被拖节点及其连边（不重排整图）
  function renderDragged(node) {
    if (!nodeLayer) buildGraph();
    const g = nodeEls.get(node.id);
    if (g) g.style.transform = `translate(${node.x}px, ${node.y}px)`;
    for (const els of edgeEls.values()) {
      if (els.source !== node.id && els.target !== node.id) continue;
      const a = state.byId.get(els.source);
      const b = state.byId.get(els.target);
      if (!a || !b) continue;
      els.line.setAttribute("x1", a.x);
      els.line.setAttribute("y1", a.y);
      els.line.setAttribute("x2", b.x);
      els.line.setAttribute("y2", b.y);
      els.label.setAttribute("x", (a.x + b.x) / 2);
      els.label.setAttribute("y", (a.y + b.y) / 2 - 4);
    }
  }

  function createArrow() {
    const marker = document.createElementNS(NS, "marker");
    marker.setAttribute("id", "ak-hlm-arrow");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", 10);
    marker.setAttribute("refY", 5);
    marker.setAttribute("markerWidth", 6);
    marker.setAttribute("markerHeight", 6);
    marker.setAttribute("orient", "auto-start-reverse");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", "M0,0 L10,5 L0,10 z");
    path.setAttribute("class", "ak-hlm-arrow");
    marker.appendChild(path);
    return marker;
  }

  function bindNodeEvents(g, node) {
    g.addEventListener("click", (event) => {
      event.stopPropagation();
      if (justDragged) {
        justDragged = false;
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        toggleMultiSelect(node);
      } else {
        selectNode(node);
      }
    });
    g.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      startDrag(node, event);
    });
  }

  // ------------------------------------------------------------------ drag
  // 拖拽只移动被拖的节点：松手不重排整图，因此拖拽过程/结束都零抖动。
  function startDrag(node, event) {
    const svgRect = svg.getBoundingClientRect();
    const downX = event.clientX;
    const downY = event.clientY;
    let dragging = false;

    const onMove = (moveEvent) => {
      const dx = moveEvent.clientX - downX;
      const dy = moveEvent.clientY - downY;
      // 位移小于阈值：视为点击，不启动拖拽
      if (!dragging) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
        dragging = true;
        justDragged = true;
      }
      // 屏幕坐标 -> 布局坐标（考虑缩放/平移），否则放大后拖拽会错位
      const z = state.zoom;
      const gx = (moveEvent.clientX - svgRect.left - z.tx) / z.k;
      const gy = (moveEvent.clientY - svgRect.top - z.ty) / z.k;
      node.fx = gx;
      node.fy = gy;
      node.x = gx;
      node.y = gy;
      renderDragged(node);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (dragging) {
        node.fx = undefined;
        node.fy = undefined;
        // 不重新布局整图：节点停留在拖放位置，其余节点与边保持静止
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // ---------------------------------------------------------------- settle
  function nextFrame() {
    return new Promise((resolve) => requestAnimationFrame(resolve));
  }

  // 布局归一化：将已收敛的布局缩放/平移到铺满画布（带边距），消除“中心成团”
  function fitToCanvas() {
    if (state.nodes.length < 2) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of state.nodes) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    }
    const bw = maxX - minX;
    const bh = maxY - minY;
    if (!(bw > 1) || !(bh > 1)) return;
    const pad = 36;
    const scale = Math.min(
      (state.width - pad * 2) / bw,
      (state.height - pad * 2) / bh,
      8, // 放宽上限：节点多时允许把紧团放大铺满画布，避免全部挤在中央
    );
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    for (const n of state.nodes) {
      n.x = state.width / 2 + (n.x - cx) * scale;
      n.y = state.height / 2 + (n.y - cy) * scale;
    }
  }

  // 目标最小间距：按画布面积与节点数自适应（节点越多越密，但至少 MIN_NODE_DIST、最多 64px），
  // 保证再多的节点也能摊开到可读间距而不是挤成一团。
  function minNodeDist() {
    const n = state.nodes.length;
    if (n <= 1) return MIN_NODE_DIST;
    return Math.max(
      MIN_NODE_DIST,
      Math.min(64, Math.sqrt((state.width * state.height) / n) * 1.6),
    );
  }

  // 最小间距分离：把任意两个靠太近的节点对称推开到至少 minNodeDist()，确定性消除拥挤。
  function enforceMinSeparation() {
    const nodes = state.nodes;
    const dist = minNodeDist();
    const maxIter = Math.min(400, Math.max(60, nodes.length * 2));
    for (let iter = 0; iter < maxIter; iter++) {
      let moved = false;
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i];
          const b = nodes[j];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const d = Math.hypot(dx, dy);
          if (d > 0 && d < dist) {
            const push = (dist - d) / 2;
            const ux = dx / d;
            const uy = dy / d;
            a.x -= ux * push;
            a.y -= uy * push;
            b.x += ux * push;
            b.y += uy * push;
            moved = true;
          }
        }
      }
      if (!moved) break;
    }
  }

  // 离屏计算布局：只更新节点坐标，不渲染；算完后一次性渲染 + 淡入。
  // 由于加载/重置期间没有任何可见的连续动画，天然不会抖动。
  async function settle() {
    if (state.running) return;
    state.running = true;
    let ticks = 0;
    let lowEnergyFrames = 0;
    let lastYield = performance.now();
    while (ticks < MAX_TICKS) {
      const energy = tick(ticks);
      ticks += 1;
      lowEnergyFrames = energy < SETTLE_EPSILON ? lowEnergyFrames + 1 : 0;
      if (lowEnergyFrames >= SETTLE_FRAMES) break;
      // 每 ~16ms 让出主线程，避免长任务卡顿
      if (performance.now() - lastYield >= 16) {
        await nextFrame();
        lastYield = performance.now();
      }
    }
    state.running = false;
    state.settled = true;
    fitToCanvas(); // 铺满画布，消除中心成团
    enforceMinSeparation(); // 保证最小间距，确定性消除拥挤
    fitToCanvas(); // 分离可能推出边界，再铺满一次
    render(); // 一次性渲染最终布局（节点以流式动画逐个浮现）
    setStatus(`已加载 ${state.nodes.length} 节点 / ${state.edges.length} 条关系`, true);
  }

  // ------------------------------------------------------------ interactions
  function selectNode(node) {
    state.selected = node;
    state.selectedEdge = null;
    clearMulti();
    if (edgeInfo) edgeInfo.hidden = true;
    render();
    showNodeInfo(node);
  }

  function showNodeInfo(node) {
    if (!nodeInfo || !nodeFields) return;
    const fields = [
      ["名称", node.id],
      ["类型", node.label],
      [node.label === "Character" ? "身份" : "简介", node.info || "—"],
    ];
    nodeFields.replaceChildren();
    for (const [k, v] of fields) {
      const div = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      div.append(dt, dd);
      nodeFields.appendChild(div);
    }
    nodeInfo.hidden = false;
    loadNodeDetail(node);
  }

  function renderPlot(container, chapters) {
    if (!container) return;
    container.replaceChildren();
    const heading = document.createElement("h4");
    heading.textContent = "相关章节情节";
    container.appendChild(heading);
    if (!chapters || !chapters.length) {
      const hint = document.createElement("p");
      hint.className = "ak-hlm-plot-empty";
      hint.textContent = "暂无相关章节情节（可重新 kg-ingest 抽取章节摘要）";
      container.appendChild(hint);
      return;
    }
    for (const ch of chapters.slice(0, 12)) {
      const item = document.createElement("div");
      item.className = "ak-hlm-plot-item";
      const title = document.createElement("strong");
      title.textContent = ch.name || (ch.no ? `第${ch.no}回` : "章节");
      const summary = document.createElement("p");
      summary.textContent = ch.summary || "（无摘要）";
      item.append(title, summary);
      container.appendChild(item);
    }
  }

  async function loadNodeDetail(node) {
    if (!nodePlot) return;
    nodePlot.replaceChildren();
    try {
      const response = await fetch("/api/hongloumeng/node-detail", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
        body: JSON.stringify({ name: node.id }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "查询失败");
      renderPlot(nodePlot, body.chapters);
    } catch (error) {
      renderPlot(nodePlot, null);
    }
  }

  // ------------------------------------------------------------ edge select
  function bindEdgeEvents(els) {
    els.group.addEventListener("click", (event) => {
      event.stopPropagation();
      if (justDragged) {
        justDragged = false;
        return;
      }
      selectEdge(els);
    });
  }

  function selectEdge(els) {
    state.selected = null;
    state.selectedEdge = els;
    clearMulti();
    if (nodeInfo) nodeInfo.hidden = true;
    render();
    showEdgeInfo(els);
  }

  function showEdgeInfo(els) {
    if (!edgeInfo || !edgeFields) return;
    const fields = [
      ["起点", els.source],
      ["关系", els.relation],
      ["终点", els.target],
    ];
    edgeFields.replaceChildren();
    for (const [k, v] of fields) {
      const div = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      div.append(dt, dd);
      edgeFields.appendChild(div);
    }
    edgeInfo.hidden = false;
    loadEdgeDetail(els);
  }

  async function loadEdgeDetail(els) {
    if (!edgePlot) return;
    edgePlot.replaceChildren();
    try {
      const response = await fetch("/api/hongloumeng/edge-detail", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
        body: JSON.stringify({
          source: els.source,
          relation: els.relation,
          target: els.target,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "查询失败");
      renderPlot(edgePlot, body.chapters);
    } catch (error) {
      renderPlot(edgePlot, null);
    }
  }

  function highlightByName(name) {
    const node = state.byId.get(name);
    if (!node) {
      setStatus(`未在图谱中找到「${name}」`, false);
      return;
    }
    state.selected = node;
    state.selectedEdge = null;
    clearMulti();
    if (edgeInfo) edgeInfo.hidden = true;
    // 定位到画布中心前先把视图重置为 1:1，保证被搜节点落在屏幕中心
    state.zoom.k = 1;
    state.zoom.tx = 0;
    state.zoom.ty = 0;
    // 直接定位到画布中心并显示（不重排整图，保持稳定）
    node.x = state.width / 2;
    node.y = state.height / 2;
    render();
    applyViewport();
    showNodeInfo(node);
    setStatus(`已定位「${name}」`, true);
  }

  function resetView() {
    state.selected = null;
    state.selectedEdge = null;
    clearMulti();
    state.zoom.k = 1;
    state.zoom.tx = 0;
    state.zoom.ty = 0;
    state.nodes.forEach((n) => {
      n.x = undefined;
      n.y = undefined;
    });
    if (nodeInfo) nodeInfo.hidden = true;
    if (edgeInfo) edgeInfo.hidden = true;
    if (state.nodes.length) {
      initPhysics();
      settle(); // 离屏重算布局后一次性渲染 + 流式浮现，全程无可见抖动
    }
  }

  async function ask(questionText) {
    if (askSubmit) askSubmit.disabled = true;
    setStatus("问答中…", undefined);
    try {
      const response = await fetch("/api/hongloumeng/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
        body: JSON.stringify({ question: questionText }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "问答失败");
      answerText.textContent = body.answer || "（无回答）";
      answerEvidence.textContent = JSON.stringify(
        {
          entities: body.entities || [],
          strategy: body.strategy || "",
          evidence: (body.evidence || []).slice(0, 30),
        },
        null,
        2,
      );
      answerBox.hidden = false;
      setStatus("已返回回答", true);
    } catch (error) {
      answerText.textContent = error.message || "问答失败";
      answerEvidence.textContent = "";
      answerBox.hidden = false;
      setStatus("问答失败", false);
    } finally {
      if (askSubmit) askSubmit.disabled = false;
    }
  }

  function getCsrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  // ---------------------------------------------------------------- boot
  async function load() {
    resize();
    window.addEventListener("resize", () => {
      resize();
      render();
    });
    try {
      setStatus("加载图谱…", undefined);
      const response = await fetch("/api/hongloumeng/graph?limit=160");
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "加载失败");

      const nodes = (body.nodes || []).filter((n) => n && n.id);
      const edges = (body.edges || []).filter((e) => e && e.source && e.target);

      state.nodes = nodes.map((n) => ({ ...n, x: undefined, y: undefined }));
      state.edges = edges;
      // byId 必须指向 state.nodes 中的同一批对象（力导向只给它们赋 x/y），
      // 否则渲染边时 a.x/b.x 恒为 undefined
      state.byId = new Map(state.nodes.map((n) => [n.id, n]));
      state.adj = new Map();
      for (const edge of edges) {
        if (!state.adj.has(edge.source)) state.adj.set(edge.source, new Set());
        if (!state.adj.has(edge.target)) state.adj.set(edge.target, new Set());
        state.adj.get(edge.source).add(edge.target);
        state.adj.get(edge.target).add(edge.source);
      }

      if (!state.nodes.length) {
        setStatus("图谱为空", false);
        empty.hidden = false;
        return;
      }
      empty.hidden = true;
      initPhysics();
      settle(); // 离屏计算布局 -> 一次性渲染 -> 淡入，全程无可见抖动
    } catch (error) {
      setStatus(error.message || "图谱不可用", false);
      empty.hidden = false;
      empty.textContent = error.message || "图谱不可用";
    }
  }

  // 搜索框回车或失焦即定位
  let searchTimer = null;
  searchInput?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const q = searchInput.value.trim();
      if (q) highlightByName(q);
    }, 300);
  });
  searchInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const q = searchInput.value.trim();
      if (q) highlightByName(q);
    }
  });
  resetBtn?.addEventListener("click", resetView);
  // 缩放按钮：以画布中心为基准放大/缩小，重置回 1:1
  document.querySelector("[data-hlm-zoom-in]")?.addEventListener("click", () =>
    zoomAt(state.width / 2, state.height / 2, ZOOM_FACTOR),
  );
  document.querySelector("[data-hlm-zoom-out]")?.addEventListener("click", () =>
    zoomAt(state.width / 2, state.height / 2, 1 / ZOOM_FACTOR),
  );
  document.querySelector("[data-hlm-zoom-reset]")?.addEventListener("click", () => {
    state.zoom.k = 1;
    state.zoom.tx = 0;
    state.zoom.ty = 0;
    applyViewport();
  });
  askForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const q = question?.value.trim();
    if (q) ask(q);
  });
  // 滚轮缩放：以鼠标位置为中心，preventDefault 避免页面滚动
  svg?.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      const factor = event.deltaY < 0 ? ZOOM_FACTOR : 1 / ZOOM_FACTOR;
      zoomAt(event.clientX - rect.left, event.clientY - rect.top, factor);
    },
    { passive: false },
  );

  // 空白处拖拽平移（按住空白拖动），纯点击则取消选中。
  let panning = null;
  svg?.addEventListener("pointerdown", (event) => {
    panning = {
      startX: event.clientX,
      startY: event.clientY,
      tx0: state.zoom.tx,
      ty0: state.zoom.ty,
      moved: false,
    };
    svg.setPointerCapture?.(event.pointerId);
  });
  svg?.addEventListener("pointermove", (event) => {
    if (!panning) return;
    const dx = event.clientX - panning.startX;
    const dy = event.clientY - panning.startY;
    if (Math.hypot(dx, dy) >= DRAG_THRESHOLD) panning.moved = true;
    if (panning.moved) {
      state.zoom.tx = panning.tx0 + dx;
      state.zoom.ty = panning.ty0 + dy;
      applyViewport();
    }
  });
  const endPan = (event) => {
    if (panning && !panning.moved && event.target === svg) {
      // 纯点击空白：取消选中/多选
      if (state.selected || state.selectedEdge || state.multiPair) {
        state.selected = null;
        state.selectedEdge = null;
        clearMulti();
        if (nodeInfo) nodeInfo.hidden = true;
        if (edgeInfo) edgeInfo.hidden = true;
        render();
      }
    }
    panning = null;
  };
  svg?.addEventListener("pointerup", endPan);
  svg?.addEventListener("pointercancel", () => {
    panning = null;
  });

  load();
})();
