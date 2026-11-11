# bol.com

## Voorwaarden

Gelezen op 2026-11-01. Geen van de documenten noemt een versiedatum.

| Bron | Wat erin staat |
| --- | --- |
| [Klantaccountvoorwaarden](https://www.bol.com/nl/nl/tc/klantaccountvoorwaarden) | Art. 5: het account is voor eigen gebruik en derden krijgen geen toegang. Art. 6: kwaadwillig gebruik van technologie is verboden. Art. 7 en 8: bij redelijk vermoeden mag bol aankopen beperken of het account direct sluiten. |
| [Copyright](https://www.bol.com/nl/nl/tc/copyright) | De site is voor eigen of intern gebruik. Verveelvoudigen mag alleen door de site op één computer te downloaden en te bekijken, plus één print. Doorsturen of op een netwerk zetten mag niet zonder toestemming. |
| [Shophulp](https://www.bol.com/nl/nl/tc/gebruikersvoorwaarden-shophulp) | Scripts, bots en scrapers zijn expliciet verboden. Dit geldt alleen voor Shophulp. |
| [Privacybeleid](https://www.bol.com/nl/nl/tc/privacybeleid) | Bol gebruikt gegevens om misbruik door bots te vinden. Bol kan de bestelgeschiedenis gebruiken om bots bij populaire producten te weren. |
| [Thuiswinkelvoorwaarden](https://www.bol.com/nl/nl/tc/algemene-voorwaarden-thuiswinkel) | Niets over websitegebruik of automatisering. |
| [robots.txt](https://www.bol.com/robots.txt) | Crawlers mogen niet bij onder meer `/api/graphql`, `/*/s/`, `/*/account/`, `/*/order/` en `/*/rnwy/ajax/*`. |

Geen enkel document verbiedt een script voor de hele site. Bol kan een account wel
sluiten op een redelijk vermoeden, en daar hoeft geen overtreding voor bewezen te
zijn. Het grootste risico is dus je klantaccount. `robots.txt` is geen overeenkomst,
maar laat wel zien dat bol deze paden niet voor automatisering bedoelt.

Bij het openen van de voorwaarden verscheen eerst een beveiligingsscherm, dat vanzelf
verdween. Er is dus bescherming tegen bots. Die omzeilen we niet (regel 3).

## Besluiten

- **Opnemen mag.** Dat is gewoon handmatig gebruik in je eigen browser.
- **Shophulp blijft buiten de test.** Ook niet opnemen of afspelen.
- **Geen tool voor afrekenen.** Dit blijft handmatig.
- **Ingelogd testen, net als bij DEGIRO.** De ingebouwde browser is ingelogd als Reimer,
  dus de test gebeurt op zijn echte winkelwagen. Zijn bestaande artikel blijft staan.
- **Afspelen buiten de browser is een apart besluit.** Dat nemen we na de opname:
  - zonder account: alleen een paar losse verzoeken, handmatig gestart
  - met account: pas na een expliciet besluit van Reimer, en alleen op zijn eigen
    machine, want een sluiting raakt zijn echte account
- **Bij een blokkade stoppen we.** Dat is de uitkomst van de test.

## Soort connector

Dit wordt pas zeker na de opname.

| Keuze | Waarde |
| --- | --- |
| Levering | HTML met ingesloten data, plus GraphQL |
| Inloggen | Nog onbekend |
| Schrijven | Omkeerbaar: winkelwagen |

## Waarnemingen

Meegelezen in de ingebouwde browser op 2026-11-01. Dat is een andere opname dan de
HAR uit de DEGIRO-test: geen bestand, wel direct.

- **Zoeken:** `GET /nl/nl/s/?searchtext=...` geeft server-gerenderde HTML. De data zit in
  een inline script van ruim 500 kB voor React Router. Geen losse zoek-API gezien.
- **Product:** `GET /nl/nl/p/<slug>/<id>/` bevat JSON-LD met naam, varianten, beoordeling
  en prijsbereik.
- **In winkelwagen:** een gewone link naar `/nl/order/basket/addItems.html`.
- **GraphQL:** `POST /api/graphql` is op elke pagina actief. Ook advertentie-analytics
  lopen hierover.
- **Botbescherming:** op elke pagina draait `/.well-known/sbsd/519268`. Die stuurt een
  uitdaging naar de browser en post het antwoord terug. We bootsen dit niet na (regel 3).
- **Zonder browser:** drie keer `GET` van dezelfde productpagina met een eerlijke
  User-Agent: 403, 200 en 403. De inhoud van de 200 is niet bekeken. Bol weigert een script
  dus niet altijd, maar dit is te weinig om iets over toegang te zeggen. Eén zoekverzoek
  zonder browser gaf na de geslaagde extensie-opname ook 403. In de ingebouwde browser gaf
  dezelfde zoekopdracht vlak daarvoor 41 producten.
- **Opgeslagen queries:** bevestigd in de opname. Elk GraphQL-verzoek stuurt een
  `operationName`, variabelen en een hash. Gezien onder meer: `CreateBasket`,
  `BasketSubscriptionsQuery`, `CustomerOffersQuery`, `SearchHistory`.
- **Blokkade:** tijdens de standaardopname toonde bol "IP adres is geblokkeerd", vanwege
  mogelijk misbruik. Toevoegen aan de winkelwagen lukte daardoor niet.
- **Winkelwagen:** toevoegen en verwijderen lopen niet via de link maar via
  `/api/graphql`. De browser wist de antwoorden bij het wisselen van pagina, en de
  tool toont geen verzoekinhoud. De namen van de acties zijn dus niet gezien.
- **JavaScript:** in 116 bestanden staan fragmenten maar geen `query` of `mutation` met
  een naam. Niet alle routes waren geladen, dus dit is onvolledig.
- **Winkelwagen lezen:** `me.baskets` in GraphQL, met klanttype `IdentifiedCustomer`.

## Tools

| Tool | Bewijs | Wat niet gezien is | Gedraaid |
| --- | --- | --- | --- |
| `search` | Opname, met antwoord | Niets | Nee |
| `get_product` | Opname, met antwoord | Niets | Nee, 403 zonder browser |
| `add_to_cart` | Deels: `AddItem`, hash en invoer | Het antwoord | Nee |
| `remove_from_cart` | Deels: `RemoveItem`, hash en invoer | Het antwoord | Nee |
| `get_cart` | Deels: `BasketQueryWithoutTextResources` en `me.baskets` | De velden met artikelen | Nee |

Invoer van de schrijfacties, gezien in de opname:

- `AddItem`: `input` met `basketId`, `offerUid`, `productId` en `quantity`
- `RemoveItem`: `removeItemInput` met `basketId` en `itemId`

`list_orders` en `get_order_status` zijn vervallen: er komt geen bestelling, dus die
opname is niet gedaan.

## Resultaat

**Tweede poging, 2026-11-01: gelukt met de extensie.** Reimer klikte in zijn gewone Chrome,
ingelogd, met de achtergrondopname. Alle GraphQL-verzoeken gaven 200, er kwam geen blokkade
en `AddItem` en `RemoveItem` zijn gezien. Dit is één poging: het wijst op de debug-Chrome
als oorzaak van de blokkade, maar bewijst dat niet. Nog steeds heeft geen tool gedraaid.

**Eerste poging, 2026-11-01: bol blokkeerde het IP-adres van Reimer tijdelijk.** Reimer
beslist of en wanneer er iets naar bol gaat buiten zijn eigen klikken.

De oorzaak is niet bewezen. De test zelf is de waarschijnlijkste verklaring. Dit kwam kort
achter elkaar vanaf één IP-adres:

- geautomatiseerde klikken en meer dan 100 keer script-downloads in de ingebouwde browser
- losse verzoeken zonder browser
- een Chrome met debugverbinding en een nieuw profiel, in de standaardopname

Hoofdverdachte is de standaardopname: dezelfde winkelwagen-klikken werkten eerder wel, in de
ingebouwde browser en ingelogd. De opname-Chrome meldt `navigator.webdriver = true` aan
elke site, lokaal gecontroleerd. Dat signaal verbergen is omzeilen en doen we niet. Een
nieuwe poging gebruikt daarom een gewone browser, ingelogd, zonder debugverbinding.

Niet omzeilen: geen VPN, geen ander netwerk, geen andere browser om door te gaan. Afspelen vanuit
de browser is niet getest. De blokkadepagina verwijst voor scripts naar de Developer Guide
van bol; dat is de officiële route, als die ooit nodig is.
