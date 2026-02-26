from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


class LLMConfigError(ValueError):
    pass


class LLMRequestError(RuntimeError):
    pass


@dataclass
class ProviderSpec:
    name: str
    api_base_url: str
    api_key: str
    models: list[str]
    transformer_use: list[Any]


@dataclass
class ResolvedLLMTarget:
    provider_name: str
    model_name: str
    api_base_url: str
    api_key: str
    transformer_use: list[Any]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LLMConfigError(f"LLM config file not found: {path}") from exc
    except Exception as exc:
        raise LLMConfigError(f"Failed to read LLM config file: {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"Invalid JSON in LLM config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMConfigError("LLM config root must be a JSON object")
    return data


def _parse_provider(entry: dict[str, Any]) -> ProviderSpec:
    if not isinstance(entry, dict):
        raise LLMConfigError("Provider entry must be an object")
    for key in ("name", "api_base_url", "api_key", "models"):
        if key not in entry:
            raise LLMConfigError(f"Provider missing required field: {key}")
    models = entry["models"]
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise LLMConfigError("Provider.models must be a string array")
    transformer = entry.get("transformer") or {}
    transformer_use = transformer.get("use") if isinstance(transformer, dict) else []
    if transformer_use is None:
        transformer_use = []
    if not isinstance(transformer_use, list):
        raise LLMConfigError("Provider.transformer.use must be a list")
    name = str(entry["name"])
    env_key = f"{name.upper()}_API_KEY"
    api_key = os.getenv(env_key) or str(entry.get("api_key", "")).strip()
    if not api_key:
        raise LLMConfigError(f"Provider '{name}' has empty api_key and env override {env_key} is not set")
    return ProviderSpec(
        name=name,
        api_base_url=str(entry["api_base_url"]).strip(),
        api_key=api_key,
        models=[str(m) for m in models],
        transformer_use=transformer_use,
    )


def resolve_llm_target(config_path: str | Path) -> ResolvedLLMTarget:
    path = Path(config_path)
    data = _load_json(path)
    providers_data = data.get("Providers")
    router = data.get("Router")
    if not isinstance(providers_data, list) or not providers_data:
        raise LLMConfigError("Providers must be a non-empty array")
    if not isinstance(router, dict):
        raise LLMConfigError("Router must be an object")
    default_route = router.get("default")
    if not isinstance(default_route, str) or "," not in default_route:
        raise LLMConfigError("Router.default must be a string in format 'provider,model'")
    provider_name, model_name = [x.strip() for x in default_route.split(",", 1)]
    providers = [_parse_provider(p) for p in providers_data]
    provider = next((p for p in providers if p.name == provider_name), None)
    if provider is None:
        raise LLMConfigError(f"Router.default provider '{provider_name}' not found in Providers")
    if model_name not in provider.models:
        raise LLMConfigError(f"Model '{model_name}' not listed in provider '{provider_name}' models")
    if not provider.api_base_url.startswith("http"):
        raise LLMConfigError(f"Provider api_base_url must be http(s): {provider.api_base_url}")
    return ResolvedLLMTarget(
        provider_name=provider.name,
        model_name=model_name,
        api_base_url=provider.api_base_url,
        api_key=provider.api_key,
        transformer_use=provider.transformer_use,
    )


def _apply_transformers(payload: dict[str, Any], transformer_use: list[Any]) -> None:
    for item in transformer_use:
        if not (isinstance(item, list) and len(item) == 2):
            continue
        name, params = item
        if name == "maxtoken":
            if isinstance(params, dict):
                max_tokens = params.get("max_tokens")
                if isinstance(max_tokens, int) and max_tokens > 0 and "max_tokens" not in payload:
                    payload["max_tokens"] = max_tokens
        # Reserved for future transformers.


def build_chat_payload(
    target: ResolvedLLMTarget,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": target.model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    _apply_transformers(payload, target.transformer_use)
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _extract_delta_content(delta: Any) -> str:
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    out.append(item["text"])
            return "".join(out)
    return ""


async def stream_chat_completion(
    target: ResolvedLLMTarget,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    payload = build_chat_payload(
        target,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    headers = {
        "Authorization": f"Bearer {target.api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(
                "POST",
                target.api_base_url,
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    snippet = body.decode("utf-8", errors="replace")[:500]
                    raise LLMRequestError(f"LLM HTTP {resp.status_code}: {snippet}")

                saw_data_line = False
                async for line in resp.aiter_lines():
                    if line is None:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        saw_data_line = True
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            evt = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = evt.get("choices") or []
                        if not choices:
                            continue
                        choice0 = choices[0]
                        if isinstance(choice0, dict):
                            delta = choice0.get("delta")
                            if delta:
                                content = _extract_delta_content(delta)
                                if content:
                                    yield content
                            # Some providers may return non-stream-like chunks.
                            msg = choice0.get("message") if isinstance(choice0.get("message"), dict) else None
                            if msg:
                                content = _extract_delta_content(msg) or (msg.get("content") if isinstance(msg.get("content"), str) else "")
                                if content:
                                    yield content
                    elif not saw_data_line and line.startswith("{"):
                        # Fallback: provider ignored stream=true and returned full JSON.
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if choices and isinstance(choices[0], dict):
                            msg = choices[0].get("message") or {}
                            content = msg.get("content")
                            if isinstance(content, str) and content:
                                yield content
                        break
        except httpx.HTTPError as exc:
            raise LLMRequestError(f"LLM request failed: {exc}") from exc

