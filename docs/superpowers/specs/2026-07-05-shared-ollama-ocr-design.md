# XHS 与 RAG 共享 Ollama OCR 设计

## 背景

当前 XHS 媒体理解注册表只提供 `none`，RAG OCR 则在启用后默认创建 Tesseract Analyzer。两条链路没有共享同一个 OCR Provider，也无法使用用户本机部署的 Ollama `glm-ocr:latest`。

目标环境提供以下服务：

- Endpoint：`http://localhost:11434/api/generate`
- Model：`glm-ocr:latest`
- Prompt：`Text Recognition:`

Ollama `/api/generate` 使用 Base64 `images` 接收图像；设置 `stream=false` 后返回单个 JSON 对象，OCR 文本位于 `response`。

## 目标

- 建立一个通用 OCR Provider 层，由 XHS 媒体理解与 RAG 文档加载共同使用。
- 支持 `ollama` 与 `none` 两个稳定 Provider ID，并保留后续扩展其他实现的注册边界。
- 提供可在另一台安装了 Ollama/GLM-OCR 的机器上运行的真实图片验证命令。
- 保持 XHS 搜索和 RAG 批量摄取的局部失败隔离，不因单张图片失败丢失全部文本结果。
- 对 URL、图片大小、超时和响应结构设置明确边界。

## 非目标

- 不在当前开发机器上安装或启动 Ollama。
- 不引入 Ollama Python SDK，直接使用项目已有的 `httpx` 依赖调用 REST API。
- 不使用 GLM-OCR 生成的主观分数冒充置信度。
- 不实现自动模型下载、GPU 调度或多实例负载均衡。
- 不把 OCR 输出直接视为已验证事实；现有 Review 仍负责证据审查。

## 总体架构

通用 `OllamaOcrAnalyzer` 接收图片字节，负责：

1. 校验图片大小和 MIME 类型；
2. Base64 编码图片；
3. 调用 `/api/generate`；
4. 校验 HTTP 状态、JSON 结构和非空 `response`；
5. 返回标准 OCR 文本和可追溯 usage。

上层通过两个适配器复用它：

- XHS `ocr` Media Provider：下载 `MediaAsset.source_url`，调用 Analyzer，并转换成 `MediaEvidence`。
- RAG Image Analyzer：把 PDF 页或 Word 内嵌图片的字节直接交给同一 Analyzer。

CLI `agentkit ocr-check IMAGE` 也调用同一 Analyzer，避免验证脚本与生产代码产生两套协议实现。

## 配置模型

新增全局共享配置：

```env
AGENTKIT_OCR_PROVIDER=none
AGENTKIT_OCR_URL=http://localhost:11434/api/generate
AGENTKIT_OCR_MODEL=glm-ocr:latest
AGENTKIT_OCR_TIMEOUT_SECONDS=120
AGENTKIT_OCR_MAX_IMAGE_BYTES=10485760
```

场景开关继续独立存在：

```env
AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=none
AGENTKIT_RAG_OCR_ENABLED=false
```

启用共享 OCR 时使用：

```env
AGENTKIT_OCR_PROVIDER=ollama
AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=ocr
AGENTKIT_RAG_OCR_ENABLED=true
```

配置优先级如下：

1. `AGENTKIT_OCR_PROVIDER=none` 是全局硬关闭；不允许任何 OCR 网络调用或隐式 Tesseract 回退。
2. XHS 只有在 `MEDIA_UNDERSTANDING_PROVIDER=ocr` 且全局 OCR Provider 可用时才执行 OCR。
3. RAG 只有在 `RAG_OCR_ENABLED=true` 且全局 OCR Provider 可用时才执行 OCR。
4. `MEDIA_UNDERSTANDING_PROVIDER=none` 只关闭 XHS 图片理解，不影响已启用的 RAG OCR。
5. `RAG_OCR_ENABLED=false` 只关闭 RAG OCR，不影响已启用的 XHS 图片理解。

`tesseract` 不再作为 `ocr_enabled=true` 时的隐式默认值。若后续保留 Tesseract，应作为显式 Provider ID 注册，避免部署环境行为不确定。

## `none` Provider 语义

当 `AGENTKIT_OCR_PROVIDER=none`：

- 不创建 Ollama HTTP Client；
- 不读取或 Base64 编码图片用于模型请求；
- XHS `ocr` Media Provider 返回 `status=skipped`、`reason=ocr_not_configured`；
- RAG 把 OCR 实际执行开关归一化为关闭，不遍历 PDF 页面或 Word 图片进行 OCR；
- `agentkit ocr-check IMAGE` 输出 `SKIPPED: OCR provider is none`，退出码为 `0`；
- `doctor` 可把显式关闭报告为正常跳过，而不是部署失败。

## Ollama 请求与响应

请求结构：

```json
{
  "model": "glm-ocr:latest",
  "prompt": "Text Recognition:",
  "images": ["<base64>"],
  "stream": false,
  "options": {
    "temperature": 0
  }
}
```

成功响应至少必须满足：

- HTTP 2xx；
- JSON 对象；
- `response` 是非空字符串；
- 若存在 `done`，其值不得为 `false`。

usage 只保留允许字段：`total_duration`、`load_duration`、`prompt_eval_count`、`prompt_eval_duration`、`eval_count`、`eval_duration`。不记录图片 Base64、完整请求体或不可控原始响应。

## Endpoint 安全边界

Ollama 是显式配置的内部基础设施，不能复用默认阻止 loopback/private IP 的普通公网 SSRF 策略。专用 Client 采用以下限制：

- 只允许 `http` 或 `https`；
- URL 必须包含 `/api/generate` 路径；
- 默认只允许 `localhost`、`127.0.0.1` 和 `::1`；
- 若未来使用远程 Ollama，必须通过独立 allow-list 配置显式授权；
- 禁止重定向；
- 设置总超时、响应体大小上限和图片字节上限；
- 错误信息不得包含图片 Base64。

## XHS 适配

默认媒体注册表新增稳定 ID `ocr`。该 Provider：

- 按 `max_images` 接收已经筛选过的 `MediaAsset`；
- 使用项目公网安全下载策略拉取 XHS 图片，并再次校验实际响应字节数；
- 依次调用共享 Analyzer，避免对本机单 GPU Ollama 产生无界并发；
- 任意图片成功时返回 `completed`，包含成功证据和失败资产摘要；
- 所有图片失败时返回 `failed`；
- 没有资产时返回 `skipped`；
- 全局 OCR Provider 为 `none` 时返回 `skipped`，且不下载图片。

GLM-OCR 没有返回可信 confidence 时，`MediaEvidence.confidence` 保持 `None`。Review 不得因为启用 OCR 自动放行内容。

## RAG 适配

`KnowledgeService.ingest_path` 根据全局 OCR Provider 构建 Analyzer，并注入 `DocumentFolderLoader`：

- `ollama`：PDF 扫描页、Word 内嵌图片调用共享 Analyzer；
- `none`：归一化为 `ocr_enabled=false`，不触发 Tesseract；
- 单页或单图片失败：继续沿用 loader 的 warning + continue 行为；
- 原生可提取文本达到阈值的 PDF 页仍不调用 OCR，控制延迟和模型负载。

RAG 和 XHS 共享 Endpoint、Model、Timeout、Prompt 与图片上限配置，不维护第二份 Ollama 参数。

## 验证命令

新增：

```powershell
agentkit ocr-check .\test-image.png
agentkit ocr-check .\test-image.png --json
```

真实验证步骤：

1. 加载全局 OCR 配置；
2. `none` 时明确输出跳过并返回 `0`；
3. 校验文件存在、扩展名/MIME 和大小；
4. 调用生产使用的共享 Analyzer；
5. 输出 provider、model、识别文本、耗时与允许的 usage；
6. 连接失败、模型不存在、响应格式错误或空文本时返回 `1`。

该命令是另一台机器的验收入口，不需要启动 AgentKit Web 服务。

## 测试策略

- 配置测试：默认 `none`、环境变量覆盖、非法 URL/timeout/size 拒绝。
- Analyzer 单元测试：Base64 请求结构、`stream=false`、模型和 prompt、usage 白名单、空响应、非 JSON、超时、HTTP 错误、响应体上限。
- `none` 测试：XHS、RAG、CLI 都不调用 HTTP、下载器或 Tesseract。
- XHS Provider 测试：部分成功、全部失败、无资产、下载超限、证据映射。
- RAG 测试：Ollama Analyzer 注入 PDF/Word 流程；单页失败只产生 warning；原生文本充分时不 OCR。
- CLI 测试：文本与 JSON 输出、跳过退出码、失败退出码。
- 所有网络测试使用注入式 fake transport，不依赖当前机器存在 Ollama。

## 文档

只更新 Git 跟踪且仍有效的文档：

- `.env.example`：共享 OCR 参数与启用示例；
- `docs/ARCHITECTURE.md`：Provider 关系、`none` 硬关闭与失败边界；
- CLI 使用文档：另一台机器执行 `ocr-check` 的步骤。

不修改用户当前未提交的 `docs/DEPLOYMENT.md`。
