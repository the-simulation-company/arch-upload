"""Transfer a local file using a supplied, temporary TUS upload capability."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from tusclient.client import TusClient
from tusclient.exceptions import TusCommunicationError, TusUploadFailed

CHUNK_SIZE = 6 * 1024 * 1024
RETRY_DELAYS = (1, 3, 5, 10, 20)


class UploadError(Exception):
    """Only these deliberately credential-free messages are displayed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _private_json(path: Path, value: dict) -> None:
    """Replace state atomically without temporarily making it world-readable."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_private_json(path: Path) -> dict:
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise UploadError("private_file_required", "Restrict session/state file access to its "
                          "owner (chmod 600 on macOS/Linux), then retry.")
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise UploadError("invalid_session", "The session/state file must contain a JSON object.")
    return value


def _identity(path: Path) -> dict:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise UploadError("invalid_file", "Supply a regular local file.")
    return {
        "path": str(path), "size": info.st_size, "mtimeNs": info.st_mtime_ns,
        "device": info.st_dev, "inode": info.st_ino,
    }


def _check_url(url: str, endpoint: str | None = None) -> None:
    parsed = urlsplit(url)
    # Loopback HTTP is useful for local integration tests; remote transfers require TLS.
    if (parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UploadError("invalid_session", "The upload session contains an invalid endpoint.")
    if endpoint and (parsed.scheme, parsed.netloc) != (
        urlsplit(endpoint).scheme, urlsplit(endpoint).netloc,
    ):
        raise UploadError("invalid_session", "The resume URL belongs to a different upload host.")


def _network_error(error: Exception) -> UploadError | None:
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return UploadError("credentials_expired", "Request fresh credentials using "
                           "begin_file_upload with the same uploadReference. Replace the session "
                           "JSON, preserve its state file, and rerun this command.")
    if status in (404, 410):
        return UploadError("session_expired", "The resumable session expired. First call "
                           "finalize_upload in case the transfer already completed. If incomplete, "
                           "renew credentials and rerun with --restart to transfer from byte zero.")
    if status and 400 <= status < 500 and status not in (408, 409, 423, 429):
        return UploadError("upload_rejected", f"Storage rejected the upload (HTTP {status}). "
                           "Check the supplied session and file size.")
    return None


def transfer(file: Path, session_file: Path, *, restart: bool = False) -> dict:
    file = file.resolve(strict=True)
    session = _read_private_json(session_file)
    reference = session["uploadReference"]
    endpoint = session["endpoint"]
    _check_url(endpoint)
    if not isinstance(reference, str) or not reference or not isinstance(session["token"], str):
        raise UploadError("invalid_session", "The upload session is missing its identity or token.")
    identity = _identity(file)
    if type(session["sizeBytes"]) is not int or not 0 < identity["size"] == session["sizeBytes"]:
        raise UploadError("size_mismatch", "The local file size does not match the upload session.")
    state_file = session_file.with_name(session_file.name + ".state.json")
    state = _read_private_json(state_file) if state_file.exists() else None
    if state:
        if (state["uploadReference"] != reference or state["identity"] != identity
                or state["endpoint"] != endpoint):
            raise UploadError("file_changed", "The file or upload identity changed. Do not resume "
                              "this transfer; begin a new upload with a new filename.")
        _check_url(state["url"], endpoint)
        if state.get("complete"):
            return {"status": "uploaded", "uploadReference": reference,
                    "sizeBytes": identity["size"],
                    "nextAction": "finalize_upload"}
        if restart:
            state = None

    client = TusClient(endpoint, headers={"x-signature": session["token"]})
    # URL persistence is managed here so token renewal never discards resume state and
    # the library cannot silently turn an authentication failure into a fresh transfer.
    uploader = client.uploader(
        str(file), chunk_size=CHUNK_SIZE, metadata=session["metadata"], retries=0,
    )
    if state:
        uploader.set_url(state["url"])

    def check_file() -> None:
        if _identity(file) != identity:
            raise UploadError("file_changed", "The local file changed during the transfer. "
                              "Do not finalize it; begin again with a new filename.")

    def persist(complete: bool = False) -> None:
        _check_url(uploader.url, endpoint)
        _private_json(state_file, {
            "uploadReference": reference, "identity": identity, "endpoint": endpoint,
            "url": uploader.url, "complete": complete,
        })

    failures = 0
    must_read_offset = bool(state)
    last_progress = 0.0
    while True:
        check_file()
        try:
            if not uploader.url:
                uploader.set_url(uploader.create_url())
                persist()
            if must_read_offset:
                uploader.offset = uploader.get_offset()
                must_read_offset = False
            if not 0 <= uploader.offset <= identity["size"]:
                raise UploadError("invalid_offset", "Storage returned an invalid upload offset.")
            if uploader.offset == identity["size"]:
                break
            uploader.upload_chunk()
            failures = 0
            now = time.monotonic()
            if now - last_progress >= 1 or uploader.offset == identity["size"]:
                print(f"Uploaded {uploader.offset}/{identity['size']} bytes "
                      f"({100 * uploader.offset / identity['size']:.1f}%)", file=sys.stderr)
                last_progress = now
        except (TusCommunicationError, TusUploadFailed, requests.RequestException) as error:
            terminal = _network_error(error)
            if terminal:
                raise terminal from None
            if failures == len(RETRY_DELAYS):
                raise UploadError(
                    "transfer_interrupted", "Transfer interrupted; rerun this command "
                    "with the same session file to resume. If the final response was lost, "
                    "finalize_upload can verify completion.",
                ) from None
            time.sleep(RETRY_DELAYS[failures])
            failures += 1
            # A failed PATCH may have been accepted. Ask the server before resending it.
            must_read_offset = bool(uploader.url)
    check_file()
    persist(complete=True)
    return {"status": "uploaded", "uploadReference": reference, "sizeBytes": identity["size"],
            "nextAction": "finalize_upload"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a local file using a temporary session.")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--restart", action="store_true",
                        help="Restart an expired resumable session after checking finalization.")
    args = parser.parse_args()
    try:
        result = transfer(args.file, args.session, restart=args.restart)
    except UploadError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted",
                          "message": "Rerun with the same session to resume."}))
        return 130
    except Exception:
        # Third-party exceptions may contain URLs, headers or server bodies. Never echo them.
        print(json.dumps({"status": "error", "code": "invalid_input_or_transfer",
                          "message": "Could not read the file/session or complete the transfer. "
                          "Check file access and session JSON, then rerun to resume."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
