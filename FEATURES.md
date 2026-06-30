# Farmamed Applicatie — Functionaliteit Checklist

Dit bestand is een controlelijst van alle endpoints en kernfuncties die in
`main.py` aanwezig moeten zijn. Gebruik dit bij elke wijziging om te
controleren of er niets verdwijnt.

## Hoe te gebruiken
Voor elke regel hieronder kan met `grep` worden gecontroleerd of de tekst
in `main.py` voorkomt. Bij twijfel over een nieuwe versie: laat Claude eerst
deze lijst langslopen vóórdat er wijzigingen worden gemaakt.

---

## Orders stroom (`/orders`, `/api/orders`)
- [ ] `GET /api/orders` — haalt pending + processing orders op (apart per status, niet gecombineerd — i.v.m. 402 fout)
- [ ] `toon_alle` parameter — "Mijn orders" = alleen processing, "Alle orders" = pending + processing
- [ ] Orders gesorteerd op ID hoog naar laag (`orders.sort(key=lambda o: o["id"], reverse=True)`)
- [ ] `created_via == "admin"` orders worden gefilterd tenzij `toon_alle=true`
- [ ] Admin-orders krijgen automatisch `heeft_verstrekking = True` (weergave-regel, geen DB wijziging)
- [ ] `heeft_verstrekking` veld op basis van `_farmamed_verstrekking` meta
- [ ] `POST /api/order-verstrekking` — zet `_farmamed_verstrekking = 1` in WooCommerce
- [ ] Goedkeuren/Afwijzen knoppen direct onder de 5-stappenbalk (niet onderaan pagina)
- [ ] Verstrekking-badge (groen) in linkerkolom orderlijst
- [ ] Recept preview via `/api/recept-preview-url` (Railway proxy, converteert PDF naar JPEG via PyMuPDF)
- [ ] Download recept via `/api/download-recept` — converteert JPG naar PDF indien nodig, bestandsnaam `{id}_Farmamed_{achternaam}.pdf`
- [ ] EDIFACT downloadknop alleen in actiebalk na goedkeuring (geen losse knop in EDIFACT tab)

## Bank stroom (`/bank`, `/api/verwerk-mt940`)
- [ ] `GET /bank` route bestaat, laadt `templates/bank.html`
- [ ] `POST /api/verwerk-mt940` — MT940 parser
  - [ ] Alleen betalingen €0–400 worden meegenomen
  - [ ] `/NAME/` veld fix voor `/NA\r\nME/` split (carriage return in MT940 bestand)
  - [ ] Ordernummer-detectie met spaties verwijderd (`rc_nospace`)
  - [ ] 4-cijferig nummer + spatie + ander cijfer wordt ook herkend (bijv. "3672 5")
  - [ ] AGB-code (02009907) wordt uitgesloten als fals-positief ordernummer
  - [ ] Matching: eerst ordernummer, dan naam-fuzzy (≥70%) + bedrag exact
  - [ ] `ongematchte_betalingen` in response — betalingen zonder match
  - [ ] Bulk-lookup van ordernummers in ongematchte betalingen (niet 1-voor-1, te traag)
  - [ ] `naam_match` fuzzy matching (≥65%) tegen pending orders voor ongematchte betalingen
- [ ] `POST /api/betalingen-verwerken` — zet status naar `processing` + `_farmamed_bank_betaald = 1`
- [ ] bank.html: order links, gevonden betaling rechts (niet andersom)
- [ ] bank.html: alleen niet-completed matches tonen in "zonder match" sectie

## Werklijst (`/status`)
- [ ] Geen MT940 import sectie meer (verplaatst naar `/bank`)
- [ ] Betaalstatus = exact één van: "Pay", "Bank", "Anders", "Niet betaald"
  - [ ] `status == "pending"` → altijd "Niet betaald" (ongeacht date_paid)
  - [ ] Pay: `transaction_id` + payment_method bevat "pay"/"paynl"
  - [ ] Bank: `_farmamed_bank_betaald == "1"`
  - [ ] Anders: processing/completed zonder Pay of Bank (incl. handmatig afgerond zonder betaling)
- [ ] Verstrekking-kolom toont `heeft_verstrekking` uit WooCommerce meta
- [ ] Handmatig vinkje aanklikbaar → roept `/api/order-verstrekking` aan

## E-mail stroom (`/emails`)
- [ ] `_email_poller_loop()` draait als achtergrondtaak vanaf `@app.on_event("startup")`
- [ ] `POST /api/poll-emails` — triggert directe eenmalige poll
- [ ] `POST /api/emails-volledig-herladen` — wist cache, haalt hele inbox synchroon opnieuw op
- [ ] Poller gebruikt `UID SEARCH`/`UID FETCH` (NIET gewone SEARCH — sequentienummers zijn niet stabiel)
- [ ] E-mails gesorteerd numeriek op UID (`CAST(uid AS INTEGER) ASC`) = exacte inbox-volgorde
- [ ] Map "Afgehandeld" heet `INBOX.Afgehandeld` (punt-scheidingsteken, NIET `/` — deze IMAP-server gebruikt geen slash)
- [ ] `POST /api/email-verwerkt` roept `_verplaats_email_imap(email_uid)` aan
- [ ] "Markeer als afgehandeld (geen bestelling)" — losse knop, werkt zonder order aan te maken
- [ ] `_verwijder_emails_niet_in()` — verwijdert lokale records van mails niet meer in INBOX
- [ ] `POST /api/zoek-herhaalorder`:
  - [ ] Claude haalt ook `geboortedatum` uit e-mail (naast naam, email, medicijn)
  - [ ] Stap 1: zoek op e-mailadres (exact, score 100)
  - [ ] Stap 2: naam-fuzzy + geboortedatum exact → score minimaal 95% indien beide kloppen
  - [ ] `huidige_order_id` wordt overal uitgefilterd (order matcht niet aan zichzelf)

## Algemeen / WooCommerce API
- [ ] `_wc_auth(wc_key, wc_secret)` helper — bouwt Basic Auth header + `User-Agent: curl/7.68.0`
- [ ] ALLE WooCommerce API calls gebruiken `headers=_wc_auth(...)`, NOOIT `auth=(wc_key, wc_secret)`
  (reden: sommige LiteSpeed/server-configuraties geven 402 bij de Python requests user-agent
  of bij specifieke parametercombinaties zoals `status=pending,processing`)
- [ ] Pending + processing apart ophalen in plaats van gecombineerd (`status=pending,processing` gaf 402)

## WordPress Plugin (`farmamed_recept_api.php`)
- [ ] `farmamed_blokkeer_afronden` — blokkeert `pending → completed` tenzij betaald (Pay/Bank/admin-scherm)
- [ ] `farmamed_check_reeds_verzonden` — als order naar `processing` gaat (betaald) EN er al een
      SendCloud/PostNL tracking-notitie bestaat → automatisch door naar `completed`
- [ ] Beide hooks op `woocommerce_order_status_changed`, named functions (niet anonieme closures —
      `__FUNCTION__` werkt niet in PHP closures voor remove_action/add_action)

---

## Bekende valkuilen (waarom dingen eerder zijn misgegaan)
1. **Oude bestanden uploaden**: als een ouder lokaal bestand wordt geüpload i.p.v. de laatste
   GitHub-versie, verdwijnen alle tussentijdse fixes stilzwijgend.
2. **Tekstvervangingen die net niet matchen**: een `old → new` vervanging kan een deel van de
   oude code laten "hangen" (dode code na een return-statement bijv.), wat een
   `IndentationError` of stille bug veroorzaakt.
3. **Kopiëren/plakken in de GitHub webeditor**: kan whitespace/inspringing beschadigen.
   Upload bestanden altijd als heel bestand (Add file → Upload files), niet kopiëren/plakken.
4. **IMAP scheidingsteken**: deze specifieke mailserver (mail.interip.nl) gebruikt een **punt**
   (`INBOX.Afgehandeld`), niet een schuine streep — dit verschilt per mailserver/provider.
