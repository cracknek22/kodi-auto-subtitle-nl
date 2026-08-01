from __future__ import annotations

import json
import os
import sys
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


MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
JOBS_PATH = os.path.join(PROFILE_PATH, "jobs.json")
TRANSLATED_PATH = os.path.join(PROFILE_PATH, "translated")
TEMP_PATH = xbmcvfs.translatePath("special://temp/").rstrip("/").rstrip("\\") + "/"


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


def ask_yes_no(filename: str, stream: str, preview: str) -> bool:
    message = (
        f"Bestand: {safe_label(filename)}\n"
        f"Actieve ondertitel: {safe_label(stream)}\n\n"
        f"Voorbeeld: {safe_label(preview)}\n\n"
        "Deze ondertitel naar natuurlijk Nederlands vertalen?"
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
) -> None:
    job_id = f"job_{uuid.uuid4().hex}"
    payload = build_request(source, job_id)
    write_json(request_path(source), payload)
    jobs[job_id] = {
        "job_id": job_id,
        "source": source,
        "video_fingerprint": playing_video_fingerprint,
    }
    save_jobs(jobs)
    xbmcgui.Dialog().notification(
        ADDON_NAME,
        "Vertaling gestart; de film blijft gewoon spelen.",
        xbmcgui.NOTIFICATION_INFO,
        6000,
    )


def output_path(job: dict, status: dict) -> str:
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
    player = xbmc.Player()
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
                        preview = first_dialogue(read_text(source))
                        if ask_yes_no(source.rsplit("/", 1)[-1], stream, preview):
                            start_job(
                                source,
                                current_video_fingerprint(player),
                                jobs,
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
                        preview = first_dialogue(read_text(temp_source))
                        if ask_yes_no(
                            temp_source.rsplit("/", 1)[-1],
                            stream,
                            preview,
                        ):
                            staged = stage_temp_subtitle(temp_source, folder)
                            staged_stat = xbmcvfs.Stat(staged)
                            baseline[staged] = (
                                int(staged_stat.st_size()),
                                int(staged_stat.st_mtime()),
                            )
                            start_job(
                                staged,
                                current_video_fingerprint(player),
                                jobs,
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

        previous = current
        temp_previous = temp_current

    log("Service gestopt")


if __name__ == "__main__":
    main()
