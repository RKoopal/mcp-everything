# Albert Heijn

Overgenomen uit ah-wbw (`app/ah/client.py`), waar dezelfde verzoeken elke vijf minuten
draaien. Alleen kassabonnen lezen.

## Soort connector

| Keuze | Waarde |
| --- | --- |
| Toegang | Zonder browser: `POST https://api.ah.nl/graphql` |
| Levering | GraphQL (`posReceiptsPage`, `posReceiptDetails`) |
| Inloggen | OAuth met de client-id `appie` en een roterende refresh-token |
| Schrijven | Geen |

## De app-identiteit

AH geeft alleen tokens uit aan zijn eigen app. Het manifest stuurt daarom de client-id,
User-Agent en `x-client-*`-headers van de iPhone-app mee. Besloten op 2026-11-04: dat mag,
voor je eigen account (regel 3 in de README).

Gebruik nooit de tokens van ah-wbw. AH vervangt de refresh-token bij elke verversing, dus
twee gebruikers van één token breken elkaar. Koppel deze connector met een eigen login.

## Koppelen

De tool `connect` geeft een koppellink. AH stuurt na het inloggen door naar
`appie://login-exit?code=…`. Een desktopbrowser kan dat adres niet openen, maar het staat in
het tabblad Network van de ontwikkelaarstools. De gebruiker plakt het op de koppelpagina; de
server wisselt de code in en bewaart alleen de tokens, versleuteld.

ah-wbw doet dit met een reverse proxy voor de loginpagina van AH, zodat niemand hoeft te
plakken. Die zit niet in deze connector.

## Waarnemingen

- AH geeft regelmatig 503 of 504. Lezen wordt herhaald (`attempts` staat op de standaard, 3).
- Een refresh-token werkt één keer. De connector vernieuwt daarom nooit gelijktijdig voor
  dezelfde gebruiker (`tests/test_runner.py`, de test met twee threads).
