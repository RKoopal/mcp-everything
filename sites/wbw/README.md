# WieBetaaltWat

Overgenomen uit ah-wbw (`app/wbw.py` en `app/archive.py`), waar dezelfde verzoeken dagelijks
draaien.

## Soort connector

| Keuze | Waarde |
| --- | --- |
| Toegang | Zonder browser, JSON-API onder `/api/` met `Accept-Version: 11` |
| Levering | JSON |
| Inloggen | E-mail en wachtwoord. Bij een verlopen sessie logt de connector één keer opnieuw in |
| Schrijven | `add_expense`, alleen met bevestiging van de gebruiker |

## Koppelen

De tool `connect` geeft een koppellink naar een formulier op de server. De gebruiker vult
daar zelf e-mail en wachtwoord in; Claude ziet ze niet. De server logt in en bewaart sessie
en gegevens versleuteld, zodat hij later zelf opnieuw kan inloggen.

## Schrijven

`add_expense` maakt een uitgave met `shares_attributes`; andere vormen geven 400.

- **Bevestigen.** Verwijderen is niet opgenomen, dus de tool is niet omkeerbaar en staat op
  `human_only`. De server vraagt bij elke aanroep om bevestiging (MCP-elicitation). Een
  client die dat niet kan, kan de tool niet gebruiken.
- **Geen dubbele posten.** Een schrijfactie vraagt een `idempotency_key`. Dezelfde sleutel
  twee keer geeft de eerste uitkomst terug, en een onbekende uitkomst blokkeert een nieuwe
  poging tot iemand het nakijkt.
- **Wat ontbreekt.** ah-wbw controleert ook dat WBW de verdeling overnam. Dat kan het format
  nog niet: de verifier kijkt alleen of `expense.id` gevuld is.

Neem een keer verwijderen op, dan kan `add_expense` omkeerbaar worden en zonder
bevestiging draaien.
