"""
Farmamed Recept Agent — FastAPI backend
Klanten uploaden hun recept, de agent leidt hen door het bestelproces.
"""

from __future__ import annotations
import os
import json
import base64
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import requests as http_requests

load_dotenv()

app = FastAPI(title="Farmamed Recept Agent")

# CORS — sta farmamed.nl toe
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In productie beperken tot farmamed.nl
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"


# ------------------------------------------------------------------
# Pagina's
# ------------------------------------------------------------------

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ------------------------------------------------------------------
# API: recept uploaden + analyseren
# ------------------------------------------------------------------

@app.post("/api/analyseer-recept")
async def analyseer_recept(bestand: UploadFile = File(...)):
    """
    Ontvang een recept (PDF of afbeelding), lees het uit met OCR
    en geef gestructureerde data terug.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API-sleutel niet geconfigureerd")

    # Bestand inlezen
    inhoud = await bestand.read()
    bestandsnaam = bestand.filename or "recept"

    # Bepaal bestandstype
    if bestandsnaam.lower().endswith(".pdf"):
        media_type = "application/pdf"
    elif bestandsnaam.lower().endswith(".png"):
        media_type = "image/png"
    elif bestandsnaam.lower().endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    else:
        media_type = "application/octet-stream"

    # Stuur naar Claude Vision API
    recept_data = await _analyseer_met_claude(inhoud, media_type)
    return JSONResponse(content=recept_data)


@app.post("/api/chat")
async def chat(request: Request):
    """
    Chat endpoint: de agent beantwoordt vragen en leidt de klant
    door het bestelproces op basis van de receptdata.
    """
    body = await request.json()
    berichten = body.get("berichten", [])
    recept_context = body.get("recept_context", {})

    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="API-sleutel niet geconfigureerd")

    antwoord = await _chat_met_claude(berichten, recept_context)
    return JSONResponse(content={"antwoord": antwoord})


# ------------------------------------------------------------------
# Claude API calls
# ------------------------------------------------------------------

async def _analyseer_met_claude(bestand_bytes: bytes, media_type: str) -> dict:
    """Stuur het recept naar Claude Vision en ontvang gestructureerde JSON."""

    b64 = base64.standard_b64encode(bestand_bytes).decode("utf-8")

    # PDF of afbeelding
    if media_type == "application/pdf":
        document_blok = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64}
        }
    else:
        document_blok = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
        }

    prompt = """Analyseer dit recept en extraheer de volgende velden als JSON.
Geef ALLEEN JSON terug, geen uitleg of markdown.

{
  "recept_datum": "DD-MM-YYYY of null",
  "medicijn": "volledige naam inclusief concentratie",
  "concentratie": "bijv. 0.02% of 5%",
  "hoeveelheid": "bijv. 30 gram",
  "iter": "aantal herhalingen of null",
  "gebruiksaanwijzing": "volledige instructie",
  "patient_naam": "voor- en achternaam",
  "geboortedatum": "DD-MM-YYYY of null",
  "bsn": "9-cijferig nummer of null",
  "email": "emailadres of null",
  "telefoon": "telefoonnummer of null",
  "voorschrijver": "naam arts",
  "agb_code": "AGB-code of null",
  "big_nummer": "BIG-nummer of null",
  "geldig": true of false (ouder dan 1 jaar = false),
  "vertrouwen": 0-100 (hoe zeker ben je van de extractie)
}"""

    response = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "user",
                    "content": [document_blok, {"type": "text", "text": prompt}]
                }
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    tekst = response.json()["content"][0]["text"].strip()
    tekst = tekst.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(tekst)
    except json.JSONDecodeError:
        return {"fout": "Kon recept niet verwerken", "ruw": tekst}


async def _chat_met_claude(berichten: list, recept_context: dict) -> str:
    """Agent-chat: begeleid de klant door het bestelproces."""

    systeem = f"""Je bent een vriendelijke apotheekassistent van Farmamed Bereidingsapotheek.
Je helpt klanten hun recept te verwerken en een bestelling te plaatsen.

Receptgegevens die al zijn uitgelezen:
{json.dumps(recept_context, ensure_ascii=False, indent=2)}

Richtlijnen:
- Spreek de klant aan bij naam als je die weet
- Bevestig welk medicijn je hebt gevonden op het recept
- Controleer of het recept geldig is (niet ouder dan 1 jaar)
- Vraag om bevestiging van de gegevens
- Leg uit wat de volgende stap is (bestelling afronden op farmamed.nl)
- Wees warm, professioneel en duidelijk
- Antwoord altijd in het Nederlands
- Houd antwoorden kort (max 3-4 zinnen)
- Als het recept verlopen is, leg dit vriendelijk uit en verwijs naar de huisarts"""

    response = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 500,
            "system": systeem,
            "messages": berichten,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]
