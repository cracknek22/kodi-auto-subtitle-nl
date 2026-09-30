from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_NAME = ADDON.getAddonInfo("name")
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("path"))
PROFILE_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
LIB_PATH = os.path.join(ADDON_PATH, "resources", "lib")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

from core import (  # noqa: E402
    build_request,
    first_dialogue,
    is_dutch_subtitle,
    request_path,
    staged_subtitle_name,
    stable_candidates,
    status_path,
    validate_completed_status,
    video_fingerprint,
)
import opensubs  # noqa: E402
import sync_client  # noqa: E402


MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
JOBS_PATH = os.path.join(PROFILE_PATH, "jobs.json")
TRANSLATED_PATH = os.path.join(PROFILE_PATH, "translated")
TEMP_PATH = xbmcvfs.translatePath("special://temp/").rstrip("/").rstrip("\\") + "/"


class PlaybackPlayer(xbmc.Player):
    """Invalidate in-flight searches even if the same video is restarted."""

    playback_generation = 0

    def onAVStarted(self):
        self.playback_generation += 1

    def onPlayBackStopped(self):
        self.playback_generation += 1

    def onPlayBackEnded(self):
        self.playback_generation += 1

    def onPlayBackError(self):
        self.playback_generation += 1


def log(message: str, level: int = xbmc.LOGINFO) -> None:
    xbmc.log(f"[{ADDON_ID}] {message}", level)


def join_vfs(folder: str, name: str) -> str:
    cleaned = folder.rstrip("/\\")
    return f"{cleaned}/{name}"


def parent_vfs(path: str) -> str:
    return f"{path.rsplit('/', 1)[0]}/"


def read_text(path: str) -> str:
    handle = xbmcvfs.File(path, "r")
    try:
        content = handle.read()
    finally:
        handle.close()
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return content[1:] if content.startswith("\ufeff") else content


def read_subtitle_bytes(path: str) -> bytes:
    """Read the same raw bytes the Radxa hashes, including BOM and CRLF."""
    if xbmcvfs.Stat(path).st_size() > MAX_SUBTITLE_BYTES:
        raise ValueError("Ondertitel is groter dan 2 MB.")
    handle = xbmcvfs.File(path, "r")
    try:
        raw = bytes(handle.readBytes(MAX_SUBTITLE_BYTES + 1))
    finally:
        handle.close()
    if not raw or len(raw) > MAX_SUBTITLE_BYTES:
        raise ValueError("De ondertitel is leeg of groter dan 2 MB.")
    return raw


def subtitle_sha256(path: str) -> str:
    return hashlib.sha256(read_subtitle_bytes(path)).hexdigest()


def write_text(path: str, content: str) -> None:
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    handle = xbmcvfs.File(temporary, "w")
    try:
        handle.write(content)
    finally:
        handle.close()

    if xbmcvfs.exists(path):
        xbmcvfs.delete(path)
    if not xbmcvfs.rename(temporary, path):
        xbmcvfs.delete(temporary)
        raise OSError(f"Kon bestand niet opslaan: {path}")


def read_json(path: str) -> dict | None:
    if not xbmcvfs.exists(path):
        return None
    try:
        payload = json.loads(read_text(path))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: str, payload: dict) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def configured_folder() -> str:
    try:
        folder = ADDON.getSettingString("subtitle_folder")
    except AttributeError:
        folder = ADDON.getSetting("subtitle_folder")
    folder = folder.strip() or "smb://192.168.2.60/share/subtitles/"
    cleaned = folder.rstrip("/\\")
    return f"{cleaned}/"


def _snapshot_folder(folder: str) -> dict[str, tuple[int, int]]:
    try:
        if not xbmcvfs.exists(folder):
            raise OSError("De ingestelde ondertitelmap is niet bereikbaar.")
        _directories, files = xbmcvfs.listdir(folder)
    except RuntimeError as exc:
        raise OSError(
            "De ingestelde ondertitelmap is tijdelijk niet bereikbaar."
        ) from exc
    result: dict[str, tuple[int, int]] = {}
    for name in files:
        if not name.casefold().endswith(".srt"):
            continue
        path = join_vfs(folder, name)
        try:
            stat = xbmcvfs.Stat(path)
            result[path] = (int(stat.st_size()), int(stat.st_mtime()))
        except (OSError, RuntimeError):
            log(f"Kon bestandsinformatie niet lezen: {name}", xbmc.LOGWARNING)
    return result


def snapshot(folder: str) -> dict[str, tuple[int, int]]:
    return _snapshot_folder(folder)


def snapshot_kodi_temp() -> dict[str, tuple[int, int]]:
    try:
        return _snapshot_folder(TEMP_PATH)
    except OSError as exc:
        log(f"Kodi-tempmap niet leesbaar: {safe_label(str(exc))}", xbmc.LOGWARNING)
        return {}


def stage_temp_subtitle(source: str, folder: str) -> str:
    stat = xbmcvfs.Stat(source)
    size = int(stat.st_size())
    if size <= 0:
        raise ValueError("De tijdelijke ondertitel is leeg.")
    if size > MAX_SUBTITLE_BYTES:
        raise ValueError("Ondertitel is groter dan 2 MB.")

    token = uuid.uuid4().hex
    destination = join_vfs(folder, staged_subtitle_name(source, token))
    if xbmcvfs.exists(destination):
        raise OSError("Het veilige ondertitelbestand bestaat al.")

    temporary = f"{destination}.{token}.tmp"
    try:
        if not xbmcvfs.copy(source, temporary):
            raise OSError("Kon de tijdelijke ondertitel niet kopiëren.")
        copied_size = int(xbmcvfs.Stat(temporary).st_size())
        if copied_size <= 0 or copied_size > MAX_SUBTITLE_BYTES:
            raise ValueError("De gekopieerde ondertitel heeft een ongeldige grootte.")
        if not xbmcvfs.rename(temporary, destination):
            raise OSError("Kon de ondertitel niet op de server opslaan.")
    finally:
        xbmcvfs.delete(temporary)

    return destination


def safe_label(text: str, limit: int = 140) -> str:
    cleaned = text.replace("[", "(").replace("]", ")")
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def active_subtitle_name(player: xbmc.Player) -> str:
    try:
        return player.getSubtitles() or "onbekend"
    except RuntimeError:
        return "onbekend"


def current_video(player: xbmc.Player) -> str:
    try:
        return player.getPlayingFile() if player.isPlayingVideo() else ""
    except RuntimeError:
        return ""


def current_video_fingerprint(player: xbmc.Player) -> str:
    return video_fingerprint(current_video(player))


def ask_yes_no(
    filename: str, stream: str, preview: str, video_title: str = "",
) -> bool:
    heading = f"Video: {safe_label(video_title)}\n" if video_title else ""
    source_label = "Gevonden ondertitel" if video_title else "Actieve ondertitel"
    question = (
        "Deze ondertitel eerst met de filmaudio synchroniseren en daarna naar natuurlijk Nederlands vertalen?"
        if ADDON.getSettingBool("sync_enabled")
        else "Deze ondertitel naar natuurlijk Nederlands vertalen?"
    )
    message = (
        f"{heading}Bestand: {safe_label(filename)}\n"
        f"{source_label}: {safe_label(stream)}\n\n"
        f"Voorbeeld: {safe_label(preview)}\n\n"
        f"{question}"
    )
    dialog = xbmcgui.Dialog()
    try:
        return dialog.yesno(
            "Ondertitel vertalen?",
            message,
            nolabel="Nee",
            yeslabel="Ja",
            defaultbutton=xbmcgui.DLG_YESNO_NO_BTN,
        )
    except (AttributeError, TypeError):
        return dialog.yesno(
            "Ondertitel vertalen?",
            message,
            nolabel="Nee",
            yeslabel="Ja",
        )


def choose_candidate(candidates: list[str]) -> str | None:
    if len(candidates) == 1:
        return candidates[0]

    labels: list[str] = []
    usable: list[str] = []
    for path in candidates:
        try:
            preview = first_dialogue(read_text(path), max_length=70)
        except (OSError, UnicodeError):
            continue
        labels.append(f"{path.rsplit('/', 1)[-1]} — {safe_label(preview, 70)}")
        usable.append(path)

    if not usable:
        return None
    choice = xbmcgui.Dialog().select("Kies de Engelse ondertitel", labels)
    return usable[choice] if 0 <= choice < len(usable) else None


def playback_identity(player: xbmc.Player) -> tuple[str, int]:
    return current_video_fingerprint(player), getattr(player, "playback_generation", 0)


def notify_opensubs(message: str) -> None:
    xbmcgui.Dialog().notification(
        ADDON_NAME, message, xbmcgui.NOTIFICATION_INFO, 7000,
    )


def search_opensubs(player, folder, jobs, baseline) -> None:
    """Let the official provider own login/search/download; only submit after Yes."""
    identity = playback_identity(player)
    if not identity[0]:
        return

    def still_playing():
        # Synchronous JSON-RPC releases Kodi's GIL but does not dispatch this
        # service's queued Player callbacks. Drain them before comparing.
        xbmc.sleep(1)
        return playback_identity(player) == identity

    staged = None
    submitted = False
    try:
        if not xbmc.getCondVisibility(f"System.AddonIsEnabled({opensubs.PROVIDER_ID})"):
            notify_opensubs("Installeer en activeer eerst de officiële OpenSubtitles.com-add-on.")
            return
        provider = xbmcaddon.Addon(opensubs.PROVIDER_ID)
        provider_temp = join_vfs(provider.getAddonInfo("profile"), "temp")
        title = xbmc.getInfoLabel("VideoPlayer.Title") or "Huidige video"
        notify_opensubs("Engelse ondertitel zoeken via OpenSubtitles…")
        entries = opensubs.directory_files(xbmc.executeJSONRPC, opensubs.search_url())
        if not still_playing():
            return
        download = opensubs.download_url(entries)
        if not download:
            notify_opensubs("Geen Engelse ondertitel gevonden. Je kunt zelf een andere zoeken.")
            return
        entries = opensubs.directory_files(xbmc.executeJSONRPC, download)
        if not still_playing():
            return
        source = opensubs.subtitle_path(entries, provider_temp, xbmcvfs.translatePath)
        if not source:
            notify_opensubs("Geen bruikbare SRT ontvangen. Controleer OpenSubtitles en je downloadlimiet.")
            return
        size = int(xbmcvfs.Stat(source).st_size())
        if not 0 < size <= MAX_SUBTITLE_BYTES:
            raise ValueError("invalid subtitle size")
        raw = read_subtitle_bytes(source)
        approved_sha256 = hashlib.sha256(raw).hexdigest()
        content = raw.decode("utf-8-sig")
        if not re.search(
            r"(?m)^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", content,
        ):
            raise ValueError("invalid SRT")
        preview = first_dialogue(content)
        if not still_playing() or not ask_yes_no(
            source.rsplit("/", 1)[-1], "Engels — OpenSubtitles.com", preview, title,
        ):
            return
        if not still_playing():
            return
        # The provider can clean/replace its temp files during a manual download.
        # Never translate different content from the preview the user approved.
        if subtitle_sha256(source) != approved_sha256:
            raise ValueError("subtitle changed during confirmation")
        staged = stage_temp_subtitle(source, folder)
        stat = xbmcvfs.Stat(staged)
        baseline[staged] = (int(stat.st_size()), int(stat.st_mtime()))
        if subtitle_sha256(staged) != approved_sha256:
            raise ValueError("subtitle changed during copy")
        if still_playing():
            start_job(
                staged, identity[0], jobs, player=player,
                approved_sha256=approved_sha256, expected_identity=identity,
            )
            submitted = True
    except sync_client.SyncError as exc:
        log("Beveiligde synchronisatie niet gestart", xbmc.LOGWARNING)
        notify_opensubs(str(exc))
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        # VFS/provider exceptions can contain account or stream URLs.
        log(f"OpenSubtitles ophalen mislukt ({type(exc).__name__})", xbmc.LOGWARNING)
        notify_opensubs("Ophalen of opslaan mislukt. Controleer OpenSubtitles en de SMB-map.")
    finally:
        # Only delete our unique copy, never the provider's file. A confirmed
        # request may already exist if persisting the local job failed.
        if staged and not submitted:
            try:
                if not xbmcvfs.exists(request_path(staged)):
                    xbmcvfs.delete(staged)
                    baseline.pop(staged, None)
            except (OSError, RuntimeError):
                log("OpenSubtitles: opruimen uitgesteld wegens SMB-uitval", xbmc.LOGWARNING)


class AutomaticSearch:
    """One attempt per playback, delayed until player metadata has settled."""

    def __init__(self):
        self.identity = None
        self.started_at = 0.0
        self.attempted = False

    def observe(self, player):
        identity = playback_identity(player)
        if identity != self.identity:
            self.identity = identity
            self.started_at = time.monotonic()
            self.attempted = False

    def skip_current(self, player):
        self.observe(player)
        self.attempted = True

    def run_if_due(self, player, folder, jobs, baseline):
        self.observe(player)
        if not self.identity[0] or self.attempted:
            return
        if not ADDON.getSettingBool("opensubs_auto_search"):
            return
        if is_dutch_subtitle(active_subtitle_name(player)) or any(
            job.get("video_fingerprint") == self.identity[0] for job in jobs.values()
        ):
            self.attempted = True
            return
        if time.monotonic() - self.started_at < 5:
            return
        if xbmc.getCondVisibility("Window.IsActive(subtitlesearch)"):
            return
        self.attempted = True
        search_opensubs(player, folder, jobs, baseline)


def load_jobs() -> dict[str, dict]:
    payload = read_json(JOBS_PATH)
    if not payload or not isinstance(payload.get("jobs"), list):
        return {}
    jobs: dict[str, dict] = {}
    for job in payload["jobs"]:
        if (
            isinstance(job, dict)
            and isinstance(job.get("job_id"), str)
            and isinstance(job.get("source"), str)
            and isinstance(job.get("video_fingerprint"), str)
            and len(job["video_fingerprint"]) in {0, 64}
        ):
            jobs[job["job_id"]] = job
    return jobs


def save_jobs(jobs: dict[str, dict]) -> None:
    write_json(JOBS_PATH, {"version": 1, "jobs": list(jobs.values())})


def start_job(
    source: str,
    playing_video_fingerprint: str,
    jobs: dict[str, dict],
    player: xbmc.Player | None = None,
    approved_sha256: str | None = None,
    expected_identity: tuple[str, int] | None = None,
) -> None:
    synchronized = ADDON.getSettingBool("sync_enabled")
    identity = expected_identity or (playback_identity(player) if player is not None else None)

    def validate_approval():
        if player is not None:
            # HTTPS and dialogs can queue Kodi Player callbacks. Process a stop
            # or restart even when getPlayingFile() still returns the same URL.
            xbmc.sleep(1)
            if playback_identity(player) != identity or not identity[0] or identity[0] != playing_video_fingerprint:
                raise sync_client.SyncError("De video is veranderd; bevestig de ondertitel opnieuw.")
        if approved_sha256 is not None and subtitle_sha256(source) != approved_sha256:
            raise sync_client.SyncError("De ondertitel is veranderd; bevestig de ondertitel opnieuw.")

    if synchronized and (player is None or approved_sha256 is None):
        raise sync_client.SyncError("Synchronisatie vereist een bevestigde ondertitel bij de huidige video.")
    validate_approval()
    job_id = f"job_{uuid.uuid4().hex}"
    payload = build_request(source, job_id, source_sha256=approved_sha256 if synchronized else None)
    if synchronized:
        reference_url, reference_headers = sync_client.parse_playback_url(current_video(player))
        sync_client.register_reference(
            ADDON.getSettingString("sync_server"),
            ADDON.getSettingString("sync_cert_sha256"),
            ADDON.getSettingString("sync_token"),
            {
                "job_id": job_id,
                "source": payload["source"],
                "source_sha256": approved_sha256,
                "url": reference_url,
                "headers": reference_headers,
            },
        )
        validate_approval()
    write_json(request_path(source), payload)
    jobs[job_id] = {
        "job_id": job_id,
        "source": source,
        "video_fingerprint": playing_video_fingerprint,
    }
    if synchronized:
        jobs[job_id]["sync_required"] = True
    save_jobs(jobs)
    xbmcgui.Dialog().notification(
        ADDON_NAME,
        "Ondertitel synchroniseren en daarna vertalen; de film blijft spelen." if synchronized else "Vertaling gestart; de film blijft gewoon spelen.",
        xbmcgui.NOTIFICATION_INFO,
        6000,
    )


def output_path(job: dict, status: dict) -> str:
    if job.get("sync_required") and status.get("version") != 2:
        raise ValueError("De vereiste synchronisatie is niet bevestigd door de Radxa.")
    output = validate_completed_status(job["source"], status)
    return join_vfs(parent_vfs(job["source"]), output)


def load_completed_subtitle(
    player: xbmc.Player,
    job: dict,
    status: dict,
) -> None:
    remote = output_path(job, status)
    if not xbmcvfs.exists(remote):
        raise OSError("Het Nederlandse SRT-bestand ontbreekt.")
    if xbmcvfs.Stat(remote).st_size() > MAX_SUBTITLE_BYTES:
        raise ValueError("De vertaalde ondertitel is groter dan 2 MB.")

    xbmcvfs.mkdirs(TRANSLATED_PATH)
    local = os.path.join(TRANSLATED_PATH, f"{job['job_id']}.nl.srt")
    if xbmcvfs.exists(local):
        xbmcvfs.delete(local)
    if not xbmcvfs.copy(remote, local):
        raise OSError("Kon de Nederlandse SRT niet lokaal kopiëren.")

    playing_video_fingerprint = current_video_fingerprint(player)
    if (
        job["video_fingerprint"]
        and playing_video_fingerprint == job["video_fingerprint"]
    ):
        player.setSubtitles(local)
        player.showSubtitles(True)
        message = "Nederlandse ondertitel is geladen."
    else:
        message = "Vertaling is gereed en op de server opgeslagen."

    xbmcgui.Dialog().notification(
        ADDON_NAME,
        message,
        xbmcgui.NOTIFICATION_INFO,
        7000,
    )


def poll_jobs(player: xbmc.Player, jobs: dict[str, dict]) -> bool:
    changed = False
    for job_id, job in list(jobs.items()):
        status = read_json(status_path(job["source"]))
        if not status or status.get("job_id") != job_id:
            continue

        state = status.get("state")
        if state == "complete":
            try:
                load_completed_subtitle(player, job, status)
            except (OSError, RuntimeError, ValueError) as exc:
                log(str(exc), xbmc.LOGERROR)
                xbmcgui.Dialog().notification(
                    ADDON_NAME,
                    safe_label(str(exc)),
                    xbmcgui.NOTIFICATION_ERROR,
                    8000,
                )
            jobs.pop(job_id, None)
            changed = True
        elif state == "failed":
            message = safe_label(str(status.get("message", "Vertaling mislukt.")))
            xbmcgui.Dialog().notification(
                ADDON_NAME,
                message,
                xbmcgui.NOTIFICATION_ERROR,
                9000,
            )
            jobs.pop(job_id, None)
            changed = True

    if changed:
        save_jobs(jobs)
    return changed


def main() -> None:
    xbmcvfs.mkdirs(PROFILE_PATH)
    xbmcvfs.mkdirs(TRANSLATED_PATH)
    monitor = xbmc.Monitor()
    player = PlaybackPlayer()
    automatic_search = AutomaticSearch()
    jobs = load_jobs()
    folder = configured_folder()

    try:
        baseline = snapshot(folder)
    except OSError as exc:
        baseline = {}
        log(str(exc), xbmc.LOGERROR)
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            str(exc),
            xbmcgui.NOTIFICATION_ERROR,
            9000,
        )
    previous = dict(baseline)
    temp_baseline = snapshot_kodi_temp()
    temp_previous = dict(temp_baseline)
    log(f"Service gestart; gecontroleerde map: {folder}; Kodi-tempmap: {TEMP_PATH}")

    while not monitor.waitForAbort(1.0):
        automatic_search.observe(player)
        try:
            poll_jobs(player, jobs)
        except (OSError, RuntimeError, ValueError) as exc:
            log(f"Tijdelijke fout bij vertaalstatus: {safe_label(str(exc))}", xbmc.LOGWARNING)

        selected_folder = configured_folder()
        if selected_folder != folder:
            folder = selected_folder
            try:
                baseline = snapshot(folder)
            except OSError:
                baseline = {}
            previous = dict(baseline)
            log(f"Gecontroleerde map gewijzigd: {folder}")
            continue

        if not player.isPlayingVideo():
            try:
                baseline = snapshot(folder)
                previous = dict(baseline)
            except OSError:
                pass
            temp_baseline = snapshot_kodi_temp()
            temp_previous = dict(temp_baseline)
            continue

        try:
            current = snapshot(folder)
        except OSError:
            continue

        temp_current = snapshot_kodi_temp()
        candidates = stable_candidates(baseline, previous, current)
        temp_candidates = stable_candidates(
            temp_baseline,
            temp_previous,
            temp_current,
        )
        if candidates:
            automatic_search.skip_current(player)
            stream = active_subtitle_name(player)
            handled_candidates: list[str] = []
            if is_dutch_subtitle(stream):
                handled_candidates = candidates
            else:
                source = choose_candidate(candidates)
                if source:
                    handled_candidates = [source]
                    try:
                        stat = xbmcvfs.Stat(source)
                        if stat.st_size() > MAX_SUBTITLE_BYTES:
                            raise ValueError("Ondertitel is groter dan 2 MB.")
                        identity = playback_identity(player)
                        raw = read_subtitle_bytes(source)
                        approved_sha256 = hashlib.sha256(raw).hexdigest()
                        preview = first_dialogue(raw.decode("utf-8-sig"))
                        if ask_yes_no(source.rsplit("/", 1)[-1], stream, preview):
                            start_job(
                                source,
                                identity[0],
                                jobs,
                                player=player,
                                approved_sha256=approved_sha256,
                                expected_identity=identity,
                            )
                    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                        log(str(exc), xbmc.LOGERROR)
                        xbmcgui.Dialog().notification(
                            ADDON_NAME,
                            safe_label(str(exc)),
                            xbmcgui.NOTIFICATION_ERROR,
                            8000,
                        )
                else:
                    # Annuleren in de keuzelijst wijst alle getoonde kandidaten af.
                    handled_candidates = candidates

            for path in handled_candidates:
                baseline[path] = current[path]

            # Kodi kan dezelfde download ook in zijn tempmap bewaren. Wanneer
            # de SMB-kopie er al is, voorkomt dit een tweede bevestigingspopup.
            for path in temp_candidates:
                temp_baseline[path] = temp_current[path]

        elif temp_candidates:
            automatic_search.skip_current(player)
            stream = active_subtitle_name(player)
            handled_temp_candidates: list[str] = []
            if is_dutch_subtitle(stream):
                handled_temp_candidates = temp_candidates
            else:
                temp_source = choose_candidate(temp_candidates)
                if temp_source:
                    handled_temp_candidates = [temp_source]
                    try:
                        stat = xbmcvfs.Stat(temp_source)
                        if stat.st_size() > MAX_SUBTITLE_BYTES:
                            raise ValueError("Ondertitel is groter dan 2 MB.")
                        identity = playback_identity(player)
                        raw = read_subtitle_bytes(temp_source)
                        approved_sha256 = hashlib.sha256(raw).hexdigest()
                        preview = first_dialogue(raw.decode("utf-8-sig"))
                        if ask_yes_no(
                            temp_source.rsplit("/", 1)[-1],
                            stream,
                            preview,
                        ):
                            xbmc.sleep(1)
                            if playback_identity(player) != identity:
                                raise sync_client.SyncError("De video is veranderd; bevestig de ondertitel opnieuw.")
                            if subtitle_sha256(temp_source) != approved_sha256:
                                raise sync_client.SyncError("De ondertitel is veranderd; bevestig de ondertitel opnieuw.")
                            staged = stage_temp_subtitle(temp_source, folder)
                            staged_stat = xbmcvfs.Stat(staged)
                            baseline[staged] = (
                                int(staged_stat.st_size()),
                                int(staged_stat.st_mtime()),
                            )
                            start_job(
                                staged,
                                identity[0],
                                jobs,
                                player=player,
                                approved_sha256=approved_sha256,
                                expected_identity=identity,
                            )
                    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                        log(str(exc), xbmc.LOGERROR)
                        xbmcgui.Dialog().notification(
                            ADDON_NAME,
                            safe_label(str(exc)),
                            xbmcgui.NOTIFICATION_ERROR,
                            8000,
                        )
                else:
                    handled_temp_candidates = temp_candidates

            for path in handled_temp_candidates:
                temp_baseline[path] = temp_current[path]

        else:
            automatic_search.run_if_due(player, folder, jobs, baseline)

        previous = current
        temp_previous = temp_current

    log("Service gestopt")


if __name__ == "__main__":
    main()
