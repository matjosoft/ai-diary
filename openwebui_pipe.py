"""
Open WebUI Pipe Function — AI Diary Assistant

Install this as a Function in Open WebUI:
  Admin Panel → Functions → Add Function → Paste this code

The pipe appears as a model called "Dagbokassistenten" in the model selector.
Configure the diary API URL via Valves in the UI.
"""

import json
from typing import Generator

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        DIARY_API_URL: str = Field(
            default="http://host.docker.internal:8000",
            description="Base URL of the AI Diary FastAPI server (use host.docker.internal to reach the host from inside Docker)",
        )
        REQUEST_TIMEOUT: int = Field(
            default=120,
            description="Timeout in seconds for diary API requests",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.name = "Dagbokassistenten"

    def pipes(self) -> list[dict]:
        return [{"id": "diary-assistant", "name": "Dagbokassistenten 📔"}]

    async def pipe(self, body: dict, __user__: dict = None) -> str | Generator:
        # Extract the latest user message
        messages = body.get("messages", [])
        if not messages:
            return "Ingen fråga mottagen."

        question = messages[-1].get("content", "")
        if not question.strip():
            return "Ställ en fråga om din dagbok!"

        # Build conversation history (exclude the current question)
        # Only pass user/assistant pairs, skip system messages
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in messages[:-1]
            if m["role"] in ("user", "assistant")
        ]

        try:
            response = requests.post(
                f"{self.valves.DIARY_API_URL}/api/chat",
                json={"question": question, "messages": history},
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("answer", "Inget svar från dagboken.")
        except requests.ConnectionError:
            return (
                "⚠️ Kunde inte ansluta till dagboks-API:et. "
                f"Kontrollera att servern körs på `{self.valves.DIARY_API_URL}`."
            )
        except requests.Timeout:
            return "⚠️ Dagboks-API:et svarade inte inom tidsgränsen. Försök igen."
        except requests.HTTPError as e:
            return f"⚠️ Fel från dagboks-API:et: {e.response.status_code} — {e.response.text}"
        except Exception as e:
            return f"⚠️ Oväntat fel: {e}"
