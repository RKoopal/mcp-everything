# Marktplaats

## Voorwaarden

Gelezen op 2026-11-02. De gebruiksvoorwaarden gelden sinds 22 juli 2026. De hoofdstukken
II tot en met V zijn alleen doorzocht op automatisering, scripts, bots en API's.

| Bron | Wat erin staat |
| --- | --- |
| [Gebruiksvoorwaarden](https://help.marktplaats.nl/s/article/gebruiksvoorwaarden), art. 7.1 | De inhoud mag niet worden gekopieerd of openbaar gemaakt. Uitzondering: persoonlijk gebruik of nieuwsberichten, tot 100 advertenties of links. |
| Art. 7.2 | Persoonsgegevens van adverteerders verzamelen mag niet. |
| Art. 7.3 | Marktplaats heeft databankrecht op de advertenties. Een substantieel deel opvragen mag niet, en kleine delen herhaald en systematisch opvragen ook niet, behalve binnen de uitzondering van art. 7.1. |
| Art. 7.4 en 7.5 | Advertenties plaatsen met een geautomatiseerd systeem mag niet zonder toestemming. API-partners krijgen die toestemming. |
| Art. 10.3 en 10.6 | Marktplaats kan een gebruiker ook zonder overtreding of waarschuwing uitsluiten, bijvoorbeeld bij onredelijk gebruik. |
| Hoofdstuk II, art. 5.2 | Inloggegevens aan derden geven mag niet. |
| [robots.txt](https://www.marktplaats.nl/robots.txt) | Crawlers mogen niet bij onder meer `/lrp/api/search*`, `/lp/api/listings*`, `/v/api/*` (deels), `/messages*`, `/mymp/`, `/asq`, `/bid/`, `/syi/` en `/flag/`. |

Geen enkele passage verbiedt een script voor persoonlijk gebruik. De grens is art. 7.3:
af en toe zelf iets zoeken valt binnen persoonlijk gebruik, systematisch opvragen niet.
Plaatsen met een script is wel expliciet verboden. `robots.txt` is geen overeenkomst,
maar laat zien dat Marktplaats de zoek-API niet voor automatisering bedoelt. Het grootste
risico is dus het account van Reimer.

## Besluiten

Voorstel, nog niet bevestigd door Reimer.

- **Geen tools voor plaatsen, bieden, berichten sturen of afrekenen.** Plaatsen is
  verboden; de rest is onomkeerbaar of naar buiten gericht. Eigen gesprekken lezen mag wel.
- **Alleen persoonlijk gebruik:** losse opdrachten, handmatig gestart, geen periodieke
  zoekopdrachten en niet meer dan 100 advertenties per keer.
- **Geen gegevens van adverteerders bewaren** (art. 7.2). `search` laat de verkoper weg.
- **Zoeken via `/q/`, niet via `/lrp/api/search`.** Dat tweede pad staat in `robots.txt`.
- **Afspelen is een apart besluit van Reimer.** Eerst zonder sessie, één verzoek. De tools
  voor gesprekken gebruiken `/messages*`, dat in `robots.txt` staat. Dat weegt mee.
- **Bij een blokkade stoppen we.** De connector herhaalt daarom niets, ook geen 429 of 503.

## Soort connector

| Keuze | Waarde |
| --- | --- |
| Toegang | Zoeken zonder sessie: zonder browser, één verzoek. Met sessie: nog niet getest |
| Levering | HTML met `__NEXT_DATA__` (zoeken), JSON (de rest) |
| Inloggen | Cookies, vernieuwen kan alleen een mens |
| Schrijven | Geen |

## Waarnemingen

Twee opnames met de extensie, 2026-11-02, in Reimers gewone Chrome: 21:04 (zoeken, categorie,
inloggen) en 21:05 (berichten). Geen antwoorden, want de achtergrondopname bewaart die niet.

- **Twee opnames, één values-bestand.** De extensie overschrijft `marktplaats.values.json`
  bij elke opname en maakt van de samenvatting een nieuw bestand. In `recordings/` hoort de
  samenvatting dus bij de eerste opname en de waarden bij de tweede. De waarden van de
  zoekopdracht zijn weg. De samenvatting van de tweede opname staat vermoedelijk nog in
  Downloads als `marktplaats (1).json`; niet gecontroleerd.
- **Zoeken:** `GET /q/<tekst>/` is server-gerenderd. `__NEXT_DATA__` bevat
  `props.pageProps.searchRequestAndResponse.listings`, gezien in de ingebouwde browser op één
  pagina (36 advertenties). De site zelf gebruikt ook `GET /lrp/api/search`.
- **Berichten:** de pagina `/messages` gebruikt een RPC-API onder `/messages/api/rpc/`, met
  `input` als JSON in de query (`{"json": {...}}`).
- **Gelezen markeren:** een gesprek openen stuurt `POST conversations.markAsRead` met de
  header `x-mp-xsrf`. Een terugdraai-actie is niet gezien, dus dit zou `human_only` zijn. Deze
  tool ontbreekt bewust: lezen heeft hem niet nodig.
- **Niet opgenomen:** berichten sturen, favorieten, bieden.
- **Botbescherming:** elke sessie heeft een `aws-waf-token`, en de login stuurt een
  ThreatMetrix-handtekening. Beide bootsen we niet na en sturen we niet mee (regel 3). De
  connector stuurt ook geen browser-headers.
- **Inloggen:** `POST /identity/v2/api/login`. Geen tool: een mens logt in, in de browser.

## Tools

| Tool | Sessie | Bewijs | Wat niet gezien is | Gedraaid |
| --- | --- | --- | --- | --- |
| `search` | Nee | Gedraaid, verifier geslaagd | Niets | Ja, één keer |
| `message_count` | Ja | Opname | Het antwoord | Nee |
| `notification_count` | Ja | Opname | Het antwoord | Nee |
| `recent_searches` | Ja | Opname | Het antwoord | Nee |
| `saved_searches` | Ja | Opname | Het antwoord | Nee |
| `get_conversations` | Ja | Opname | Het antwoord | Nee |
| `get_messages` | Ja | Opname | Het antwoord | Nee |
| `get_advertisement` | Ja | Opname | Het antwoord | Nee |

Alleen `search` normaliseert. De andere tools geven het ruwe antwoord terug tot de vorm
bekend is. Welke van de drie sessiecookies echt nodig zijn, is ook nog niet bekend.

## Gebruiken

Zoeken, zonder sessie:

```bash
uv run python -c "from sites.marktplaats import connector as c; print(c.search(c.connect(), 'fujifilm xt30')[:3])"
```

Met sessie: neem op terwijl je ingelogd bent en laad die opname. De sessie gaat naar
`~/.config/mcp-everything/marktplaats/` (0600).

```bash
uv run python -c "from sites.marktplaats import connector as c; c.load_session('recordings/marktplaats.values.json'); print(c.message_count(c.connect(logged_in=True)))"
```

## Resultaat

**`search` werkt, 2026-11-02.** Eén verzoek met een eerlijke `python-requests`-User-Agent,
zonder sessie, in opdracht van Reimer: HTTP 200, 810 kB HTML, geen doorverwijzing. Dat gaf 36
advertenties voor "fujifilm xt30", met id, titel, prijs, prijstype, datum en URL ingevuld.
Bij één advertentie ontbrak de plaats. De resultaten kloppen met de zoekterm.

Dit is één verzoek: het zegt niets over hoe Marktplaats met meer verkeer omgaat. De zeven
tools met sessie hebben niet gedraaid, en daarover beslist Reimer apart.
