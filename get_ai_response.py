"""Importable helper: send a prompt to Gemma and get the reply back as a string.

Usage from another file in the same folder:

    from gemma_reply import getReply

    answer = getReply("hello who are you")
    print(answer)

Run this file directly to play the excuse game:

    python gemma_reply.py
"""

import os
import random
import re
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


# ---------------------------------------------------------------------------
# Excuse game
# ---------------------------------------------------------------------------

# Every question is phrased so the player has to come up with an excuse.
QUESTIONS = [
    "Your boss walks in and finds you asleep at your desk at 2pm. What's your excuse?",
    "You were supposed to be at your best friend's wedding an hour ago, and you just walked in covered in mud. What's your excuse?",
    "Your teacher asks why your homework is not on their desk for the fifth time this week. What's your excuse?",
    "Your mum finds out the kitchen is on fire and you're holding a fire extinguisher and a pizza box. What's your excuse?",
    "You just replied-all to the entire company with a meme meant for your group chat. What's your excuse?",
    "The police pull you over for driving a lawnmower down the motorway. What's your excuse?",
    "Your landlord asks why there's a goat in your flat. What's your excuse?",
    "You said you'd be home by 10pm. It's now 4am and you're sneaking in through the window. What's your excuse?",
    "Your date asks why you showed up 45 minutes late wearing a pirate costume. What's your excuse?",
    "Your manager asks why you missed the deadline for the project you've had for three months. What's your excuse?",
    "You were caught eating the last slice of your flatmate's birthday cake before the party. What's your excuse?",
    "The gym asks why you've paid for a membership for a year and have visited once. What's your excuse?",
]

JUDGE_INSTRUCTION = (
    "You are a brutally snarky, sarcastic judge in a comedy game where players "
    "make up excuses. You enjoy roasting weak excuses and grudgingly respect "
    "creative ones."
)


def judgeExcuse(question: str, excuse: str) -> tuple[int | None, str]:
    """Rate an excuse out of 10 and return (rating, snarky_reply).

    rating is None if the model's reply could not be parsed.
    """
    prompt = f"""Scenario: "{question}"
Player's excuse: "{excuse}"

Rate this excuse from 1 to 10 for creativity, humour, believability and effort.
Then give a snarky reply of at most two sentences roasting (or begrudgingly praising) it.

Reply in EXACTLY this format and nothing else:
RATING: <number>/10
<your snarky reply>"""

    reply = getReply(prompt, instruction=JUDGE_INSTRUCTION, max_tokens=400)

    match = re.search(r"RATING:\s*(\d{1,2})\s*/\s*10", reply, re.IGNORECASE)
    if not match:
        return None, reply

    rating = max(1, min(10, int(match.group(1))))
    snark = reply[match.end():].strip()
    return rating, snark or reply


def playExcuseGame() -> None:
    """Ask one random question, take one excuse, give one snarky rating."""
    print("🎭 EXCUSE GAME 🎭\n")

    question = random.choice(QUESTIONS)
    print(f"🚨 {question}")
    excuse = input("Your excuse: ").strip()

    if not excuse:
        print("\nSilence. Bold strategy. 0/10.")
        return

    print("Judging...")
    try:
        rating, snark = judgeExcuse(question, excuse)
    except Exception as e:
        print(f"Judge error: {e}")
        return

    if rating is not None:
        print(f"\n⭐ {rating}/10")
    print(f"💬 {snark}")


if __name__ == "__main__":
    playExcuseGame()
