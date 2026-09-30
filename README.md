# arch-upload

A small command-line helper that sends a local file to a supplied TUS resumable-upload
endpoint. It contains generic file-transfer code, not application permissions or backend logic.

Requires Python 3.11+; the recommended `uvx` command manages the helper's installation.
Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/)
for macOS, Linux, or Windows if `uvx` is unavailable.

## Use

Request an upload session from the service that will receive the file. Save its `session`
object as a private JSON file outside source control. On macOS/Linux restrict access with
`chmod 600 /absolute/path/session.json`; on Windows use an owner-only directory/ACL.

```sh
uvx arch-upload@0.1.0 --file "/absolute/path/data.csv" --session "/absolute/path/session.json"
```

The session contains `uploadReference`, `sizeBytes`, `endpoint`, `token`, and a `metadata`
object for TUS creation. The helper uses the token only in the `x-signature` header. No
permanent account/API credentials are required. Never put session contents into logs or Git.

The helper sends 6 MiB chunks, writes progress to stderr, and emits a JSON result on stdout.
Exit 0 means the transfer finished. The receiving service must still finalize/register it.
Exit 1 reports a sanitized failure; exit 130 means interrupted.

## Resume

Re-run the same command. A private `session.json.state.json` companion records the upload URL
and local file identity. The helper asks the server for its offset before resuming. Keep the
local file unchanged, and preserve the state file when replacing expired session credentials.

If credentials expire, request fresh ones from the receiving service for the same upload
reference, replace the session JSON, and rerun. If the resumable session itself expires,
first ask the service to finalize in case the final response was lost. If still incomplete,
renew credentials and pass `--restart` to transfer again from byte zero. It never silently
restarts a large transfer. Delete both private files after the receiving service confirms readiness.

## Development and publishing

```sh
uv sync --python 3.13
uv run --python 3.13 pytest
uv build --python 3.13
```

CI tests Linux, macOS, and Windows. The release workflow publishes a reviewed version tag
through PyPI trusted publishing with GitHub environment `pypi`; there is no stored publishing
API key. The wheel includes only `arch_upload` and packaging metadata; the source distribution
also contains this README, NOTICE, pyproject.toml, and .gitignore. Dependencies are downloaded
separately and retain their licenses.

The source is publicly readable. No general reuse or open-source license is granted; see NOTICE.
