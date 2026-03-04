# Using Local Ollama Models

This project supports two provider protocols:

- `openai` (OpenAI-compatible Chat Completions stream/SSE)
- `ollama` (native Ollama `/api/chat` stream)

## 1) Start Ollama

```powershell
ollama serve
```

## 2) Pull a local model

```powershell
ollama pull qwen2.5-coder:14b
```

## 3) Use Ollama config in this project

Use `config/llm.ollama.json`:

```json
{
  "Providers": [
    {
      "name": "ollama",
      "protocol": "ollama",
      "api_base_url": "http://127.0.0.1:11434/api/chat",
      "api_key": "",
      "models": ["qwen2.5-coder:14b"],
      "transformer": {
        "use": [["maxtoken", { "max_tokens": 2048 }]]
      }
    }
  ],
  "Router": {
    "default": "ollama,qwen2.5-coder:14b",
    "HOST": "127.0.0.1",
    "LOG": false
  }
}
```

## 4) Switch in Web UI

Set `LLM 配置路径` to:

```text
config/llm.ollama.json
```

Then run `Analyse` and `Generate` normally.

## Notes

- `api_key` for Ollama can be empty.
- If you run Ollama behind a gateway that requires auth, set `api_key` or env var `OLLAMA_API_KEY`.
- `transformer.use` with `maxtoken.max_tokens` is mapped to Ollama `options.num_predict`.

