# MCP Everything – recorder

Chrome-extensie die het verkeer van één site opneemt en naar je server stuurt. De server maakt
er tools van, voor iedereen die de server gebruikt, en bewaart jouw sessie versleuteld. Je werkt
in je gewone Chrome, ingelogd, zonder debugverbinding. De extensie voegt niets toe aan de
pagina. Wachtwoorden worden gemaskeerd. Op je computer komt niets te staan.

De extensie heeft pas toegang tot een site als jij daar toestemming voor geeft, en leest
alleen mee zolang een opname loopt. Cookies leest ze alleen bij **Link session**, en alleen
van de site in het huidige tabblad.

## Installeren

1. Download de extensie van je server: `<server>/extension.zip`. Het adres van de server staat
   er al in. Pak de zip uit. (Of gebruik deze map, als je de repo hebt. Daar staat een andere server
   in: opnemen werkt, maar de aanmeld- en inlogpagina van jouw server kunnen dan niet met de extensie praten.)
2. Open `chrome://extensions`.
3. Zet rechtsboven **Ontwikkelaarsmodus** aan.
4. Klik op **Uitgepakte extensie laden** en kies de map `mcp-everything-extension` (of deze map).
5. Zet de extensie vast via het puzzelstukje naast de adresbalk.

Na een nieuwe versie: klik in `chrome://extensions` op ↻ bij de extensie. Chrome leest de popup
elke keer opnieuw, maar het achtergrondscript pas na herladen. De popup zegt het zelf: staat er een
andere versie op schijf dan Chrome draait, dan toont ze een melding met een knop naar
`chrome://extensions`.

Een account krijg je met een uitnodigingslink (`<server>/signup/<code>`). Installeer de extensie
eerst en open dan de link: na het aanmelden geeft de pagina je token zelf aan de extensie.

## Met de server verbinden

Met een uitnodigingslink gaat dit vanzelf: zie hierboven. Met een token van een beheerder
(`python -m connector.users add <naam>`):

1. Klik op het icoon. De popup vraagt om je server en je token. Het adres van de server
   staat er al.
2. Plak het token (`usc_…`) en klik op **Connect**.

De extensie bewaart adres en token in `chrome.storage.local`. **Disconnect** onderin wist ze.

Op een site die de server al kent, toont de popup hoeveel tools er zijn en of je sessie
gekoppeld is.

## Opnemen

1. Ga naar de site en klik op het icoon van de extensie.
2. Klik op **Start recording**. De eerste keer vraagt Chrome om toestemming voor dit domein.
   Kies **Toestaan**: de opname start dan meteen. Het tabblad laadt opnieuw, zodat de opname
   vanaf het begin loopt.
3. Klik wat je wilt testen. Het icoon toont `REC`.
4. Klik op het icoon en daarna op **Stop and make tools**.

De server maakt de tools op de achtergrond. De popup toont elke stap met de tijd erbij:
versturen, Claude kiest de tools, controleren en opslaan. Het icoon toont `…` zolang dat
duurt, `✓` als het lukte en `!` als het misging. Sluit je de popup, dan gaat het gewoon door.

Als het klaar is, toont de popup welke tools erbij kwamen. Ga daarna naar Claude Code en
vraag wat je net deed. Draait Claude Code al, typ dan `/mcp` en verbind opnieuw, zodat
Claude de nieuwe tools ziet. Is Claude Code nog niet gekoppeld, klik dan op **Copy command
for Claude Code** en plak de opdracht in je terminal.

Logt de site met cookies in, dan koppelt de server meteen ook je sessie uit de opname.

## Sessie koppelen

Voor een site die met cookies inlogt en die de server al kent: log in op de site, open de
popup en klik op **Link session**. De extensie stuurt de cookies van die site naar de
server. De server bewaart ze versleuteld, behalve cookies van botbescherming. Verloopt de
sessie, dan klik je opnieuw. Sites met een wachtwoord of OAuth koppel je via de tool
`connect` in je chat.

## Opnemen met antwoorden

De gewone opname ziet verzoeken, ook uit workers, maar geen antwoorden. Met antwoorden kan
de server ook aanwijzen welk deel van een antwoord nuttig is.

1. Open DevTools op de site met `Cmd+Option+I`. De opname start zodra DevTools open is.
2. Laad de pagina opnieuw en klik wat je wilt testen.
3. Open in DevTools het tabblad **Connector** en klik op **Make tools**.

Deze opname ziet geen verkeer in workers. Sluit je DevTools vóór **Make tools**, dan is de
opname weg.

## Hoe het werkt

- `background.js` neemt op en praat met de server. Een opname gaat met `POST /api/recordings`
  naar de server. Die geeft meteen een id terug, en het script vraagt elke twee seconden
  `GET /api/recordings/<id>` op. Een fetch die langer dan 30 seconden wacht, kan Chrome
  afbreken; zo gebeurt dat niet.
- Toestemming: Chrome sluit de popup vaak zodra hij om toestemming vraagt. Daarom zegt de
  popup eerst aan het achtergrondscript wat er moet gebeuren. Dat script doet het zodra de
  toestemming er is (`chrome.permissions.onAdded`).
- De server: de sleutel `key` in `manifest.json` geeft de extensie een vast id
  (`mbgeknjocembkhfbaaaapcjphmojkjdn`), ook als je haar uitgepakt laadt. De server staat
  verzoeken van precies dat id toe (CORS), dus de extensie vraagt geen toegang tot de server.
- Aanmelden: alleen pagina's uit `externally_connectable` kunnen de extensie een bericht sturen.
  De aanmeldpagina stuurt zo het token. `/extension.zip` zet daar het adres van de eigen server in.
- Inloggen vanuit Claude: de inlogpagina van de server (OAuth) vraagt de extensie om het token
  (`{type: "token"}`). Ze geeft het alleen aan de server waarmee ze verbonden is.
- Koppelen met een code: AH geeft zijn code alleen aan de eigen app en stuurt je na het inloggen
  naar `appie://login-exit?code=…`. Chrome kan dat adres niet openen. De koppelpagina van de server
  vraagt de extensie daarom op te letten (`{type: "oauth"}`). `login.html` vraagt toegang tot de
  site, zet `catch.js` op die site en opent de login. AH springt na het inloggen met JavaScript
  naar dat adres: dat is geen verzoek, dus `webRequest` ziet het niet, maar het `navigate`-event
  van de pagina wel. Een gewone doorverwijzing (AH als je al ingelogd bent) ziet
  `webRequest.onBeforeRedirect`. Dan gaat dat tabblad dicht en verdwijnt `catch.js` weer. Alleen de koppelpagina die het vroeg krijgt het adres (`{type: "code"}`, één keer, tien
  minuten) en stuurt het zelf naar de server.
- De popup tekent wat het achtergrondscript in `chrome.storage.session` zet.

## Onderhoud

`tests/test_extension.py` houdt de samenvatting gelijk aan `connector/har.py`, en test het
achtergrondscript met een nagemaakte `chrome` en server.
