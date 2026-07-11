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
            description="Base URL of the AI Diary FastAPI server, as seen from the Open WebUI backend (use host.docker.internal to reach the host from inside Docker)",
        )
        PUBLIC_DIARY_URL: str = Field(
            default="",
            description="Public, browser-reachable base URL of the diary API. Used when building audio links the user's browser opens. Leave empty to fall back to DIARY_API_URL.",
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

    # Words that signal the user actually wants to see the images, not just
    # read about them. Covers Swedish (this is a Swedish diary) and English.
    _IMAGE_KEYWORDS = (
        "bild", "bilder", "bilden", "foto", "fotot", "foton", "fotografi",
        "kort", "korten", "visa", "se ", "titta", "photo", "picture", "image",
        "show me", "let me see",
    )

    @classmethod
    def _wants_images(cls, question: str) -> bool:
        """True if the user explicitly asked to see the actual photos."""
        q = question.lower()
        return any(kw in q for kw in cls._IMAGE_KEYWORDS)

    @staticmethod
    def _append_photos(answer: str, photos: list[dict], include_images: bool) -> str:
        """Append photo info to the answer.

        By default only the descriptions are listed (no image data). When the
        user asked to see the photos, the images are embedded as markdown so
        Open WebUI renders them inline.
        """
        lines = [answer.rstrip(), ""]
        for p in photos:
            date_str = p.get("date", "")
            description = (p.get("description") or "").strip()

            if include_images:
                src = p.get("data_url") or p.get("url") or ""
                if not src:
                    continue
                alt = f"{date_str}: {description}" if description else date_str
                alt = alt.replace("\n", " ").replace("]", "")
                lines.append(f"![{alt}]({src})")
                if description:
                    lines.append(f"*{date_str} — {description}*")
                lines.append("")
            else:
                caption = description or (p.get("caption") or "").strip()
                if not caption and not date_str:
                    continue
                lines.append(f"📷 *{date_str}* — {caption}" if caption else f"📷 *{date_str}*")

        if not include_images and photos:
            lines.append("")
            lines.append("_Fråga efter bilderna om du vill se dem._")

        return "\n".join(lines).rstrip()

    def _append_audio(self, answer: str, audio_url: str | None, label: str | None) -> str:
        """Append a browser-openable link to the rendered audio file.

        Open WebUI sanitises raw <audio> HTML and refuses to navigate to
        data: URLs from links, so we use the diary API's file endpoint and
        let the browser open it (it shows an inline player for MP3).
        """
        if not audio_url:
            return answer
        public_base = (
            self.valves.PUBLIC_DIARY_URL.strip() or self.valves.DIARY_API_URL
        ).rstrip("/")
        full = f"{public_base}{audio_url}"
        title = f"Dagboksradion — {label}" if label else "Dagboksradion"
        return "\n".join([
            answer.rstrip(),
            "",
            f"🎙️ **{title}**",
            f"[▶ Lyssna]({full})",
        ]).rstrip()

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
                json={"question": question, "messages": history, "client_type": "web"},
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            answer = data.get("answer", "Inget svar från dagboken.")
            photos = data.get("photos") or []
            if photos:
                include_images = self._wants_images(question)
                answer = self._append_photos(answer, photos, include_images)
            audio_url = data.get("audio_url")
            if audio_url:
                answer = self._append_audio(answer, audio_url, data.get("audio_label"))
            return answer
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
