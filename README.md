# Farmamed Recept Agent — Web

FastAPI chatbot die klanten begeleidt bij het indienen van hun recept en woocommere orders laat uitlezen.

## Lokaal testen

```bash
pip install -r requirements.txt
# Maak .env aan met ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload
# Open http://localhost:8000
```

## Deployen op Railway

1. Maak een GitHub repository aan (bijv. `farmamed-web`)
2. Push deze bestanden naar die repository
3. Ga naar railway.app → New Project → Deploy from GitHub repo
4. Selecteer de repository
5. Voeg environment variable toe: `ANTHROPIC_API_KEY` = jouw sleutel
6. Railway deployt automatisch — je krijgt een publieke URL

## Embedden in WordPress (farmamed.nl)

Voeg dit toe aan een WordPress-pagina via een HTML-blok:

```html
<iframe 
  src="https://JOUW-APP.up.railway.app" 
  width="100%" 
  height="700px" 
  frameborder="0"
  style="border-radius:12px;">
</iframe>
```

Of gebruik een redirect naar de Railway-URL.
