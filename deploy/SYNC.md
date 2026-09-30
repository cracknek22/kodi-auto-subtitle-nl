# ffsubsync op de Radxa

Dit is een optionele uitbreiding op de bestaande vertaalservice. De Kodi-ZIP
bevat alleen de koppeling; ffsubsync draait op de Radxa. Bestaande modellen en
de originele SRT blijven behouden. Werk Kodi pas bij wanneer de box vrij is.

## Voorbereiding

- Maak een herstelkopie van de bestaande Python-bestanden en user-service.
- Wacht tot er geen vertaalopdracht actief is voordat je de service herstart.
- Plaats naast `subtitle_translator.py` ook `subtitle_sync.py`,
  `ffsubsync_worker.py`, `sync_reference.py` en `media_proxy.py`.
- Python 3.11+, OpenSSL, FFmpeg en ffprobe moeten beschikbaar zijn.
- Gebruik een aparte Python-omgeving. Er zijn geen Ollama-, Gemma-, Torch- of
  GPU/NPU-pakketten nodig.

```sh
python3 -m venv /home/radxa/.local/share/subtitle-translator/sync-venv
/home/radxa/.local/share/subtitle-translator/sync-venv/bin/python -m pip install -r requirements-sync.txt
```

`requirements-sync.txt` legt ffsubsync vast op 0.5.1. Haal FFmpeg via een
vertrouwde pakketbron of de links op [ffmpeg.org](https://ffmpeg.org/download.html),
kies ARM64 voor de Radxa en verifieer de download. FFmpeg en ffprobe moeten in
dezelfde map staan; geef die map hieronder mee. De andere packages en diensten
op de Radxa hoeven niet te veranderen.

## Privéconfiguratie

Voer vanuit de repository uit (pas het lokale adres en FFmpeg-pad aan):

```sh
python3 deploy/setup-sync.py \
  --bind 192.168.2.60 \
  --media-host '*.real-debrid.com' \
  --unit-dir /home/radxa/.config/systemd/user/subtitle-translator.service.d \
  --python /home/radxa/.local/share/subtitle-translator/sync-venv/bin/python \
  --ffmpeg-dir /pad/naar/ffmpeg/bin
systemctl --user daemon-reload
systemctl --user restart subtitle-translator.service
systemctl --user is-active subtitle-translator.service
```

De hostlijst beperkt welke publieke mediabronnen mogen worden opgehaald. Voeg
alleen een specifieke extra provider toe als je die echt gebruikt. Private
IP-adressen en redirects daarheen blijven geblokkeerd. Stel geen algemene `*`
in. Dit is geen omzeiling van de beschikbaarheids- of toegangsregels van een
mediaprovider.

De helper maakt `~/.config/subtitle-translator/sync/` met rechten 700 en privé-
bestanden met rechten 600. Hij behoudt bestaande sleutels en wijzigt geen
modelkeuze. `connection.json` bevat het serveradres, de certificaatvingerafdruk
en het pad naar het token, niet het token zelf. Lees/voer het token alleen in
een vertrouwde lokale sessie in, nooit in gedeelde logs of de repository.

Het drop-inbestand `sync.conf` stelt HTTPS op poort 8766 in, met maximaal 1 GB
geheugen, één BLAS-thread en behoud van de bestaande CPU-grens. Open deze poort
niet publiek op de router; gebruik LAN of een vertrouwde VPN. Een firewall moet
verkeer vanaf de Kodi-box toestaan. Het certificaat is zelfondertekend en wordt
door Kodi aan zijn SHA-256-vingerafdruk herkend. Bij bewust vervangen ervan moet
Kodi opnieuw gekoppeld worden.

## Werking en grenzen

Na bevestiging registreert Kodi een referentie via `/api/v1/references`. Die is
één uur geldig, eenmalig bruikbaar en gekoppeld aan bronnaam, opdrachtnummer en
SHA-256 van de bronbytes. De gedeelde versie-2-opdracht verplicht synchronisatie.
Ontbrekende, verlopen of gewijzigde referenties leiden tot een fout, niet tot
een vertaling met ongecontroleerde timing. Na een serviceherstart vervallen
referenties in geheugen; bevestig zo nodig opnieuw in Kodi.

ffsubsync analyseert meerdere audiofragmenten via een tijdelijke localhost-
mediaproxy. Alleen zelfstandige mediacontainers zijn toegestaan; playlists en
geneste verwijzingen niet. De echte mediaverwijzing verschijnt niet in FFmpeg-
argumenten. De server controleert hosts, DNS-adressen en redirects voordat hij
verbindt. De uitvoer moet dezelfde cue-inhoud en volgorde houden, met beperkte
offset en tijdschaalcorrectie. De berekening stopt na maximaal vijf minuten.

Dit corrigeert verschuiving en beperkte drift, maar bewijst geen perfecte
spraak-/tekstmatch. Controleer bij de eerste echte film een fragment aan het
begin, midden en einde. De originele SRT wordt nooit overschreven.

## Testen en herstel

```sh
python3 -m unittest discover -s tests -v
```

De echte bibliotheektest vereist de sync-venv en
`RUN_FFSUBSYNC_INTEGRATION=1`. Voor een werkelijke audioanalyse volg je de
instructies bovenaan `tests/test_sync_audio_integration.py`. Die test gebruikt
zelf gegenereerde spraak, geen film of betaald vertaalverzoek.

Bij terugdraaien: zet synchronisatie uit in Kodi, wacht op een vrije service,
verplaats alleen het nieuwe `sync.conf` buiten de drop-inmap en herstel de
eigen codebackup. Voer daarna `daemon-reload` en een serviceherstart uit. Laat
de oorspronkelijke ondertitelbestanden, modellen en SMB-configuratie intact.
