import json
import random
import os
import sys
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment variables from .env file
load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("❌ Error: GEMINI_API_KEY environment variable not found.")
    print("Please export GEMINI_API_KEY in your terminal or create a .env file.")
    sys.exit(1)

# Pass the API key directly to the client
client = genai.Client(api_key=api_key)

def evaluate_excuse(statement, excuse):
    """Hits Gemini using the official google-genai SDK to score a player's excuse."""
    
    prompt = f"""
    You are the judge of a comedy game. 
    Scenario: "{statement}"
    Excuse provided: "{excuse}"
    
    Score this excuse from 1 to 100 based on creativity, humor, logic, and absurdity.
    You must respond ONLY with a valid JSON object in this exact format:
    {{"score": 85, "reasoning": "A short funny sentence explaining the score."}}
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7,
            ),
        )
        
        result = json.loads(response.text)
        return result["score"], result["reasoning"]
        
    except Exception as e:
        return 0, f"Judge error: {e}"

def main():
    try:
        with open("statements.json", "r") as f:
            statements = json.load(f)
    except FileNotFoundError:
        statements = [
            "You are 3 hours late to your own wedding.",
            "You accidentally deleted the production database.",
            "Your dog is driving your car on the highway."
        ]
        
    print("🎭 Welcome to Excuse Showdown! 🎭\n")
    players_count = int(input("How many players? "))
    players = [input(f"Name of Player {i+1}: ") for i in range(players_count)]
    
    round_statement = random.choice(statements)
    print(f"\n🚨 SCENARIO: {round_statement}")
    
    results = []
    for player in players:
        print(f"\n👉 {player}'s turn:")
        excuse = input("Your excuse: ")
        
        print(f"Judging {player}'s excuse...")
        score, reasoning = evaluate_excuse(round_statement, excuse)
        
        results.append({
            "player": player, 
            "excuse": excuse, 
            "score": score, 
            "reasoning": reasoning
        })
        
    print("\n" + "="*40)
    print("🏆 FINAL RANKINGS 🏆")
    print("="*40)
    
    results.sort(key=lambda x: x["score"], reverse=True)
    
    for rank, res in enumerate(results, 1):
        print(f"\n#{rank} {res['player']} - {res['score']}/100")
        print(f"Excuse: \"{res['excuse']}\"")
        print(f"Judge Gemma says: \"{res['reasoning']}\"")

if __name__ == "__main__":
    main()