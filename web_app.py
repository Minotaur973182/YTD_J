import os
import uuid
import time
import shutil
import tempfile
import threading
import zipfile

from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    abort
)

import yt_dlp


# ============================================================
# CONFIGURATION
# ============================================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

TEMP_ROOT = Path(tempfile.gettempdir()) / "YTD_J"

TEMP_ROOT.mkdir(parents=True, exist_ok=True)

TEMP_FILE_LIFETIME = 30 * 60

jobs = {}

jobs_lock = threading.Lock()


# ============================================================
# JOB MANAGEMENT
# ============================================================

def create_job():

    job_id = str(uuid.uuid4())

    job_folder = TEMP_ROOT / job_id

    job_folder.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "folder": job_folder,
        "status": "queued",
        "message": "Waiting to start...",
        "progress": 0,
        "current_video": "",
        "completed": 0,
        "failed": 0,
        "total": 0,
        "files": [],
        "error": None,
        "cancel_event": threading.Event(),
        "created": time.time(),
        "last_access": time.time(),
        "zip_path": None
    }

    with jobs_lock:
        jobs[job_id] = job

    return job


def get_job(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

        if job:
            job["last_access"] = time.time()

        return job


def cleanup_job(job_id):

    with jobs_lock:

        job = jobs.get(job_id)

        if not job:
            return

        if job["status"] in ("queued", "downloading"):
            return

        folder = job["folder"]

        jobs.pop(job_id, None)

    shutil.rmtree(folder, ignore_errors=True)


def cleanup_worker():

    while True:

        time.sleep(60)

        now = time.time()

        expired = []

        with jobs_lock:

            for job_id, job in list(jobs.items()):

                if job["status"] in ("queued", "downloading"):
                    continue

                if now - job["last_access"] > TEMP_FILE_LIFETIME:
                    expired.append(job_id)

        for job_id in expired:
            cleanup_job(job_id)


threading.Thread(
    target=cleanup_worker,
    daemon=True
).start()


# ============================================================
# PROGRESS LOGGER
# ============================================================

class DownloadLogger:

    def __init__(self, job):
        self.job = job

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):

        with jobs_lock:
            self.job["message"] = str(message)[:250]

    def error(self, message):

        with jobs_lock:
            self.job["message"] = str(message)[:250]


# ============================================================
# PROGRESS HOOK
# ============================================================

def make_progress_hook(job):

    def progress_hook(data):

        if job["cancel_event"].is_set():

            raise yt_dlp.utils.DownloadError(
                "Download cancelled."
            )

        status = data.get("status")

        info = data.get("info_dict", {})

        title = info.get("title", "Downloading video")

        playlist_count = (
            info.get("playlist_count")
            or info.get("n_entries")
            or 0
        )

        playlist_index = info.get("playlist_index") or 1

        with jobs_lock:

            job["current_video"] = title

            if playlist_count:
                job["total"] = max(
                    job["total"],
                    playlist_count
                )

            if status == "downloading":

                downloaded = data.get("downloaded_bytes", 0)

                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                )

                if total:

                    job["progress"] = round(
                        downloaded / total * 100,
                        1
                    )

                job["message"] = (
                    f"Downloading video {playlist_index}"
                )

            elif status == "finished":

                job["progress"] = 100

                job["message"] = "Processing video..."

    return progress_hook


# ============================================================
# DOWNLOAD OPTIONS
# ============================================================

def make_download_options(job, settings):

    folder = job["folder"]

    selected_format = settings["format"]
    selected_quality = settings["quality"]

    options = {

        "outtmpl": str(
            folder / "%(title).100s [%(id)s].%(ext)s"
        ),

        "noplaylist": settings["type"] == "Single Video",

        "ignoreerrors": True,

        "quiet": True,

        "no_warnings": True,

        "logger": DownloadLogger(job),

        "progress_hooks": [
            make_progress_hook(job)
        ],

        "windowsfilenames": True,

        "retries": 5,

        "fragment_retries": 5,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

    }

    # MP3

    if selected_format == "MP3":

        bitrate = selected_quality.replace(
            " kbps",
            ""
        )

        options["format"] = "bestaudio/best"

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate
            }
        ]

    # MP4

    else:

        if selected_quality == "Best":

            options["format"] = (
                "bestvideo+bestaudio/best"
            )

        else:

            height = int(
                selected_quality.replace("p", "")
            )

            options["format"] = (
                f"bestvideo[height<={height}]+"
                f"bestaudio/best[height<={height}]/"
                f"best[height<={height}]"
            )

        options["merge_output_format"] = "mp4"

    return options


# ============================================================
# DOWNLOAD WORKER
# ============================================================

def download_worker(job, url, settings):

    try:

        with jobs_lock:

            job["status"] = "downloading"
            job["message"] = "Connecting to YouTube..."

        options = make_download_options(
            job,
            settings
        )

        with yt_dlp.YoutubeDL(options) as ydl:

            ydl.extract_info(
                url,
                download=True
            )

        if job["cancel_event"].is_set():

            with jobs_lock:
                job["status"] = "cancelled"
                job["message"] = "Download cancelled."

            return

        # Find finished files.

        files = []

        for path in job["folder"].iterdir():

            if path.is_file() and path.name != "playlist.zip":

                files.append(path)

        files.sort(
            key=lambda path: path.name.lower()
        )

        if not files:

            raise Exception(
                "No files were downloaded. Check whether the video is available."
            )

        file_data = []

        for path in files:

            file_data.append({
                "name": path.name,
                "size": path.stat().st_size
            })

        # Create ZIP for playlists with multiple files.

        zip_path = None

        if settings["type"] == "Playlist" and len(files) > 1:

            zip_path = job["folder"] / "playlist.zip"

            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED
            ) as archive:

                for path in files:

                    archive.write(
                        path,
                        arcname=path.name
                    )

        with jobs_lock:

            job["files"] = file_data

            job["completed"] = len(files)

            job["total"] = max(
                job["total"],
                len(files)
            )

            job["progress"] = 100

            job["zip_path"] = zip_path

            job["status"] = "finished"

            job["message"] = "Download finished!"

    except Exception as error:

        if job["cancel_event"].is_set():

            with jobs_lock:

                job["status"] = "cancelled"
                job["message"] = "Download cancelled."

        else:

            with jobs_lock:

                job["status"] = "error"
                job["error"] = str(error)
                job["message"] = "Download failed."


# ============================================================
# WEB PAGES
# ============================================================

@app.route("/")
def index():

    return render_template("index.html")


# ============================================================
# START DOWNLOAD
# ============================================================

@app.route("/api/start", methods=["POST"])
def start_download():

    data = request.get_json(silent=True) or {}

    url = data.get("url", "").strip()

    download_type = data.get("type", "Single Video")

    selected_format = data.get("format", "MP4")

    quality = data.get("quality", "Best")

    if not url:
        return jsonify({
            "error": "Please enter a YouTube URL."
        }), 400

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return jsonify({
            "error": "Invalid URL."
        }), 400

    if not parsed.hostname or not (
        parsed.hostname == "youtube.com"
        or parsed.hostname.endswith(".youtube.com")
        or parsed.hostname == "youtu.be"
        or parsed.hostname.endswith(".youtu.be")
    ):
        return jsonify({
            "error": "Please enter a YouTube URL."
        }), 400

    if download_type not in ("Single Video", "Playlist"):
        return jsonify({
            "error": "Invalid download type."
        }), 400

    if selected_format not in ("MP4", "MP3"):
        return jsonify({
            "error": "Invalid format."
        }), 400

    valid_video_qualities = [
        "Best",
        "1080p",
        "720p",
        "480p",
        "360p"
    ]

    valid_audio_qualities = [
        "320 kbps",
        "256 kbps",
        "192 kbps",
        "128 kbps"
    ]

    valid_qualities = (
        valid_audio_qualities
        if selected_format == "MP3"
        else valid_video_qualities
    )

    if quality not in valid_qualities:
        return jsonify({
            "error": "Invalid quality selection."
        }), 400

    if selected_format == "MP4" and not shutil.which("ffmpeg"):

        return jsonify({
            "error": "FFmpeg is not installed on the server."
        }), 500

    settings = {
        "type": download_type,
        "format": selected_format,
        "quality": quality
    }

    job = create_job()

    thread = threading.Thread(
        target=download_worker,
        args=(job, url, settings),
        daemon=True
    )

    thread.start()

    return jsonify({
        "job_id": job["id"]
    })


# ============================================================
# JOB STATUS
# ============================================================

@app.route("/api/status/<job_id>")
def download_status(job_id):

    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": "Download session expired."
        }), 404

    with jobs_lock:

        return jsonify({

            "id": job["id"],

            "status": job["status"],

            "message": job["message"],

            "progress": job["progress"],

            "current_video": job["current_video"],

            "completed": job["completed"],

            "failed": job["failed"],

            "total": job["total"],

            "files": job["files"],

            "error": job["error"],

            "has_zip": bool(job["zip_path"])

        })


# ============================================================
# CANCEL DOWNLOAD
# ============================================================

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_download(job_id):

    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": "Download session not found."
        }), 404

    with jobs_lock:

        if job["status"] in ("queued", "downloading"):

            job["cancel_event"].set()

            job["message"] = "Cancelling..."

    return jsonify({
        "success": True
    })


# ============================================================
# DOWNLOAD INDIVIDUAL FILE
# ============================================================

@app.route("/download/<job_id>/<filename>")
def download_file(job_id, filename):

    job = get_job(job_id)

    if not job:
        abort(404)

    if job["status"] != "finished":
        abort(404)

    # Only allow files recorded by the completed job.

    allowed = {
        item["name"]
        for item in job["files"]
    }

    if filename not in allowed:
        abort(404)

    path = job["folder"] / filename

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=filename
    )


# ============================================================
# DOWNLOAD PLAYLIST ZIP
# ============================================================

@app.route("/download-zip/<job_id>")
def download_zip(job_id):

    job = get_job(job_id)

    if not job:
        abort(404)

    if job["status"] != "finished":
        abort(404)

    zip_path = job["zip_path"]

    if not zip_path or not zip_path.is_file():
        abort(404)

    return send_file(
        zip_path,
        as_attachment=True,
        download_name="YouTube_Playlist.zip"
    )


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":

    print("=" * 50)
    print("YOUTUBE DOWNLOADER WEB SERVER")
    print("=" * 50)
    print("Local address: http://localhost:5000")
    print("=" * 50)

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True
    )
