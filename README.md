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
4. Met de optionele, beveiligd gekoppelde synchronisatie vergelijkt de Radxa
   eerst de SRT met de filmaudio via ffsubsync. De oorspronkelijke SRT blijft
   onaangeroerd; alleen een tijdelijke kopie krijgt gecorrigeerde tijdcodes.
5. De Radxa vertaalt de dialoog met GPT-5.6 Luna. Als Luna tijdelijk vol is,
   wordt GPT-5.6 Terra geprobeerd.
6. Cue-nummers, tijdcodes (eventueel gecorrigeerd), HTML/ASS-tags, witruimte en
   regelafbrekingen worden lokaal bewaard en niet door het model herschreven.
7. Kodi laadt de Nederlandse SRT alleen wanneer dezelfde video nog speelt.

De afspeel-URL wordt nooit in de gedeelde map opgeslagen. Kodi bewaart lokaal
alleen een SHA-256-vingerafdruk om te controleren of dezelfde video nog speelt.
Voor zoeken gebruikt de officiële OpenSubtitles-add-on zelf de video-informatie;
deze koppeling kopieert of leest geen OpenSubtitles-inloggegevens.

## Kodi installeren

1. Download `service.autosubtranslate.nl-0.4.0.zip` bij
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

Zonder synchronisatie wijzigt de vertaler de tijdcodes niet. Met synchronisatie
past ffsubsync de timing vóór het vertalen aan; het model zelf verandert nooit
tijdcodes. Verschillen in begintijd of afspeelsnelheid kunnen zo worden
gecorrigeerd, maar een afwijkende montage of verkeerde aflevering kan niet
gegarandeerd passend worden gemaakt. De controle gebruikt spraakactiviteit,
geen begrip van de gesproken tekst. Bij een mislukte synchronisatie stopt de hele opdracht; er
wordt niet stilzwijgend zonder synchronisatie vertaald.
Bij ontbrekende of onjuiste videometadata kan OpenSubtitles een verkeerde match
vinden; kies dan Nee en zoek handmatig een passende versie.

## Synchronisatie veilig koppelen (v0.4.0)

Synchronisatie staat in de add-on standaard **uit** totdat de server is gekoppeld.
Stel eerst de Radxa in volgens [de installatiehandleiding](deploy/SYNC.md).
Open daarna de add-oninstellingen → **Ondertitels synchroniseren**:

1. Vul het serveradres in, bijvoorbeeld `https://192.168.2.60:8766`.
2. Vul de SHA-256-certificaatvingerafdruk en het privétoegangstoken van de Radxa
   in. Deel het token niet via GitHub, chats of de SMB-map. De invoer is verborgen,
   maar Kodi bewaart dit in zijn lokale instellingen: behandel backups daarvan
   dus als vertrouwelijk.
3. Zet **Eerst synchroniseren met de filmaudio, daarna vertalen** aan.

Deze versie ondersteunt directe HTTP(S)-mediabestanden van expliciet toegestane
publieke hosts, bijvoorbeeld een door Kodi opgeloste Real-Debrid-link. Een
`plugin://`-link, SMB/lokaal videobestand, HLS/DASH-playlist of onbekende
mediahost wordt niet automatisch omgezet of geaccepteerd. Synchronisatie kan
mediadata downloaden; bij een videobestand is dat niet gegarandeerd alleen audio.
Er wordt geen volledige film als bestand op de gedeelde map opgeslagen.

Kodi verstuurt de videolink en eventuele afspeelheaders pas na jouw bevestiging,
via HTTPS met een vooraf gecontroleerde certificaatvingerafdruk. De Radxa
bewaart deze tijdelijk in geheugen, gekoppeld aan precies de goedgekeurde SRT.
De SMB-opdracht bevat alleen bestandsnaam, opdrachtnummer en bestandshash.

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
`deploy/subtitle-translator.service`. Deze begrenst alleen vertalen op 512 MB RAM
en maximaal één CPU-kern. De optionele synchronisatie-instelling verhoogt de
geheugengrens naar 1 GB en behoudt die CPU-grens. Dit zijn maxima, geen continu
gereserveerd geheugen. ffsubsync gebruikt lokaal CPU voor audioanalyse;
de taalmodelberekening blijft in de cloud.

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

De synchronisatietests controleren tevens HTTPS-pinning vóór tokenoverdracht,
eenmalige referenties, publieke mediahosts, bronhashes, veilige redirects,
beperkte FFmpeg-protocollen/formaten en stoppen bij synchronisatiefouten.
Met de optionele `ffsubsync==0.5.1`-afhankelijkheid testen
`tests/test_sparse_sync.py` de audiosteekproeven: niet-beluisterde stukken
krijgen geen gewicht in de vergelijking, terwijl daadwerkelijk gemeten stilte
stilte blijft. De worker corrigeert hiervoor de nulopvulling van versie 0.5.1;
de kwaliteitsgrenzen blijven ongewijzigd. Zonder bruikbare spraak stopt de test.
De optionele echte audiotest staat in `tests/test_sync_audio_integration.py`:
24 zelf gegenereerde zinnen met een opzettelijke vertraging van 3 seconden.
De mediaproxy en FFmpeg/ffsubsync zijn daarbij echt; de vertaaluitvoer is een
testvervanger. Dit bewijst nog niet de werking met jouw Kodi-box of elke film.
