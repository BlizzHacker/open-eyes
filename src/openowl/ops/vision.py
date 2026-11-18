"""Sight for text-only models: describe the screen or an image with a vision model.

Many local models cannot read the screenshot images Open Eyes returns. The
``look`` tool sends the screenshot to a configured vision model instead and
returns plain text, so any model can "see".

``[vision]`` supports three API styles:

* ``openai``    -- any OpenAI-compatible /chat/completions (Ollama /v1, LiteLLM,
                   vLLM, LM Studio, OpenRouter ...).
* ``ollama``    -- Ollama's native /api/chat.
* ``anthropic`` -- Anthropic Messages /v1/messages (FCC, the Anthropic API).
"""

from __future__ import annotations

import base64
import mimetypes

import httpx

from openowl.ops import policy
from openowl.ops.config import ConfigError, VisionConfig, get_config

DEFAULT_PROMPT = (
    "Describe this screen for an assistant that cannot see it. List the open windows "
    "and apps, any dialogs or errors, the main readable text, and the clickable controls "
    "with their approximate positions (as x,y pixel coordinates of the image)."
)

# Tests replace this to route requests through httpx.MockTransport.
transport_factory = None


def build_request(cfg: VisionConfig, image_b64: str, mime: str, prompt: str,
                  api_key: str) -> tuple[str, dict, dict]:
    """Return (url, headers, json) for one vision call."""
    headers = {"Content-Type": "application/json"}
    if cfg.api == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": cfg.model, "max_tokens": cfg.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                {"type": "text", "text": prompt},
            ]}],
        }
        return f"{cfg.base_url}/v1/messages", headers, payload
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if cfg.api == "ollama":
        payload = {"model": cfg.model, "stream": False,
                   "messages": [{"role": "user", "content": prompt, "images": [image_b64]}]}
        return f"{cfg.base_url}/api/chat", headers, payload
    if cfg.api == "openai":
        payload = {
            "model": cfg.model, "max_tokens": cfg.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ]}],
        }
        return f"{cfg.base_url}/chat/completions", headers, payload
    raise ConfigError(f"vision.api must be openai, ollama or anthropic, not '{cfg.api}'.")


def parse_reply(api: str, data: dict) -> str:
    if api == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if api == "ollama":
        return data.get("message", {}).get("content", "")
    return data["choices"][0]["message"]["content"]


def ask_vision(image_bytes: bytes, mime: str, prompt: str) -> str:
    cfg = get_config().vision
    if not cfg.configured:
        raise ConfigError(
            "No vision model configured. Add [vision] with api, base_url and model "
            "(e.g. an Ollama qwen2.5vl or a LiteLLM/FCC vision route)."
        )
    api_key = cfg.secret("api_key")
    url, headers, payload = build_request(
        cfg, base64.b64encode(image_bytes).decode("ascii"), mime, prompt, api_key)
    extra = {"transport": transport_factory()} if transport_factory else {}
    with httpx.Client(timeout=float(cfg.raw.get("timeout", 120)), **extra) as client:
        resp = client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"Vision model returned HTTP {resp.status_code}: {resp.text[:300]}")
    return parse_reply(cfg.api, resp.json()).strip()


def do_look(question: str = "", region: dict | None = None, monitor: int = 1) -> str:
    from openowl.tools.screenshot import capture_screenshot

    shot = capture_screenshot(monitor_index=monitor, region=region)
    prompt = DEFAULT_PROMPT if not question else (
        f"{question}\n\nAnswer from what is visible in this screenshot. When you mention a "
        f"control, give its approximate x,y pixel position in the image."
    )
    answer = ask_vision(base64.b64decode(shot["image"]), "image/jpeg", prompt)
    note = ""
    scale = shot.get("dpi_scale") or 1.0
    if shot.get("width"):
        note = (f"\n\n[screenshot {shot['width']}x{shot['height']}"
                + (f", dpi scale {scale}" if scale != 1.0 else "") + f", saved to {shot.get('path')}]")
    return policy.clip(answer + note)


def do_describe_image(path: str, question: str = "") -> str:
    target = policy.check_path(path)
    mime = mimetypes.guess_type(target.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise ValueError(f"{target} does not look like an image ({mime}).")
    return policy.clip(ask_vision(target.read_bytes(), mime,
                                  question or "Describe this image in detail, including any text."))


def register(server, desktop: bool = True) -> None:
    if desktop:
        @server.tool()
        def look(question: str = "", region: dict | None = None, monitor: int = 1) -> str:
            """See the screen through the configured vision model and get a text answer.

            Use this when you cannot view images yourself: it screenshots the desktop
            and returns a description, or the answer to your question about it.

            Parameters:
                question: What you want to know ("Is there an error dialog?", "Where is the Save button?"). Empty = full description.
                region: Optional {"x","y","w","h"} crop in screen pixels.
                monitor: 1 = primary, 2 = second, 0 = all.
            """
            return do_look(question, region, monitor)

    @server.tool()
    def describe_image(path: str, question: str = "") -> str:
        """Describe an image file (or answer a question about it) with the configured vision model.

        Parameters:
            path: Image file path (png, jpg, webp, gif).
            question: Optional question; empty = detailed description.
        """
        return do_describe_image(path, question)
