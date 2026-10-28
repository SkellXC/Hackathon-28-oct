"""Importable helper: send a prompt to Gemma and get the reply back as a string.

Usage from another file in the same folder:

    from gemma_reply import getReply

    answer = getReply("hello who are you")
    print(answer)
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load .env if present in the same folder
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path, override=True, encoding="utf-8-sig")

# Replace "YOUR_API_KEY_HERE" with your actual API key string
HARDCODED_API_KEY = "AQ.Ab8RN6KczsHOR6KNzLAsJmb-FlVnsTz2hNxWq8u1rHgWcJE-Yg"

# Uses .env variable first; falls back to the hardcoded string if .env is missing
API_KEY = os.environ.get("GEMINI_API_KEY") or HARDCODED_API_KEY

if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
    raise ValueError("API key is missing. Set GEMINI_API_KEY in .env or update HARDCODED_API_KEY.")

MODEL = "gemma-4-26b-a4b-it"  # swap for a smaller model name from list_models.py
MAX_TOKENS = 300  # ~2 sentences; raise if replies get cut off
INSTRUCTION = "Reply in at most two short sentences."

_client = None  # created on first use, then reused so later calls are faster


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=API_KEY)
    return _client


def getReply(
    prompt: str,
    instruction: str = INSTRUCTION,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
    temperature: float | None = None,
) -> str:
    """Send `prompt` to Gemma and return the reply text."""
    # Prepend instruction since some Gemma models reject system instructions
    full_prompt = f"{instruction}\n\n{prompt}" if instruction else prompt
    response = _get_client().models.generate_content(
        model=model,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return (response.text or "").strip()


get_reply = getReply  # snake_case alias


if __name__ == "__main__":
    print(getReply("hello who are you"))
