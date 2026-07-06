# Operations Unicode 与 Network 关系流动动画实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让运行追踪 JSON 直接显示完整中文，并让当前选中节点的关联关系以清晰但不误导的流动光点呈现。

**Architecture:** Unicode 修复放在 Flask/Jinja 展示边界，通过不标记为 Safe 的 `json.dumps(..., ensure_ascii=False)` 结果继续接受 Jinja HTML 自动转义。Network 保留静态基础边，额外为选中节点的直接关系渲染流动覆盖层；真实 `active=true` 边使用独立更强状态，图例解释两者差异。

**Tech Stack:** Python、Flask、Jinja2、原生 JavaScript、SVG、CSS、pytest。

---

### Task 1: Unicode 安全 JSON 展示

**Files:**
- Modify: `tests/integration/test_web_auth.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/web/templates/operations.html`

- [ ] **Step 1: 写失败测试**

在 Operations 测试中写入包含 `你好` 和 `</script>` 的事件载荷，断言响应包含中文，不包含 `\\u4f60\\u597d`，且原始 Script 标签不会出现在 HTML。

```python
assert '"text": "你好"' in html
assert "\\u4f60\\u597d" not in html
assert "<script>alert(1)</script>" not in html
```

- [ ] **Step 2: 验证测试失败**

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests\integration\test_web_auth.py -k "operations" -q
```

预期：中文断言失败，因为当前 `tojson` 输出 Unicode 转义。

- [ ] **Step 3: 实现安全 Unicode Filter**

在 `app.py` 注册 `tojson_unicode` Filter，使用 `json.dumps(value, ensure_ascii=False, indent=indent, default=str)` 返回普通字符串，不使用 `Markup`。模板改为 `event.payload | tojson_unicode(indent=2)`，由 Jinja 自动转义 HTML 特殊字符。

```python
@app.template_filter("tojson_unicode")
def tojson_unicode(value: Any, indent: int = 2) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)
```

- [ ] **Step 4: 运行 Operations 与 XSS 回归测试**

```powershell
.venv\Scripts\python.exe -m pytest tests\integration\test_web_auth.py -k "operations" -q
```

预期：中文可见且 XSS 断言通过。

### Task 2: 选中关系流动光点

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/templates/agents.html`
- Modify: `src/agentkit/web/static/js/agent_graph.js`
- Modify: `src/agentkit/web/static/css/pages.css`

- [ ] **Step 1: 写失败测试**

断言模板包含 `data-network-legend`，脚本为每条边创建 `ak-network-current` 覆盖路径，选中节点时只为直接关联覆盖路径添加 `is-selected-relation`，并继续保留 `relationship.active === true` 的真实活动判断。

```python
assert "data-network-legend" in html
assert "ak-network-current" in js
assert "is-selected-relation" in js
assert "relationship.active === true" in js
```

- [ ] **Step 2: 验证测试失败**

```powershell
.venv\Scripts\python.exe -m pytest tests\integration\test_web_ui_redesign.py -k "network_relation_flow" -q
```

预期：图例或覆盖路径契约缺失。

- [ ] **Step 3: 实现双层边语义**

基础路径保持静态。在同一路径上增加 `ak-network-current` 覆盖层；选中节点时对直接关系添加 `is-selected-relation`，真实活动关系添加 `is-active-run`。CSS 使用圆头短虚线和 `stroke-dashoffset` 形成流动光点，真实活动速度快于选中关系。

```javascript
const current = path.cloneNode(false);
current.classList.add("ak-network-current");
current.classList.toggle("is-active-run", relationship.active === true);
edgeLayer.append(path, current);
```

- [ ] **Step 4: 增加图例与 Reduced Motion**

图例文案固定为“流动光点：当前选中关系；快速流动：实时运行”。`prefers-reduced-motion` 下关闭动画，保留静态线宽差异。

- [ ] **Step 5: 运行 Network 回归测试**

```powershell
.venv\Scripts\python.exe -m pytest tests\integration\test_web_ui_redesign.py tests\integration\test_web_auth.py -k "agent_network or registry" -q
```

预期：全部通过。

### Task 3: 提交修复

- [ ] **Step 1: 检查差异与测试**

```powershell
node --check src/agentkit/web/static/js/agent_graph.js
git diff --check
```

- [ ] **Step 2: 提交**

```powershell
git add docs/superpowers/specs/2026-07-04-agentkit-ui-taste-redesign-design.md `
  docs/superpowers/plans/2026-07-04-operations-unicode-network-flow.md `
  tests/integration/test_web_auth.py tests/integration/test_web_ui_redesign.py `
  src/agentkit/web/app.py src/agentkit/web/templates/operations.html `
  src/agentkit/web/templates/agents.html src/agentkit/web/static/js/agent_graph.js `
  src/agentkit/web/static/css/pages.css
git commit -m "fix: clarify audit Unicode and network relationship flow"
```
