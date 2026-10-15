"""
Streaming chat against the configured model providers.

Two kinds of provider: "anthropic" goes through the official anthropic SDK, "openai" through the
official openai SDK, which also serves any OpenAI-compatible endpoint such as DeepSeek's.
Each stream yields ("text" | "thinking" | "notice", str) pairs, and last ("usage", dict) with
the token counts in one shape for every provider:
    {"input", "cache_hit", "cache_miss", "cache_write", "output", "reasoning"}
cache_hit is the part of the input served from the provider's prompt cache, billed far lower.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Iterator

import anthropic
import openai

# Models that take the server-side refusal fallback in its "default" form
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5"}
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ProviderError(Exception):
    pass


def image_part(path: Path) -> tuple[str, str]:
    """(media type, base64 data) of an uploaded image"""
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/png")
    return media, base64.standard_b64encode(path.read_bytes()).decode("ascii")


def resolve_key(provider: dict) -> str:
    return provider.get("api_key") or os.environ.get(provider.get("env") or "", "")


def stream_chat(
    provider: dict,
    model: str,
    system: str,
    history: list[dict],
    upload_dir: Path,
    thinking: bool = True,
    effort: str = "high",
) -> Iterator[tuple[str, str]]:
    """
    history: [{"role": "user"|"assistant", "content": str, "images": [file names]}]
    thinking switches DeepSeek's thinking mode; effort ("low" | "high" | "max") sets how hard
    DeepSeek or Claude thinks. Providers without either ignore them.
    """
    if provider["type"] == "anthropic":
        yield from stream_anthropic(provider, model, system, history, upload_dir, effort)
    else:
        yield from stream_openai(provider, model, system, history, upload_dir, thinking, effort)


# Claude

def claude_options(model: str, effort: str = "high") -> dict:
    """
    Thinking and effort settings by model generation: adaptive thinking with a readable summary
    on 4.6 and later (Haiku from 5 on), at the effort chosen on the page.
    """
    m = re.match(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?(?:$|-)", model)
    if not m:
        return {}
    family = m.group(1)
    version = (int(m.group(2)), int(m.group(3) or 0))
    options: dict = {}
    if version >= (4, 6) and not (family == "haiku" and version < (5, 0)):
        options["thinking"] = {"type": "adaptive", "display": "summarized"}
        options["output_config"] = {"effort": effort}
    if model in CLAUDE_FALLBACK_MODELS:
        options["betas"] = [CLAUDE_FALLBACK_BETA]
        options["fallbacks"] = "default"
    return options


def anthropic_messages(history: list[dict], upload_dir: Path) -> list[dict]:
    messages = []
    for msg in history:
        if msg["role"] == "user" and msg.get("images"):
            content: list[dict] = []
            for name in msg["images"]:
                media, data = image_part(upload_dir / name)
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data},
                })
            content.append({"type": "text", "text": msg["content"] or "（见图片）"})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"] or "（空）"})
    return messages


def stream_anthropic(provider, model, system, history, upload_dir, effort="high"):
    key = resolve_key(provider)
    # With no key configured the SDK still resolves ANTHROPIC_API_KEY or an `ant auth login` profile
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    options = claude_options(model, effort)
    kwargs = dict(
        model=model,
        max_tokens=int(provider.get("max_tokens") or 64000),
        # The system prompt (~50k tokens with the skill) is identical every turn: cache it, and
        # with the top-level marker also the conversation so far, up to its last block
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        cache_control={"type": "ephemeral"},
        messages=anthropic_messages(history, upload_dir),
        **{k: v for k, v in options.items() if k != "betas"},
    )
    try:
        if "betas" in options:
            stream_ctx = client.beta.messages.stream(betas=options["betas"], **kwargs)
        else:
            stream_ctx = client.messages.stream(**kwargs)
        with stream_ctx as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield "text", event.delta.text
                    elif event.delta.type == "thinking_delta":
                        yield "thinking", event.delta.thinking
            final = stream.get_final_message()
        u = final.usage
        hit = getattr(u, "cache_read_input_tokens", 0) or 0
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        fresh = u.input_tokens or 0  # Claude's input_tokens excludes cached reads and writes
        yield "usage", {
            "input": hit + write + fresh, "cache_hit": hit, "cache_miss": write + fresh,
            "cache_write": write, "output": u.output_tokens or 0, "reasoning": None,
        }
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            yield "notice", f"模型拒绝了这个请求（类别：{category or '未说明'}）。"
        elif final.stop_reason == "max_tokens":
            yield "notice", "输出达到 max_tokens 上限被截断，可以让它“继续”或调大上限。"
    except anthropic.AuthenticationError:
        raise ProviderError("Claude API Key 无效或未设置，请在设置里填写。")
    except anthropic.PermissionDeniedError as e:
        raise ProviderError(f"Claude 拒绝访问（权限不足）：{e.message}")
    except anthropic.NotFoundError as e:
        raise ProviderError(f"Claude 找不到该模型或接口：{e.message}")
    except anthropic.RateLimitError:
        raise ProviderError("Claude 请求过于频繁（限流），请稍后再试。")
    except anthropic.APIStatusError as e:
        raise ProviderError(f"Claude API 错误 {e.status_code}：{e.message}")
    except anthropic.APIConnectionError:
        raise ProviderError("连接 Claude API 失败，请检查网络或代理。")


# OpenAI and OpenAI-compatible (DeepSeek, ...)

def openai_messages(system: str, history: list[dict], upload_dir: Path) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system}]
    for msg in history:
        if msg["role"] == "user" and msg.get("images"):
            content: list[dict] = [{"type": "text", "text": msg["content"] or "（见图片）"}]
            for name in msg["images"]:
                media, data = image_part(upload_dir / name)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{data}"},
                })
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"] or "（空）"})
    return messages


def openai_usage(usage) -> dict:
    """
    DeepSeek reports prompt_cache_hit_tokens / prompt_cache_miss_tokens, OpenAI only
    prompt_tokens_details.cached_tokens; both put reasoning in completion_tokens_details
    """
    data = usage.model_dump()
    prompt = data.get("prompt_tokens") or 0
    details = data.get("prompt_tokens_details") or {}
    hit = data.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens") or 0
    miss = data.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(prompt - hit, 0)
    reasoning = (data.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return {
        "input": prompt, "cache_hit": hit, "cache_miss": miss, "cache_write": 0,
        "output": data.get("completion_tokens") or 0, "reasoning": reasoning,
    }


def stream_openai(provider, model, system, history, upload_dir, thinking=True, effort="high"):
    key = resolve_key(provider)
    if not key:
        raise ProviderError(f"{provider['name']} 的 API Key 未设置，请在设置里填写。")
    client = openai.OpenAI(api_key=key, base_url=provider.get("base_url") or None)
    kwargs: dict = dict(
        model=model,
        messages=openai_messages(system, history, upload_dir),
        stream=True,
        # the last chunk then carries the usage, with DeepSeek's cache hit and miss counts
        stream_options={"include_usage": True},
    )
    if provider.get("max_tokens"):
        kwargs["max_tokens"] = int(provider["max_tokens"])
    if provider.get("reasoning") == "deepseek":
        # DeepSeek's switch is not an OpenAI parameter, so it goes in the body as is; the
        # thinking it does comes back in delta.reasoning_content and is never sent back
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        if thinking:
            kwargs["reasoning_effort"] = effort
    try:
        stream = client.chat.completions.create(**kwargs)
        finish = None
        usage = None
        try:
            for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                # DeepSeek's reasoner streams its chain of thought separately from the answer
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield "thinking", reasoning
                if delta.content:
                    yield "text", delta.content
                finish = choice.finish_reason or finish
        finally:
            stream.close()
        if usage is not None:
            yield "usage", openai_usage(usage)
        if finish == "length":
            yield "notice", "输出达到 max_tokens 上限被截断，可以让它“继续”或在设置里调大上限。"
    except openai.AuthenticationError:
        raise ProviderError(f"{provider['name']} API Key 无效，请在设置里检查。")
    except openai.NotFoundError as e:
        raise ProviderError(f"{provider['name']} 找不到该模型或接口：{e.message}")
    except openai.RateLimitError as e:
        raise ProviderError(f"{provider['name']} 限流或余额不足：{e.message}")
    except openai.APIStatusError as e:
        raise ProviderError(f"{provider['name']} API 错误 {e.status_code}：{e.message}")
    except openai.APIConnectionError:
        raise ProviderError(f"连接 {provider['name']} API 失败，请检查网络或 Base URL。")
