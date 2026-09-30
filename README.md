# Automatische Nederlandse ondertitels voor Kodi

Deze Kodi-service zoekt automatisch een Engelse SRT via OpenSubtitles, vraagt
eerst om bevestiging en vertaalt daarna naar natuurlijk Nederlands. De vertaling draait via
Codex op de Radxa; Kodi blijft tijdens het vertalen gewoon afspelen en laadt de
Nederlandse SRT zodra die klaar is.

## Wat gebeurt er?

1. Ongeveer vijf seconden na de start van een film of aflevering zoekt de add-on
   via de officiële OpenSubtitles.com-add-on een Engelse ondertitel. De eerste
   bruikbare Engelse match in de volgorde van die add-on wordt gedownload.
   Als er al een Nederlandse ondertitel actief is of een vertaling voor deze
   video loopt, wordt de automatische zoekactie overgeslagen.
2. De add-on toont de videotitel, bron en een korte tekstvoorvertoning.
3. Alleen na **Ja** wordt een tijdelijke Kodi-SRT onder een veilige, unieke
   naam naar de SMB-map gekopieerd en schrijft Kodi een bevestigde
   vertaalopdracht.
4. De Radxa vertaalt de dialoog met GPT-5.6 Luna. Als Luna tijdelijk vol is,
   wordt GPT-5.6 Terra geprobeerd.
5. De originele cue-nummers, tijdcodes, HTML/ASS-tags, witruimte en
   regelafbrekingen worden lokaal bewaard en niet door het model herschreven.
6. Kodi laadt de Nederlandse SRT alleen wanneer dezelfde video nog speelt.

De afspeel-URL wordt nooit in de gedeelde map opgeslagen. Kodi bewaart lokaal
alleen een SHA-256-vingerafdruk om te controleren of dezelfde video nog speelt.
Voor zoeken gebruikt de officiële OpenSubtitles-add-on zelf de video-informatie;
deze koppeling kopieert of leest geen OpenSubtitles-inloggegevens.

## Kodi installeren

1. Download `service.autosubtranslate.nl-0.3.0.zip` bij
   [GitHub Releases](https://github.com/cracknek22/kodi-auto-subtitle-nl/releases).
2. Zet in Kodi zo nodig **Instellingen → Systeem → Add-ons → Onbekende
   bronnen** aan.
3. Kies **Add-ons → Installeren van zipbestand** en selecteer de gedownloade
   ZIP.
4. Open **Mijn add-ons → Diensten → Automatische Nederlandse Ondertitels →
   Configureren**.
5. Kies als gecontroleerde map:
   `smb://192.168.2.60/share/subtitles/`
6. Installeer/activeer de officiële **OpenSubtitles.com**-add-on
   (`service.subtitles.opensubtitles-com`) vanuit Kodi's add-onrepository en
   log daarin zelf in. De bestaande aanmelding wordt gebruikt.
7. Laat **Automatisch Engelse ondertitels zoeken via OpenSubtitles** aan staan
   (standaard aan). Herstart Kodi na het bijwerken van de ZIP.

Voor automatisch ophalen hoeft Kodi's eigen downloadmap niet op SMB te staan:
deze add-on kopieert de opgehaalde SRT na jouw bevestiging zelf naar de server.
Voor handmatige downloads kun je bij **Instellingen → Speler → Taal** de
**Aangepaste ondertitelmap** wel op dezelfde SMB-map zetten.

Gebruik bij het toevoegen van de SMB-bron je eigen Samba-gebruikersnaam en
wachtwoord; deze staan niet in de add-on of in deze repository.

## SMB-wachtwoord wijzigen

De Radxa bewaart het Samba-wachtwoord in een afzonderlijk bestand met rechten
`600`; het staat niet in `.env`, Compose-argumenten of deze repository. Kies
het wachtwoord interactief via SSH:

```text
/home/radxa/smb-stack/change-smb-password.sh
```

Het wachtwoord moet 16 tot en met 64 tekens bevatten. Alle afdrukbare
ASCII-leestekens zijn toegestaan; spaties en regeleinden niet. Het hulpmiddel
test de nieuwe aanmelding en controleert dat het oude wachtwoord is geweigerd.
Bij een fout wordt de vorige secret automatisch teruggezet en getest.

## Gebruik

Start een film of aflevering. Wacht op de automatische OpenSubtitles-zoekactie
en controleer het voorbeeld in de bevestigingspopup. Kies **Nee** bij een
verkeerde ondertitel en **Ja** om te vertalen. Bij Nee wordt geen vertaalopdracht
gemaakt en niets naar SMB gekopieerd. Er is maximaal één automatische poging
per afspeelbeurt; fouten en Nee starten geen herhaalde downloads.

De Engelse download gebeurt vóór het voorbeeld en telt dus mee voor je
OpenSubtitles-downloadlimiet, ook bij Nee. Er is geen extra API-sleutel voor
deze koppeling nodig. De limieten en voorwaarden van je OpenSubtitles-account
blijven gelden.

Handmatig een andere Engelse ondertitel downloaden blijft mogelijk. Zodra
een nieuwe SRT stabiel in de gecontroleerde SMB-map of Kodi's tijdelijke map
staat, verschijnt de bestaande bevestigingspopup. Bij meerdere nieuwe SRT's
laat de add-on eerst kiezen welk bestand bedoeld is. Je kunt automatisch
zoeken in de instellingen uitschakelen zonder deze handmatige route te verliezen.

De vertaler wijzigt de originele tijdcodes niet. Dat behoudt de timing van de
gekozen ondertitel, maar maakt een verkeerde release niet alsnog synchroon.
Bij ontbrekende of onjuiste videometadata kan OpenSubtitles een verkeerde match
vinden; kies dan Nee en zoek handmatig een passende versie.

## Belangrijke Kodi-beperking

Kodi geeft add-ons niet het bestandspad van de actieve ondertitelstream.
De handmatige route controleert daarom zowel de SMB-map als Kodi's tijdelijke map. Dit werkt
voor **nieuwe externe SRT-bestanden** die Kodi als bestand beschikbaar maakt,
ook wanneer de originele SRT bij de film hoort. Een ingebedde ondertiteltrack
in een MKV of een stream die nooit als SRT-bestand wordt opgeslagen, kan niet
rechtstreeks worden vertaald. De automatische OpenSubtitles-route kan wel een
losse Engelse SRT ophalen als de film al ingebedde ondertiteltracks heeft.

De providerkoppeling gebruikt Kodi's
[Files.GetDirectory](https://kodi.wiki/view/JSON-RPC_API/v13.5#Files.GetDirectory)
en de zoek-/downloadacties van de
[officiële OpenSubtitles.com-add-on](https://github.com/opensubtitles-dev/service.subtitles.opensubtitles-com).
De lokale tests simuleren Kodi, SMB en providerantwoorden. Een volledige proef
op de Android-box (zoeken, bevestigen, vertalen en laden) blijft daarnaast nodig.

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
bevestigde opdrachten, veilige Kodi-tempkopieën, padvalidatie, gestructureerde
Codex-uitvoer, SMB-uitval en het voorkomen van afspeel-URL's in gedeelde
opdrachten. De OpenSubtitles-tests controleren bovendien Engelse resultaten,
cacheverversing, Nee zonder opdracht, bronwijzigingen en het afbreken bij
een andere of opnieuw gestarte video.
