const urlInput = document.getElementById("url");

const typeSelect = document.getElementById("type");

const formatSelect = document.getElementById("format");

const qualitySelect = document.getElementById("quality");

const qualityLabel = document.getElementById("quality-label");

const downloadButton = document.getElementById("download-button");

const cancelButton = document.getElementById("cancel-button");

const statusLabel = document.getElementById("status");

const percentageLabel = document.getElementById("percentage");

const progressBar = document.getElementById("progress-bar");

const currentVideo = document.getElementById("current-video");

const progressInfo = document.getElementById("progress-info");

const results = document.getElementById("results");

const logBox = document.getElementById("log");


let currentJob = null;

let pollingTimer = null;

let downloadFinished = false;


// ============================================================
// LOGGING
// ============================================================

function log(message) {

    const line = document.createElement("p");

    line.textContent = message;

    logBox.appendChild(line);

    logBox.scrollTop = logBox.scrollHeight;
}


// ============================================================
// FORMAT SWITCHING
// ============================================================

function updateFormat() {

    qualitySelect.innerHTML = "";

    if (formatSelect.value === "MP3") {

        qualityLabel.textContent = "AUDIO QUALITY";

        const qualities = [
            "320 kbps",
            "256 kbps",
            "192 kbps",
            "128 kbps"
        ];

        qualities.forEach(quality => {

            const option = document.createElement("option");

            option.textContent = quality;

            qualitySelect.appendChild(option);

        });

    } else {

        qualityLabel.textContent = "VIDEO QUALITY";

        const qualities = [
            "Best",
            "1080p",
            "720p",
            "480p",
            "360p"
        ];

        qualities.forEach(quality => {

            const option = document.createElement("option");

            option.textContent = quality;

            qualitySelect.appendChild(option);

        });

    }

}

formatSelect.addEventListener(
    "change",
    updateFormat
);


// ============================================================
// FORMAT FILE SIZE
// ============================================================

function formatBytes(bytes) {

    if (bytes < 1024) {
        return bytes + " B";
    }

    if (bytes < 1024 * 1024) {
        return (bytes / 1024).toFixed(1) + " KB";
    }

    if (bytes < 1024 * 1024 * 1024) {
        return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}


// ============================================================
// START DOWNLOAD
// ============================================================

async function startDownload() {

    const url = urlInput.value.trim();

    if (!url) {

        log("ERROR: Enter a YouTube URL.");

        return;
    }

    downloadFinished = false;

    downloadButton.disabled = true;

    cancelButton.disabled = false;

    results.innerHTML = "";

    progressBar.style.width = "0%";

    percentageLabel.textContent = "0%";

    statusLabel.textContent = "Starting...";

    currentVideo.textContent = "Connecting to YouTube...";

    progressInfo.textContent = "Preparing download...";

    log("Starting download...");

    const settings = {

        url: url,

        type: typeSelect.value,

        format: formatSelect.value,

        quality: qualitySelect.value

    };

    try {

        const response = await fetch("/api/start", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify(settings)

        });

        const data = await response.json();

        if (!response.ok) {

            throw new Error(
                data.error || "Could not start download."
            );

        }

        currentJob = data.job_id;

        log("Download session created.");

        pollStatus();

    } catch (error) {

        log("ERROR: " + error.message);

        statusLabel.textContent = "Error";

        downloadButton.disabled = false;

        cancelButton.disabled = true;

    }

}

downloadButton.addEventListener(
    "click",
    startDownload
);


// ============================================================
// POLL STATUS
// ============================================================

async function pollStatus() {

    if (!currentJob || downloadFinished) {
        return;
    }

    try {

        const response = await fetch(
            "/api/status/" + currentJob
        );

        const data = await response.json();

        if (!response.ok) {

            throw new Error(
                data.error || "Could not retrieve download status."
            );

        }

        statusLabel.textContent = data.message;

        const progress = Math.min(
            100,
            Math.max(0, data.progress || 0)
        );

        progressBar.style.width = progress + "%";

        percentageLabel.textContent = progress + "%";

        currentVideo.textContent = data.current_video || "";

        if (data.total > 0) {

            progressInfo.textContent =
                data.completed + " completed out of " + data.total;

        }

        if (data.status === "finished") {

            downloadFinished = true;

            progressBar.style.width = "100%";

            percentageLabel.textContent = "100%";

            statusLabel.textContent = "Finished";

            log("Download completed.");

            displayResults(data);

            finishInterface();

            return;

        }

        if (data.status === "cancelled") {

            downloadFinished = true;

            statusLabel.textContent = "Cancelled";

            log("Download cancelled.");

            finishInterface();

            return;

        }

        if (data.status === "error") {

            downloadFinished = true;

            statusLabel.textContent = "Error";

            log("ERROR: " + data.error);

            finishInterface();

            return;

        }

    } catch (error) {

        log("ERROR: " + error.message);

        downloadFinished = true;

        finishInterface();

        return;

    }

    pollingTimer = setTimeout(
        pollStatus,
        1000
    );

}


// ============================================================
// DISPLAY RESULTS
// ============================================================

function displayResults(data) {

    results.innerHTML = "";

    if (!data.files || data.files.length === 0) {

        results.textContent = "No files were produced.";

        return;
    }

    data.files.forEach(file => {

        const card = document.createElement("div");

        card.className = "file-card";

        const details = document.createElement("div");

        const name = document.createElement("div");

        name.className = "file-name";

        name.textContent = file.name;

        const size = document.createElement("div");

        size.className = "file-size";

        size.textContent = formatBytes(file.size);

        details.appendChild(name);

        details.appendChild(size);

        const button = document.createElement("a");

        button.className = "file-download";

        button.textContent = "DOWNLOAD";

        button.href =
            "/download/" +
            currentJob +
            "/" +
            encodeURIComponent(file.name);

        button.setAttribute("download", "");

        card.appendChild(details);

        card.appendChild(button);

        results.appendChild(card);

    });

    if (data.has_zip) {

        const zipButton = document.createElement("a");

        zipButton.className = "zip-button";

        zipButton.textContent = "DOWNLOAD ENTIRE PLAYLIST (ZIP)";

        zipButton.href =
            "/download-zip/" + currentJob;

        zipButton.setAttribute("download", "");

        results.prepend(zipButton);

    }

    log(data.files.length + " file(s) ready to download.");

}


// ============================================================
// CANCEL
// ============================================================

async function cancelDownload() {

    if (!currentJob) {
        return;
    }

    cancelButton.disabled = true;

    statusLabel.textContent = "Cancelling...";

    log("Cancellation requested.");

    try {

        await fetch(
            "/api/cancel/" + currentJob,
            {
                method: "POST"
            }
        );

    } catch (error) {

        log("Could not send cancellation request.");

    }

}

cancelButton.addEventListener(
    "click",
    cancelDownload
);


// ============================================================
// FINISH INTERFACE
// ============================================================

function finishInterface() {

    clearTimeout(pollingTimer);

    downloadButton.disabled = false;

    cancelButton.disabled = true;

    currentJob = null;

}


// ============================================================
// INITIALIZATION
// ============================================================

updateFormat();

log("Web downloader ready.");
