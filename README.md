# Automatische Nederlandse ondertitels voor Kodi

Deze Kodi-service vraagt eerst om bevestiging en vertaalt daarna een gekozen
Engelse SRT automatisch naar natuurlijk Nederlands. De vertaling draait via
Codex op de Radxa; Kodi blijft tijdens het vertalen gewoon afspelen en laadt de
Nederlandse SRT zodra die klaar is.

## Wat gebeurt er?

1. Kodi bewaart een nieuw gedownloade Engelse SRT in de ingestelde SMB-map.
2. De add-on toont de bestandsnaam en een korte tekstvoorvertoning.
3. Alleen na **Ja** schrijft Kodi een bevestigde vertaalopdracht.
4. De Radxa vertaalt de dialoog met GPT-5.6 Luna. Als Luna tijdelijk vol is,
   wordt GPT-5.6 Terra geprobeerd.
5. De originele cue-nummers, tijdcodes, HTML/ASS-tags, witruimte en
   regelafbrekingen worden lokaal bewaard en niet door het model herschreven.
6. Kodi laadt de Nederlandse SRT alleen wanneer dezelfde video nog speelt.

De afspeel-URL wordt nooit in de gedeelde map opgeslagen. Kodi bewaart lokaal
alleen een SHA-256-vingerafdruk om te controleren of dezelfde video nog speelt.

## Kodi installeren

1. Download `service.autosubtranslate.nl-0.1.0.zip` bij
   [GitHub Releases](https://github.com/cracknek22/kodi-auto-subtitle-nl/releases).
2. Zet in Kodi zo nodig **Instellingen → Systeem → Add-ons → Onbekende
   bronnen** aan.
3. Kies **Add-ons → Installeren van zipbestand** en selecteer de gedownloade
   ZIP.
4. Open **Mijn add-ons → Diensten → Automatische Nederlandse Ondertitels →
   Configureren**.
5. Kies als gecontroleerde map:
   `smb://192.168.2.60/share/subtitles/`
6. Stel in **Instellingen → Speler → Taal** de opslaglocatie voor gedownloade
   ondertitels in op **Aangepaste ondertitelmap** en kies exact dezelfde
   SMB-map.

Gebruik bij het toevoegen van de SMB-bron je eigen Samba-gebruikersnaam en
wachtwoord; deze staan niet in de add-on of in deze repository.

## Gebruik

Start een film of aflevering en download/kies een Engelse externe ondertitel.
Zodra de SRT stabiel in de gecontroleerde map staat, verschijnt de
bevestigingspopup. Kies **Nee** bij een verkeerde ondertitel en **Ja** om te
vertalen. Bij meerdere nieuwe SRT's laat de add-on eerst kiezen welk bestand
bedoeld is.

## Belangrijke Kodi-beperking

Kodi geeft add-ons niet het bestandspad van de actieve ondertitelstream. Daarom
werkt de automatische popup betrouwbaar voor **nieuwe externe SRT-bestanden**
die Kodi in de gecontroleerde map opslaat. Een al bestaand bestand dat niet
opnieuw wordt opgeslagen en een ingebedde ondertiteltrack in een videobestand
kunnen niet automatisch aan een bronbestand worden gekoppeld.

## Radxa-service

Vereisten:

- Python 3
- een aangekoppelde ondertitelmap op `/mnt/storage/subtitles`
- de officiële Codex CLI
- `codex login status` moet aangeven dat de Radxa met ChatGPT is aangemeld

De meegeleverde user-service staat in
`deploy/subtitle-translator.service`. Deze begrenst de vertaler op 512 MB RAM
en maximaal één CPU-kern. Omdat de modelberekening in de cloud gebeurt, blijft
de normale belasting op de Radxa zeer laag.

Voor de huidige Radxa-installatie (`radxa` als gebruiker) zijn de
installatiestappen:

```text
git clone https://github.com/cracknek22/kodi-auto-subtitle-nl.git /home/radxa/subtitle-translator
codex login
codex login status
mkdir -p /home/radxa/.config/systemd/user
cp /home/radxa/subtitle-translator/deploy/subtitle-translator.service /home/radxa/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now subtitle-translator.service
systemctl --user status subtitle-translator.service
```

Voer eenmalig `sudo loginctl enable-linger radxa` uit als de service ook moet
blijven draaien wanneer `radxa` niet via SSH is aangemeld. Gebruik je een
andere gebruikersnaam, Codex-locatie of opslagmap, pas dan eerst
`WorkingDirectory`, `ExecStart`, `CODEX_BINARY`, `CODEX_WORK_DIR` en
`WATCH_DIR` in het servicebestand aan. De officiële installatie-instructies
voor de Codex CLI staan op
[developers.openai.com/codex/cli](https://developers.openai.com/codex/cli/).

## Privacy en gebruik

De dialoogtekst wordt voor vertaling naar OpenAI gestuurd. Tijdcodes,
ondertitelopmaak en de video- of Real-Debrid-URL worden niet meegestuurd.
ChatGPT-login gebruikt de Codex-gebruikslimiet van het aangemelde account.
Dit is daarom niet volledig offline en niet gegarandeerd onbeperkt. Zie de
[officiële Codex-informatie](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan).

## Testen

```text
python3 -m unittest discover -s tests -v
```

De tests controleren onder meer exacte tijdcode- en opmaakbewaring,
bevestigde opdrachten, padvalidatie, gestructureerde Codex-uitvoer,
SMB-uitval en het voorkomen van afspeel-URL's in gedeelde opdrachten.
