"""
HILMA + Cloudia Apuväline-agentti
==================================
Hakee HILMAsta ja Cloudiasta apuvälinekilpailutuksia, analysoi ne Claude AI:lla
ja lähettää yhteenvedon sähköpostiin.

Asennus:
    pip install requests anthropic beautifulsoup4

Ympäristömuuttujat (aseta ennen ajoa):
    ANTHROPIC_API_KEY   - Anthropic API-avain (console.anthropic.com)
    EMAIL_FROM          - Lähettäjän sähköposti (Gmail)
    EMAIL_TO            - Vastaanottajan sähköposti
    GMAIL_APP_PASSWORD  - Gmail-sovellussalasana (myaccount.google.com > Turvallisuus > Sovellussalasanat)

Ajo:
    python hilma_apuvaline_agentti.py

Ajastus (Linux/Mac crontab - ajetaan joka aamu klo 8):
    0 8 * * * /usr/bin/python3 /polku/hilma_apuvaline_agentti.py
"""

import os
import json
import smtplib
import requests
import anthropic
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup


# ─── KONFIGURAATIO ────────────────────────────────────────────────────────────

# Hakusanat – muokkaa omien tuotteidesi mukaan
HAKUSANAT = [
    "rollaattori",
    "pyörätuoli",
    "kyynärsauva",
    "potilasnostin",
    "suihkutuoli",
    "suihkuvanu",
    "wc-koroke",
    "wc koroke",
    "potilasvaa'at",
    "potilasvaaka",
    "liikkumisen apuväline",
    "apuväline",
    "liikkumisapuväline",
    "kävelyteline",
    "kävelyke",
]

# CPV-koodit apuvälineille (EU:n hankintanimikkeistö)
# Lisää tai poista tarpeen mukaan: https://simap.ted.europa.eu/cpv
CPV_KOODIT = [
    "33196200",  # Vammaisten apuvälineet
    "33196100",  # Vanhusten apuvälineet
    "33193000",  # Pyörätuolit ja liikkumisapuvälineet
    "33193100",  # Pyörätuolit
    "33193200",  # Pyörätuolien osat ja tarvikkeet
    "33141600",  # Keräys- ja infuusiolaitteet
    "33158400",  # Sähköstimulaatiolaitteet
]

# Kuinka monta päivää taaksepäin haetaan (1 = vain tänään julkaistut)
HAKU_PAIVAT = 1

# Vain ilmoitukset joiden arvo on vähintään tämä (EUR), 0 = kaikki
MIN_ARVO_EUR = 0

# API-avaimet ympäristömuuttujista
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


# ─── HILMA-HAKU ───────────────────────────────────────────────────────────────

def hae_hilmasta() -> list[dict]:
    """
    Hakee HILMA:n avoimesta REST-rajapinnasta kilpailutuksia.
    Dokumentaatio: https://hankintailmoitukset.fi/fi/docs/api
    """
    print("Haetaan HILMAsta...")

    base_url = "https://hankintailmoitukset.fi/api/v2/notices"
    alku_pvm = (datetime.now() - timedelta(days=HAKU_PAIVAT)).strftime("%Y-%m-%d")

    kaikki_ilmoitukset = []

    # Hae avainsanoilla
    for hakusana in HAKUSANAT:
        try:
            params = {
                "keyword": hakusana,
                "publishedFrom": alku_pvm,
                "limit": 20,
                "offset": 0,
            }
            resp = requests.get(base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            ilmoitukset = data.get("notices", data.get("items", []))
            print(f"  '{hakusana}': {len(ilmoitukset)} osumaa")
            kaikki_ilmoitukset.extend(ilmoitukset)
        except Exception as e:
            print(f"  Varoitus: haku sanalla '{hakusana}' epäonnistui: {e}")

    # Hae CPV-koodeilla
    for cpv in CPV_KOODIT:
        try:
            params = {
                "cpvCode": cpv,
                "publishedFrom": alku_pvm,
                "limit": 20,
                "offset": 0,
            }
            resp = requests.get(base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            ilmoitukset = data.get("notices", data.get("items", []))
            if ilmoitukset:
                print(f"  CPV {cpv}: {len(ilmoitukset)} osumaa")
            kaikki_ilmoitukset.extend(ilmoitukset)
        except Exception as e:
            print(f"  Varoitus: CPV-haku {cpv} epäonnistui: {e}")

    # Poista duplikaatit (sama ilmoitus-ID)
    nahdyt_idt = set()
    uniikit = []
    for ilm in kaikki_ilmoitukset:
        ilm_id = ilm.get("id") or ilm.get("noticeId") or ilm.get("noticeNumber")
        if ilm_id and ilm_id not in nahdyt_idt:
            nahdyt_idt.add(ilm_id)
            uniikit.append(ilm)

    print(f"Yhteensä {len(uniikit)} uniikkia ilmoitusta löytyi.\n")
    return uniikit


# ─── CLAUDE-ANALYYSI ──────────────────────────────────────────────────────────

def analysoi_claudella(ilmoitukset: list[dict]) -> list[dict]:
    """
    Lähettää ilmoitukset Claude AI:lle analysoitavaksi.
    Claude arvioi relevanssin ja tiivistää sisällön.
    """
    if not ilmoitukset:
        return []

    if not ANTHROPIC_API_KEY:
        print("Varoitus: ANTHROPIC_API_KEY puuttuu – ohitetaan Claude-analyysi.")
        return [{"ilmoitus": ilm, "relevanssi": "tuntematon", "perustelu": "-", "yhteenveto": ilm.get("title", "")} for ilm in ilmoitukset]

    print("Analysoidaan Claude AI:lla...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Muotoile ilmoitukset tekstiksi Claudelle
    ilmoitus_teksti = ""
    for i, ilm in enumerate(ilmoitukset, 1):
        ilmoitus_teksti += f"""
Ilmoitus {i}:
  Otsikko: {ilm.get('title', ilm.get('noticeName', 'Ei otsikkoa'))}
  Hankintayksikkö: {ilm.get('contractingAuthority', {}).get('name', ilm.get('organisation', 'Ei tietoa'))}
  Kuvaus: {ilm.get('description', ilm.get('shortDescription', 'Ei kuvausta'))[:500]}
  Arvo: {ilm.get('estimatedValue', {}).get('amount', ilm.get('estimatedTotalValue', 'Ei tietoa'))} EUR
  Deadline: {ilm.get('submissionDeadline', ilm.get('tenderDeadline', 'Ei tietoa'))}
  URL: {ilm.get('noticeUrl', ilm.get('url', ''))}
"""

    prompt = f"""Olet apuvälineyritysten hankinta-asiantuntija. Yrityksemme myy seuraavia tuotteita:
- Rollaattorit ja kävelytuet
- Pyörätuolit (manuaaliset ja sähköiset)
- Kyynärsauvat ja kävelykepit
- Potilasnostimet ja siirtovälineet
- Suihkutuolit, suihkuvaunut ja kylpyhuoneapuvälineet
- WC-korokkeet ja WC-apuvälineet
- Potilasvaa'at
- Muut liikkumisen apuvälineet

Analysoi seuraavat HILMA-hankintailmoitukset ja arvioi kuinka relevantteja ne ovat yrityksemme kannalta.

{ilmoitus_teksti}

Palauta vastauksesi AINOASTAAN JSON-muodossa, ilman mitään muuta tekstiä:
{{
  "analyysit": [
    {{
      "ilmoitus_numero": 1,
      "relevanssi": "korkea" | "kohtalainen" | "matala" | "ei relevantti",
      "perustelu": "lyhyt perustelu suomeksi (max 30 sanaa)",
      "yhteenveto": "tiivistetty kuvaus suomeksi (max 20 sanaa)",
      "suositeltava_toimenpide": "tarjoa" | "seuraa" | "ohita"
    }}
  ]
}}"""

    try:
        viesti = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        vastaus_teksti = viesti.content[0].text.strip()

        # Poista mahdolliset markdown-koodimerkit
        if vastaus_teksti.startswith("```"):
            vastaus_teksti = vastaus_teksti.split("```")[1]
            if vastaus_teksti.startswith("json"):
                vastaus_teksti = vastaus_teksti[4:]

        analyysi_data = json.loads(vastaus_teksti)
        analyysit = analyysi_data.get("analyysit", [])

        # Yhdistä alkuperäinen ilmoitus analyysiin
        tulokset = []
        for a in analyysit:
            idx = a["ilmoitus_numero"] - 1
            if 0 <= idx < len(ilmoitukset):
                tulokset.append({
                    "ilmoitus": ilmoitukset[idx],
                    "relevanssi": a.get("relevanssi", "tuntematon"),
                    "perustelu": a.get("perustelu", ""),
                    "yhteenveto": a.get("yhteenveto", ""),
                    "toimenpide": a.get("suositeltava_toimenpide", "seuraa"),
                })

        # Suodata pois ei-relevantit
        relevantit = [t for t in tulokset if t["relevanssi"] != "ei relevantti"]
        print(f"Claude arvioi {len(relevantit)}/{len(ilmoitukset)} ilmoitusta relevanteiksi.\n")
        return relevantit

    except Exception as e:
        print(f"Virhe Claude-analyysissä: {e}")
        return [{"ilmoitus": ilm, "relevanssi": "tuntematon", "perustelu": "", "yhteenveto": "", "toimenpide": "seuraa"} for ilm in ilmoitukset]


# ─── SÄHKÖPOSTI ───────────────────────────────────────────────────────────────

def muodosta_sahkoposti(tulokset: list[dict]) -> tuple[str, str]:
    """Muodostaa sähköpostin otsikon ja HTML-sisällön."""

    pvm = datetime.now().strftime("%d.%m.%Y")
    korkeat = [t for t in tulokset if t["relevanssi"] == "korkea"]
    kohtalaiset = [t for t in tulokset if t["relevanssi"] == "kohtalainen"]
    matalat = [t for t in tulokset if t["relevanssi"] == "matala"]

    otsikko = f"HILMA-agentti {pvm}: {len(tulokset)} kilpailutusta – {len(korkeat)} korkean prioriteetin"

    def ilmoitus_html(tulos: dict, vari: str) -> str:
        ilm = tulos["ilmoitus"]
        nimi = ilm.get("title") or ilm.get("noticeName") or "Ei otsikkoa"
        hankintayksikko = (ilm.get("contractingAuthority") or {}).get("name") or ilm.get("organisation") or "Ei tietoa"
        arvo = (ilm.get("estimatedValue") or {}).get("amount") or ilm.get("estimatedTotalValue") or "Ei tietoa"
        deadline = ilm.get("submissionDeadline") or ilm.get("tenderDeadline") or "Ei tietoa"
        url = ilm.get("noticeUrl") or ilm.get("url") or "https://hankintailmoitukset.fi"
        ilm_id = ilm.get("id") or ilm.get("noticeId") or ilm.get("noticeNumber") or ""

        return f"""
        <div style="border-left: 4px solid {vari}; padding: 12px 16px; margin: 12px 0; background: #f9f9f9; border-radius: 0 6px 6px 0;">
          <div style="font-weight: bold; font-size: 15px; color: #1a1a1a;">{nimi}</div>
          <div style="color: #555; font-size: 13px; margin-top: 4px;">{hankintayksikko}</div>
          <div style="margin-top: 8px; font-size: 13px; color: #333;">{tulos.get('yhteenveto', '')}</div>
          <div style="margin-top: 6px; font-size: 12px; color: #777;">
            💰 Arvo: <b>{arvo} €</b> &nbsp;|&nbsp;
            📅 Deadline: <b>{deadline}</b> &nbsp;|&nbsp;
            🎯 Suositus: <b>{tulos.get('toimenpide', '').upper()}</b>
          </div>
          <div style="margin-top: 4px; font-size: 12px; color: #999; font-style: italic;">{tulos.get('perustelu', '')}</div>
          <a href="{url}" style="display: inline-block; margin-top: 8px; font-size: 12px; color: #185FA5;">
            Avaa ilmoitus HILMAssa →
          </a>
        </div>"""

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; color: #1a1a1a;">

    <div style="background: #185FA5; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
      <h1 style="margin: 0; font-size: 20px;">HILMA Apuväline-agentti</h1>
      <p style="margin: 4px 0 0; opacity: 0.85; font-size: 14px;">{pvm} – {len(tulokset)} relevanttia kilpailutusta löytyi</p>
    </div>

    <div style="padding: 20px 24px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px;">

      <div style="display: flex; gap: 16px; margin-bottom: 20px;">
        <div style="flex: 1; text-align: center; padding: 12px; background: #e8f4e8; border-radius: 6px;">
          <div style="font-size: 28px; font-weight: bold; color: #2d6a2d;">{len(korkeat)}</div>
          <div style="font-size: 12px; color: #555;">Korkea prioriteetti</div>
        </div>
        <div style="flex: 1; text-align: center; padding: 12px; background: #fff4e0; border-radius: 6px;">
          <div style="font-size: 28px; font-weight: bold; color: #8a5500;">{len(kohtalaiset)}</div>
          <div style="font-size: 12px; color: #555;">Kohtalainen</div>
        </div>
        <div style="flex: 1; text-align: center; padding: 12px; background: #f5f5f5; border-radius: 6px;">
          <div style="font-size: 28px; font-weight: bold; color: #555;">{len(matalat)}</div>
          <div style="font-size: 12px; color: #555;">Matala</div>
        </div>
      </div>
    """

    if korkeat:
        html += '<h2 style="color: #2d6a2d; font-size: 15px; margin-top: 0;">🟢 Korkea prioriteetti – harkitse tarjoamista</h2>'
        for t in korkeat:
            html += ilmoitus_html(t, "#2d6a2d")

    if kohtalaiset:
        html += '<h2 style="color: #8a5500; font-size: 15px; margin-top: 20px;">🟡 Kohtalainen – seuraa tilannetta</h2>'
        for t in kohtalaiset:
            html += ilmoitus_html(t, "#f0a500")

    if matalat:
        html += '<h2 style="color: #888; font-size: 15px; margin-top: 20px;">⚪ Matala relevanssi</h2>'
        for t in matalat:
            html += ilmoitus_html(t, "#ccc")

    html += f"""
      <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 24px 0;">
      <p style="font-size: 12px; color: #999; text-align: center;">
        Haettu HILMAsta {pvm} · Analysoitu Claude AI:lla ·
        <a href="https://hankintailmoitukset.fi" style="color: #185FA5;">hankintailmoitukset.fi</a>
      </p>
    </div></body></html>"""

    return otsikko, html


def laheta_sahkoposti(otsikko: str, html_sisalto: str) -> bool:
    """Lähettää sähköpostin Gmail SMTP:n kautta."""

    if not all([EMAIL_FROM, EMAIL_TO, GMAIL_APP_PASSWORD]):
        print("Varoitus: Sähköpostiasetukset puuttuvat – tulostetaan sähköposti konsoliin.\n")
        print(f"OTSIKKO: {otsikko}")
        print("(HTML-sisältö generoitu, aseta EMAIL_FROM, EMAIL_TO ja GMAIL_APP_PASSWORD)")
        return False

    viesti = MIMEMultipart("alternative")
    viesti["Subject"] = otsikko
    viesti["From"] = EMAIL_FROM
    viesti["To"] = EMAIL_TO
    viesti.attach(MIMEText(html_sisalto, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, viesti.as_string())
        print(f"Sähköposti lähetetty: {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"Virhe sähköpostin lähetyksessä: {e}")
        return False


# ─── CLOUDIA-HAKU ─────────────────────────────────────────────────────────────

def hae_cloudiasta() -> list[dict]:
    """
    Hakee Cloudian kilpailutussivulta apuväline-ilmoituksia.
    Cloudia on Suomen suurin hankintarengas (kunnat, kuntayhtymät).
    """
    print("Haetaan Cloudiasta...")

    cloudia_ilmoitukset = []
    alku_pvm = datetime.now() - timedelta(days=HAKU_PAIVAT)

    # Cloudia tarjoaa avoimen hakusivun kilpailutuksille
    for hakusana in HAKUSANAT[:6]:  # Rajoitetaan hakuja ettei tule liikaa
        try:
            url = "https://www.cloudia.fi/kilpailutukset"
            params = {
                "search": hakusana,
                "status": "open",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; HILMA-agentti/1.0)"
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Etsi kilpailutuskortteja sivulta
            # Cloudia käyttää erilaisia CSS-luokkia eri versioissa
            kortit = (
                soup.find_all("div", class_="tender-item") or
                soup.find_all("article", class_="tender") or
                soup.find_all("li", class_="competition-item") or
                soup.find_all("div", class_="competition") or
                soup.find_all(attrs={"data-tender": True})
            )

            for kortti in kortit:
                try:
                    otsikko_el = (
                        kortti.find("h2") or kortti.find("h3") or
                        kortti.find("a", class_="title") or kortti.find("a")
                    )
                    otsikko = otsikko_el.get_text(strip=True) if otsikko_el else ""

                    if not otsikko:
                        continue

                    linkki_el = kortti.find("a", href=True)
                    linkki = linkki_el["href"] if linkki_el else ""
                    if linkki and not linkki.startswith("http"):
                        linkki = "https://www.cloudia.fi" + linkki

                    # Etsi päivämäärä
                    pvm_el = kortti.find(class_=lambda c: c and "date" in c.lower()) if kortti else None
                    deadline = pvm_el.get_text(strip=True) if pvm_el else "Ei tietoa"

                    # Etsi hankintayksikkö
                    org_el = kortti.find(class_=lambda c: c and ("org" in c.lower() or "buyer" in c.lower())) if kortti else None
                    organisaatio = org_el.get_text(strip=True) if org_el else "Cloudia-jäsen"

                    cloudia_ilmoitukset.append({
                        "id": f"cloudia-{linkki}",
                        "title": otsikko,
                        "organisation": organisaatio,
                        "description": f"Kilpailutus löytyi hakusanalla: {hakusana}",
                        "submissionDeadline": deadline,
                        "noticeUrl": linkki,
                        "lahde": "Cloudia",
                    })
                except Exception:
                    continue

            if kortit:
                print(f"  Cloudia '{hakusana}': {len(kortit)} osumaa")

        except Exception as e:
            print(f"  Varoitus: Cloudia-haku sanalla '{hakusana}' epäonnistui: {e}")

    # Poista duplikaatit
    nahdyt = set()
    uniikit = []
    for ilm in cloudia_ilmoitukset:
        avain = ilm.get("id", ilm.get("title", ""))
        if avain and avain not in nahdyt:
            nahdyt.add(avain)
            uniikit.append(ilm)

    print(f"Cloudia: {len(uniikit)} uniikkia ilmoitusta.\n")
    return uniikit


# ─── PÄÄOHJELMA ───────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("HILMA Apuväline-agentti")
    print(f"Ajettu: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("=" * 50 + "\n")

    # 1. Hae HILMA-ilmoitukset
    hilma_ilmoitukset = hae_hilmasta()

    # 2. Hae Cloudia-ilmoitukset
    cloudia_ilmoitukset = hae_cloudiasta()

    # 3. Yhdistä kaikki ilmoitukset
    kaikki_ilmoitukset = hilma_ilmoitukset + cloudia_ilmoitukset

    if not kaikki_ilmoitukset:
        print("Ei uusia ilmoituksia tänään. Ei lähetetä sähköpostia.")
        return

    print(f"Yhteensä {len(kaikki_ilmoitukset)} ilmoitusta (HILMA: {len(hilma_ilmoitukset)}, Cloudia: {len(cloudia_ilmoitukset)})\n")

    # 4. Analysoi Claude AI:lla
    tulokset = analysoi_claudella(kaikki_ilmoitukset)

    if not tulokset:
        print("Ei relevantteja ilmoituksia löytynyt. Ei lähetetä sähköpostia.")
        return

    # 5. Muodosta ja lähetä sähköposti
    otsikko, html = muodosta_sahkoposti(tulokset)
    laheta_sahkoposti(otsikko, html)

    print("\nValmis!")
    print(f"Löydettiin {len(tulokset)} relevanttia kilpailutusta.")


if __name__ == "__main__":
    main()
