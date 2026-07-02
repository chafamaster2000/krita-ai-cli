#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash) for the kri prompt workflow.

Fires on every Bash call. Exits silently (no output at all) unless the
command touches the AI prompt: `kri ai set-prompt` / `kri ai generate`, or
`ai_set_prompt` / `ai_generate` inside a `kri batch` heredoc. When it
matches, it reads the active style's resolved architecture from the plugin
(action ai_status on localhost:5678) and injects a reminder naming the model
family + prompt convention.

Reminder-only: never blocks; any failure degrades to a generic reminder."""
import sys
import os
import json
import urllib.request

NAT = "lenguaje natural (frases descriptivas)"
TAG = "tags danbooru (palabras separadas por comas)"

# Arch.name -> (familia, convención del positivo, negativo)
ARCH = {
    "flux": ("Flux", NAT, "vacío (se ignora)"),
    "flux_k": ("Flux Kontext", NAT, "vacío (se ignora)"),
    "flux2_4b": ("Flux.2", NAT, "vacío (se ignora)"),
    "flux2_9b": ("Flux.2", NAT, "vacío (se ignora)"),
    "chroma": ("Chroma", NAT, "vacío (se ignora)"),
    "zimage": ("Z-Image", NAT, "vacío (Turbo, CFG≈1)"),
    "qwen": ("Qwen-Image", NAT, "opcional, poco peso"),
    "qwen_e": ("Qwen Edit", NAT, "opcional"),
    "qwen_e_p": ("Qwen Edit Plus", NAT, "opcional"),
    "qwen_l": ("Qwen Layered", NAT, "opcional"),
    "sd3": ("SD3 / 3.5", NAT, "soportado, suave"),
    "ernie": ("ERNIE Image", NAT, "opcional"),
    "illu": ("Illustrious", TAG, "worst quality, low quality, ..."),
    "illu_v": ("Illustrious (v-pred)", TAG, "worst quality, low quality, ..."),
    "anima": ("Anima (anime)", TAG, "worst quality, low quality, ..."),
}


def fetch_status():
    token = os.environ.get("KRITAMCP_TOKEN", "")
    # 127.0.0.1 literal: en Windows "localhost" resuelve IPv6 primero y el
    # fallback a IPv4 mete ~2s (mismo fix que cli/kri).
    url = os.environ.get("KRITA_URL", "http://127.0.0.1:5678")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Kritamcp-Token"] = token
    body = json.dumps({"action": "ai_status", "params": {}}).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode())


def model_line():
    """Best-effort one-liner about the active model. Empty string on any failure."""
    try:
        st = fetch_status()
    except Exception:
        return ""
    m = st.get("model") or {}
    arch = m.get("architecture")
    ck = m.get("checkpoint")
    info = ARCH.get(arch)
    if info:
        fam, conv, neg = info
        return (f"Modelo activo: architecture='{arch}' ({fam}). "
                f"Convención del positivo: {conv}. Negativo: {neg}. ")
    if arch in ("sdxl", "sd15"):
        return (
            f"Modelo activo: architecture='{arch}' (AMBIGUO: SDXL/SD1.5 corre tanto "
            f"booru —Pony/NoobAI/Animagine— como realista de lenguaje natural). "
            f"Mirá checkpoint='{ck}' y el nombre del estilo para decidir la convención. "
        )
    if arch:
        return f"Modelo activo: architecture='{arch}'. "
    return ""


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    command = (data.get("tool_input") or {}).get("command", "")

    # Fast path: not a kri prompt command → no output, no HTTP call.
    if "kri" not in command:
        return
    has_set_prompt = "ai set-prompt" in command or "ai_set_prompt" in command
    has_generate = "ai generate" in command or "ai_generate" in command
    if not (has_set_prompt or has_generate):
        return

    parts = []
    if has_set_prompt:
        parts.append(
            "Antes de escribir este prompt usá: kri-prompt-format (formatear "
            "según el modelo activo), e kri-prompt-unknown-entities (googlear "
            "nombres propios no famosos antes de describirlos)."
        )
    if has_generate:
        parts.append(
            "Antes de generar: corré kri-prompt-sanity-check sobre el prompt final "
            "(coherencia con el modelo activo y con lo que pidió el usuario; restos de "
            "ediciones, contradicciones, score tags si la familia los pide, negativo, "
            "nombres propios resueltos)."
        )
    base = " ".join(parts)

    line = model_line()
    msg = (line + base) if line else (
        base + " (No pude leer ai_status; verificá el modelo activo vos.)")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": msg,
        }
    }))


if __name__ == "__main__":
    main()
