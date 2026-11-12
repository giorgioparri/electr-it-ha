<p align="center">
  <img src="https://raw.githubusercontent.com/giorgioparri/electr-it-ha/HEAD/images/logo.png" alt="electrITHA" width="320">
</p>

<p align="right"><a href="https://github.com/giorgioparri/electr-it-ha/blob/HEAD/README.md">🇬🇧 Read in English</a></p>

# electrITHA — Integrazione personalizzata per Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/giorgioparri/electr-it-ha.svg)](https://github.com/giorgioparri/electr-it-ha/releases)
[![Validate](https://github.com/giorgioparri/electr-it-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/giorgioparri/electr-it-ha/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/giorgioparri/electr-it-ha)](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/LICENSE)

[![Apri la tua istanza di Home Assistant e apri un repository nell'Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=giorgioparri&repository=electr-it-ha&category=integration)

Un'integrazione per [Home Assistant](https://www.home-assistant.io/) che ti dice quanto ti sta costando l'elettricità che prelevi dalla rete, quasi in tempo reale, applicando le regole di un **contratto di fornitura domestico italiano** alle letture dei tuoi contatori: prezzo dell'energia (indicizzato al PUN, fisso o regolato), perdite di rete, dispacciamento, trasporto, oneri di sistema, accise e IVA.

> **Perché esiste:** il PUN è solo una parte di quello che costa un kWh. Nelle bollette su cui questa integrazione è verificata valeva tra un terzo e poco più della metà del totale; il resto è trasporto, oneri di sistema, dispacciamento, accise e IVA, ciascuno con la sua regola. Questa integrazione le applica tutte, come fa il tuo venditore, ed è controllata su bollette vere.

---

## ⚠️ Note importanti

- **Durante il mese i valori sono stime.** La maggior parte dei contratti indicizzati fattura la *media mensile* del PUN per fascia, che è definitiva solo a mese concluso. Ogni valore stimato ha l'attributo `estimated`.
- **I valori regolati sono inclusi, e coprono il 2026.** Trasporto, oneri di sistema, prezzi del servizio di tutela e imposte sono quelli pubblicati da ARERA per ogni trimestre del 2026. Per una data fuori dal periodo coperto i sensori diventano sconosciuti e dicono perché, invece di inventare una tariffa. I periodi successivi arrivano con gli aggiornamenti dell'integrazione.
- **Solo clienti domestici in bassa tensione.** Residenti e non residenti, qualsiasi potenza impegnata.
- **Non ancora applicati:** sconti, energia verde opzionale, prezzi validi per un periodo, scaglioni di consumo, fasce Peak/Off-Peak.
- La **franchigia delle accise** per i residenti fino a 3 kW segue la legge (primi 150 kWh al mese esenti, ridotti oltre la soglia) ma non è ancora stata verificata su una bolletta con il dettaglio.
- **Il PUN si può scaricare dal GME con il tuo account.** Le API ufficiali del GME sono gratuite ma personali: ogni utente si registra e riceve le sue credenziali, che non vanno condivise. Senza account puoi prendere il PUN da qualsiasi entità che hai già, oppure scriverlo a mano.
- Progetto non ufficiale, non affiliato ad ARERA, GME, Acquirente Unico, Terna né ad alcun venditore.

---

## Funzioni

- **Prezzo del prossimo kWh**, IVA inclusa, per la fascia in vigore: pronto per la plancia Energia
- **Costo del mese in corso** e **costo del mese precedente**, calcolati come una bolletta: una riga per ogni componente, ciascuna arrotondata al centesimo, IVA riga per riga
- **Costo fisso giornaliero**: quanto costa la fornitura senza consumi
- **Fasce F1 / F2 / F3** con le festività nazionali italiane, ed energia prelevata nel mese divisa per fascia
- **Uno o più contatori**: gli incrementi di più sensori di energia cumulativi vengono sommati (forniture trifase, più punti di misura)
- **PUN dal GME**: con un account GME la media mensile del PUN per fascia viene scaricata dalle API ufficiali, una volta per tutte le forniture, di solito una volta al giorno
- **Qualsiasi contratto** che rientri nel modello, incollato in JSON e validato subito; riconfigurabile in ogni momento senza perdere i contatori del mese
- Italiano e inglese

## Sensori

Ogni fornitura configurata è un dispositivo con sei sensori.

| Sensore | Cosa mostra | Unità |
|---|---|---|
| Prezzo del prossimo kWh | Costo di un kWh in più adesso, IVA inclusa. Attributi: fascia, vendita, rete, oneri di sistema, accise, IVA | EUR/kWh |
| Costo fisso giornaliero | Quote fisse e di potenza di oggi, IVA inclusa | EUR/d |
| Costo del mese in corso | La bolletta dal primo del mese a ora. Attributi: imponibile, IVA, subtotali per sezione | EUR |
| Costo del mese precedente | La bolletta del mese concluso, calcolata al cambio di mese. Mentre aspetta i valori di mercato di quel mese, l'attributo `waiting_for_market` è `true` | EUR |
| Energia prelevata nel mese | kWh prelevati nel mese. Attributi: kWh in F1, F2, F3 | kWh |
| Fascia oraria | La fascia in vigore: F1, F2 o F3 | — |

Quando un valore non è calcolabile lo stato è sconosciuto e l'attributo `problems` dice che cosa manca.

L'account GME è un dispositivo con dieci sensori: il PUN medio di F0 (monorario), F1, F2, F3 e F23, del mese in corso e del mese precedente, in EUR/kWh. Gli attributi dicono a quale mese (`month`) si riferisce il valore, quante ore copre (`hours`) su quelle del mese (`expected_hours`) e se è definitivo (`complete`). Nei primi giorni del mese una fascia può non avere ancora ore (di domenica non c'è F1): finché non ne ha, il sensore del mese in corso mostra il valore del mese precedente, e l'attributo `month` lo indica.

---

## Installazione

### Con HACS (consigliata)

1. In HACS apri il menu, scegli **Repository personalizzati**, aggiungi `https://github.com/giorgioparri/electr-it-ha` con tipo **Integrazione**
2. Cerca **electrITHA**, installa, poi riavvia Home Assistant

### Manuale

1. Scarica `electr_it_ha.zip` dall'ultima [release](https://github.com/giorgioparri/electr-it-ha/releases/latest)
2. Estrailo in `/config/custom_components/electr_it_ha/`
3. Riavvia Home Assistant

Richiede Home Assistant 2026.8.0 o successivo.

---

## Configurazione

**Impostazioni → Dispositivi e servizi → Aggiungi integrazione → electrITHA.** La prima volta scegli cosa aggiungere: una fornitura o l'account GME. Quando l'account GME esiste, l'integrazione passa direttamente a una nuova fornitura.

Una fornitura richiede tre schermate:

1. **Contratto di fornitura**: incolla il contratto in JSON (vedi sotto). Viene validato subito e ogni problema è elencato.
2. **Fornitura e contatori**: se sei residente, la potenza impegnata in kW, e uno o più sensori di energia cumulativi che misurano il prelievo dalla rete (kWh, Wh o MWh).
3. **Valori di mercato**: solo quelli da cui dipende il prezzo del tuo contratto. Per ciascuno scegli un'entità oppure scrivi un valore fisso in €/kWh. Un'entità può essere in €/kWh o in €/MWh. Se hai aggiunto l'account GME, i suoi sensori del PUN sono già indicati. La schermata viene saltata se il contratto non ne richiede.

Per cambiare qualcosa in seguito usa **Riconfigura** sulla voce dell'integrazione: si riaprono le stesse schermate con i valori attuali.

### L'account GME

1. Registrati alle API del GME su [https://api.mercatoelettrico.org/users/RegistrationForm/RegistrationRequest](https://api.mercatoelettrico.org/users/RegistrationForm/RegistrationRequest) e segui le istruzioni del GME per accettare le condizioni d'uso. Il GME ti invia poi nome utente e password.
2. Aggiungi di nuovo l'integrazione e scegli **L'account GME**. Le credenziali vengono controllate facendo il login.
3. Aggiungi le forniture: i campi del PUN sono già compilati con i sensori dell'account GME. Per una fornitura configurata prima, apri **Riconfigura** e sceglili al posto dei valori che ha.

La prima volta l'integrazione scarica il PUN del mese precedente e del mese in corso fino a oggi; poi solo i giorni che mancano, di solito una volta al giorno dopo le 14, quando il GME ha pubblicato i prezzi di domani. I prezzi restano nel tuo Home Assistant. Se in seguito il GME rifiuta le credenziali, Home Assistant ti chiede di inserirle di nuovo.

Le condizioni d'uso delle API del GME consentono solo l'uso personale e chiedono di citare la fonte: ogni sensore dell'account GME riporta l'attribuzione *Source: Gestore dei Mercati Energetici S.p.A.*

### Il contratto

Il contratto contiene solo ciò che decide il venditore. Tutto ciò che è regolato lo fornisce l'integrazione.

```json
{
  "schema_version": 1,
  "supplier": "Il mio venditore",
  "offer_name": "La mia offerta",
  "offer_code": null,
  "price_type": "indexed",
  "time_bands": "f1_f2_f3",
  "index": {"name": "pun_index_gme", "period": "monthly", "coefficient": 1.10, "loss_method": "price"},
  "energy_price": {"f1": 0.0286, "f2": 0.0286, "f3": 0.011},
  "dispatching": {"mode": "cdispd", "fixed_value": null, "capacity_market": false},
  "disp_bt": true,
  "fixed_fee_eur_year": 144,
  "energy_fee_eur_kwh": null,
  "green_energy": null,
  "discounts": [],
  "notes": []
}
```

| Campo | Significato |
|---|---|
| `price_type` | `indexed` (PUN più spread), `fixed` (prezzo fisso) o `regulated` (servizio di tutela) |
| `time_bands` | `mono`, `f1_f23` o `f1_f2_f3` |
| `index` | Solo per `indexed`. `coefficient` moltiplica il PUN per tenere conto delle perdite di rete: `1.10` quando il contratto dice che il PUN è maggiorato delle perdite (λ = 10%), `1.0` quando non lo è |
| `index.loss_method` | Come compaiono le perdite in bolletta: `energy` (predefinito) aggiunge righe separate di kWh di perdita, arrotondati al kWh per fascia; `price` fattura il PUN già moltiplicato per il coefficiente. Se in bolletta il PUN è molto più alto della media GME, è `price` |
| `energy_price` | €/kWh per fascia, perdite incluse, con chiavi `f0`, oppure `f1` e `f23`, oppure `f1`, `f2`, `f3`. Per `indexed` è lo spread; per `fixed` il prezzo pieno; assente per `regulated` |
| `dispatching.mode` | `cdispd` (il CDispD mensile, passante), `regulated` (dispacciamento fatturato su kWh più perdite), `fixed` (un valore deciso dal venditore, in `fixed_value`), `included` (compreso nel prezzo dell'energia), `tutela` (servizio di tutela) |
| `dispatching.capacity_market` | `true` se il capacity market è una voce a parte |
| `disp_bt` | `true` se viene fatturata la componente DispBT |
| `fixed_fee_eur_year` | La quota fissa del venditore, in € all'anno |
| `energy_fee_eur_kwh` | Una quota del venditore per kWh, in aggiunta al prezzo dell'energia |
| `green_energy` | `{"eur_kwh": …, "eur_year": …, "optional": …}` oppure `null` |
| `discounts`, `notes` | Conservati e mostrati; gli sconti non sono ancora applicati |

Il modello ricalca il tracciato con cui i venditori pubblicano le offerte sul Portale Offerte di ARERA, quindi qualsiasi offerta in vendita si può descrivere così.

### Valori di mercato

| Nome | Serve quando |
|---|---|
| PUN per fascia | `price_type` è `indexed`: la media mensile del PUN Index GME per ogni fascia del contratto, come la pubblica il GME — non moltiplicata per le perdite, che applica il contratto |
| Dispacciamento | `dispatching.mode` è `regulated` |
| Capacity market | `dispatching.capacity_market` è `true` |
| CDispD | `dispatching.mode` è `cdispd` |

Cambiano ogni mese. Per il PUN la fonte più semplice è l'account GME. Altrimenti collega ciascun valore a un'entità che lo ha già nel tuo Home Assistant (qualsiasi sensore con la media mensile per fascia, in €/kWh o €/MWh), oppure scrivi il valore dell'ultima bolletta e aggiornalo quando cambia. Dispacciamento, capacity market e CDispD non vengono ancora scaricati. Alcune bollette riportano il PUN già moltiplicato per 1,10: in quel caso dividilo per 1,10 prima di inserirlo, altrimenti le perdite vengono contate due volte.

Al cambio di mese, il mese concluso viene fatturato con i valori di quel mese. Un'entità che indica il suo mese (`month`), come i sensori dell'account GME, viene usata solo quando si riferisce a quel mese ed è definitiva; per un sensore del mese in corso si usa il suo gemello del mese precedente. Le altre entità e i valori fissi vengono usati come sono in quel momento. Se dopo tre giorni i valori giusti mancano ancora, il mese viene fatturato con quelli disponibili e il log lo segnala.

### Plancia Energia

In **Impostazioni → Plance → Energia**, sulla fonte di consumo dalla rete scegli **Usa un'entità con il prezzo corrente** e seleziona *Prezzo del prossimo kWh*. Le quote fisse non fanno parte della plancia Energia: sono in *Costo fisso giornaliero* e in *Costo del mese in corso*.

---

## Come viene calcolato il costo

Il calcolatore ricostruisce una bolletta: ogni componente è una riga, ogni riga è arrotondata al centesimo, l'IVA è calcolata riga per riga. Le componenti regolate per kWh sono fatturate in un'unica riga finché la tariffa non cambia; le quote fisse e di potenza a mese; le accise mese per mese, solo sui kWh misurati.

È verificato su bollette vere:

| Bolletta | Risultato |
|---|---|
| Indicizzata al PUN per fascia, perdite 10%, dispacciamento regolato e capacity market, 6 kW | Imponibile, IVA e totale tornano al centesimo |
| Indicizzata al PUN con le perdite nel prezzo, più spread e CDispD, due mesi a cavallo di un cambio di tariffa, 5 kW | Ogni sezione, IVA e totale tornano al centesimo |
| Servizio di tutela, bioraria, 3 kW, solo riepilogo | Ogni sezione entro un centesimo; accise non verificate |

L'energia tra due letture di un contatore va alla fascia in vigore quando arriva la seconda lettura: con un contatore che aggiorna ogni pochi minuti l'errore è qualche minuto di consumo a ogni cambio di fascia.

---

## Changelog

Vedi [CHANGELOG.md](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/CHANGELOG.md) per la storia completa delle versioni.

---

## Licenza

GPL-3.0-or-later — vedi [LICENSE](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/LICENSE)

## Avvertenza

Questa è un'integrazione non ufficiale. I valori che mostra sono stime, utili a capire i propri consumi: l'unico importo dovuto è quello della bolletta del venditore. I valori regolati sono trascritti dalle pubblicazioni di ARERA; i corrispettivi approvati dall'Autorità risultano esclusivamente dai suoi provvedimenti. Dati del PUN scaricati con un account GME: fonte Gestore dei Mercati Energetici S.p.A.

## Ringraziamenti

Realizzata con l'assistenza di [Claude](https://claude.ai) di Anthropic.
