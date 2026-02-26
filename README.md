# QEMU Device Model Generator MVP (Web)

本项目实现一个本地 Web 工具，用于“AI 生成 QEMU 外设仿真 C 代码（MVP）”。

核心流程（分阶段）：

1. 上传驱动源码 zip + 参考 QEMU 仿真源码 zip
2. Stage 1 Ingest：扫描文件树、按文件/函数/宏切块、建立轻量索引
3. Stage 2 Analyse：提取寄存器访问/轮询/IRQ/DMA/参考 QEMU 建模线索，落盘 `analysis.json`
4. Stage 3 Questions：生成澄清问题清单（偏寄存器副作用语义），落盘 `questions.json`
5. 填写并保存答案 `answers.json`
6. Stage 4 Generate：调用 OpenAI-compatible Chat Completions API（支持 streaming），生成单个 `<device_name>.c`

输出产物（全部落盘到 project 目录）：

- `artifacts/analysis.json`
- `artifacts/questions.json`
- `artifacts/answers.json`
- `artifacts/<device_name>.c`
- `artifacts/report.md`（可选）

## 技术栈

- 后端：Python + FastAPI
- 前端：本地静态 HTML/CSS/JS（无构建步骤）
- 存储：本地文件系统 + SQLite
- LLM：OpenAI-compatible Chat Completions API（支持流式输出）

## 运行前置条件

- Python `3.10+`（推荐 `3.10` 或 `3.11`）
- 注意：若系统默认 `python` 是旧版本（如 3.6），请使用 `py -3.10` / `py -3.11`

## 目录结构

```text
.
├─ app/
│  ├─ __init__.py
│  ├─ analyse.py
│  ├─ api.py
│  ├─ db.py
│  ├─ generator.py
│  ├─ ingest.py
│  ├─ llm_client.py
│  ├─ main.py
│  ├─ models.py
│  ├─ questions.py
│  ├─ retrieval.py
│  ├─ settings.py
│  └─ utils.py
├─ config/
│  └─ llm.json              # 示例配置（请自行填写 API key 或用环境变量覆盖）
├─ data/
│  └─ .gitkeep              # 运行期数据目录（SQLite、projects、产物）
├─ examples/
│  └─ sample_project/
│     └─ README.md
├─ web/
│  ├─ app.js
│  ├─ index.html
│  └─ styles.css
├─ .gitignore
├─ requirements.txt
└─ README.md
```

## 安装与启动

### 1) 安装依赖

```bash
py -3.10 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2) 准备 LLM 配置 `config/llm.json`

项目已包含示例文件，字段结构兼容以下格式（字段名保持一致）：

```json
{
  "Providers": [
    {
      "name": "dashscope",
      "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
      "api_key": "xxxxxxx",
      "models": ["qwen3-max"],
      "transformer": {
        "use": [["maxtoken", { "max_tokens": 4096 }]]
      }
    }
  ],
  "Router": {
    "default": "dashscope,qwen3-max",
    "HOST": "127.0.0.1",
    "LOG": false
  }
}
```

注意：

- 不要提交真实 API key 到仓库。
- `transformer.use` 当前仅实现 `maxtoken -> max_tokens` 转换。

### 3) 环境变量覆盖 API Key（优先级更高）

若 provider 名称为 `dashscope`，可通过环境变量覆盖：

```bash
set DASHSCOPE_API_KEY=your_real_key
```

覆盖规则：

- 优先使用 `DASHSCOPE_API_KEY`
- 若未设置，则回退到 `config/llm.json` 中的 `api_key`

### 4) 启动服务

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

打开：

- Web UI: `http://127.0.0.1:8000/ui/`
- API docs: `http://127.0.0.1:8000/docs`

## Web 使用流程（推荐）

1. 创建 project（输入 `device_name` 和 `device_type`）
2. 上传两个 zip：
   - 驱动代码 zip（driver）
   - 类似平台的 QEMU 仿真代码 zip（reference）
3. 点击 `运行 Analyse`
   - 自动生成 `analysis.json` 与 `questions.json`
4. 在页面填写问题答案并保存
   - 生成 `answers.json`
5. 点击 `开始生成`
   - 实时流式显示模型输出
   - 生成 `<device_name>.c`
   - 可选生成 `report.md`
6. 在 `Artifacts` 区域下载查看结果

## API（MVP）

已实现以下接口：

- `POST /projects`
- `GET /projects`
- `GET /projects/{id}`
- `POST /projects/{id}/upload`
- `POST /projects/{id}/analyse`
- `GET  /projects/{id}/questions`
- `POST /projects/{id}/answers`
- `POST /projects/{id}/generate` (stream, NDJSON)
- `GET  /projects/{id}/artifact/{name}`

### `POST /projects`

请求体：

```json
{
  "device_name": "foo_uart",
  "device_type": "uart"
}
```

### `POST /projects/{id}/upload`

`multipart/form-data`：

- `driver_archive`: `driver.zip`
- `reference_archive`: `reference.zip`
- `note` (optional)

当前 MVP 仅支持 `.zip`。

### `POST /projects/{id}/generate`（流式）

请求体示例：

```json
{
  "llm_config_path": "config/llm.json",
  "top_k": 12,
  "generate_report": true,
  "temperature": 0.1,
  "max_tokens": 4096
}
```

返回类型：`application/x-ndjson`

事件示例（每行一条 JSON）：

```json
{"type":"status","message":"generation_started","provider":"dashscope","model":"qwen3-max"}
{"type":"token","text":"#include \"qemu/osdep.h\"\n"}
{"type":"token","text":"..."}
{"type":"done","artifact":"foo_uart.c","report":"report.md"}
```

错误事件示例：

```json
{"type":"error","message":"Model returned a patch/diff instead of a C file"}
```

## LLM 调用实现说明（满足强制要求）

已实现：

- 从 JSON 配置读取 `Providers` / `Router`
- 根据 `Router.default` 解析 provider + model
- 调用对应 `api_base_url`
- `DASHSCOPE_API_KEY` 环境变量覆盖 `api_key`
- `transformer.use` 中 `maxtoken/max_tokens` 转换
- 流式输出（后端 NDJSON 流，前端实时渲染）

## 生成约束（Prompt 已强制）

生成 C 代码时会强制模型：

- 只输出一个完整 C 文件内容
- 不输出 patch / markdown / 解释文字
- 包含 QOM 类型、State、MMIO read/write、reset、最简 VMState、日志
- 用注释标记：
  - 哪些寄存器语义来自驱动证据
  - 哪些行为是推断/占位（`TODO`）

## 错误处理（MVP）

已覆盖常见错误：

- 缺少上传文件（driver/reference）
- `analysis.json` / `answers.json` 缺失时直接生成
- 非法/缺失 LLM 配置 JSON
- `Router.default` 格式错误或 provider/model 不匹配
- zip 非法路径（防路径穿越）
- 模型返回 patch / markdown / 非 C 内容
- LLM HTTP 错误或流中断（会通过 stream `error` 事件返回）

## 示例 Project 占位

参考 `examples/sample_project/README.md`。该目录不包含任何真实源码，仅说明输入组织方式。

## 备注（MVP 边界）

- 目前上传仅支持 `.zip`
- 分析和问题生成为规则/正则驱动的轻量实现（便于离线快速运行）
- QEMU 代码生成依赖你提供的澄清答案质量；未确定语义会在代码中用 `TODO` 标注
