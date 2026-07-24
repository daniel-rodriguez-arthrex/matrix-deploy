"""Artifactory client for discovering and downloading the latest SWU build."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import ArtifactoryConfig

Logger = Callable[[str, str], None]
Progress = Callable[[int, int], None]


class ArtifactoryError(Exception):
    """Raised when an Artifactory operation fails."""


@dataclass
class ArtifactoryCredentials:
    username: str
    token: str


class ArtifactoryClient:
    def __init__(self, config: ArtifactoryConfig, creds: ArtifactoryCredentials):
        self.config = config
        self.auth = (creds.username, creds.token)

    def _get(self, url: str, **kwargs) -> requests.Response:
        resp = requests.get(url, auth=self.auth, timeout=30, **kwargs)
        if resp.status_code == 401:
            raise ArtifactoryError("Authentication failed - check your email and token.")
        resp.raise_for_status()
        return resp

    def find_latest_swu(self, log: Optional[Logger] = None) -> str:
        """Return the latest .swu path relative to ``build_path``.

        Lists the repo folder (``repo/build_path``) recursively via the
        Artifactory Storage API and selects the newest ``.swu`` by
        ``lastModified``. If ``branch_filter`` is set, only files whose path
        contains that token are considered (falling back to all ``.swu`` files
        if the filter excludes everything).
        """
        base = f"{self.config.repo}/{self.config.build_path}".strip("/")
        url = f"{self.config.url}/api/storage/{base}?list&deep=1&listFolders=0"
        data = self._get(url).json()

        files = [
            f for f in data.get("files", [])
            if not f.get("folder", False) and f.get("uri", "").lower().endswith(".swu")
        ]
        if not files:
            raise ArtifactoryError(
                f"No .swu files found under '{base}'. "
                f"Check 'repo'/'build_path' in deploy_config.json."
            )

        candidates = files
        bf = (self.config.branch_filter or "").lower()
        if bf:
            filtered = [f for f in files if bf in f.get("uri", "").lower()]
            if filtered:
                candidates = filtered
            elif log is not None:
                log(
                    f"No .swu matched branch '{self.config.branch_filter}'; "
                    f"using newest of all {len(files)} files.",
                    "warning",
                )

        candidates.sort(key=lambda f: f.get("lastModified", ""), reverse=True)
        chosen = candidates[0]["uri"].lstrip("/")  # relative to build_path
        if log is not None:
            log(f"Latest SWU: {chosen.split('/')[-1]}", "success")
        return chosen

    def download_latest(
        self,
        cache_dir: Path,
        log: Logger,
        progress: Optional[Progress] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """Download the latest SWU into ``cache_dir`` and return its path.

        Uses an existing cached copy if present. Streams to a ``.part`` file
        first so an interrupted/cancelled download never leaves a corrupt file.
        """
        log("Listing latest SWU from Artifactory...", "info")
        rel_path = self.find_latest_swu(log)  # relative to build_path
        swu_name = rel_path.split("/")[-1]

        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / swu_name

        if dest.exists():
            log(f"Already cached: {swu_name}", "success")
            return dest

        download_url = f"{self.config.url}/{self.config.repo}/{self.config.build_path}/{rel_path}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        log(f"Downloading {swu_name}...", "info")

        try:
            with self._get(download_url, stream=True) as resp:
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                last_bucket = -1
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if is_cancelled and is_cancelled():
                            raise ArtifactoryError("Download cancelled by user.")
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(downloaded, total)
                        if total:
                            bucket = int(downloaded / total * 100) // 10
                            if bucket != last_bucket:
                                last_bucket = bucket
                                log(
                                    f"  {bucket * 10}% "
                                    f"({downloaded / 1048576:.0f}/{total / 1048576:.0f} MB)",
                                    "detail",
                                )
            tmp.replace(dest)
        finally:
            if tmp.exists():
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        log(f"Download complete: {swu_name}", "success")
        return dest
