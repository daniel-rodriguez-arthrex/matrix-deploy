"""Jenkins client for triggering a new "Embedded Builder" build."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote

import requests

Logger = Callable[[str, str], None]

JENKINS_URL = "https://jenkins-embedded.dev.actsw.net"
JENKINS_JOB = "Embedded Builder"

# Fixed build parameters, matching the manual "matrix / wrynose" release
# build triggered from the Jenkins UI.
BUILD_PARAMETERS = {
    "MACHINE": "matrix",
    "BUILD_EXTRAS": "",
    "BUILD_SDK": "",
    "BUILD_PUBLIC_SDK": "",
    "SCA_FORCE_RUN": "",
    "PULL_LATEST_UPSTREAM": "",
    "PUBLISH_AF2": "on",
    "PUBLISH_OPKG": "",
    "META_ACT_BRANCH": "refs/heads/wrynose",
    "COMMENT": "",
    "BB_BRANCH": "DEV",
    "TROUBLESHOOT_BUILD": "",
    "ENABLE_UNIT_TESTS": "",
    "HUMAN_USE": "no",
    "SSTATE_RECIPE_BUILD": "",
    "SSTATE_RECIPES_TO_BUILD": "",
    "PUBLISH_QEMU_ARTIFACTS": "",
}


class JenkinsError(Exception):
    """Raised when a Jenkins operation fails."""


@dataclass
class JenkinsCredentials:
    username: str
    token: str


@dataclass
class BuildResult:
    """Outcome of a successfully-triggered build.

    ``build_number``/``build_url`` are ``None`` if the build was still
    sitting in Jenkins' queue (e.g. waiting on a free executor) when we
    stopped polling - the build was still triggered successfully, we just
    couldn't confirm its final number yet.
    """

    queue_url: Optional[str]
    build_number: Optional[int]
    build_url: Optional[str]


def _describe_response(resp: requests.Response) -> str:
    """Best-effort human-readable summary of a failed response: status,
    server/auth headers, and a truncated body preview (HTML login pages,
    JSON error bodies, etc. are all common here)."""
    body = (resp.text or "").strip()
    if len(body) > 300:
        body = body[:300] + "... (truncated)"
    www_auth = resp.headers.get("WWW-Authenticate", "")
    parts = [f"HTTP {resp.status_code} from {resp.url}"]
    if www_auth:
        parts.append(f"WWW-Authenticate: {www_auth}")
    if body:
        parts.append(f"Body: {body!r}")
    return " | ".join(parts)


class JenkinsClient:
    def __init__(
        self,
        creds: JenkinsCredentials,
        url: str = JENKINS_URL,
        job_name: str = JENKINS_JOB,
    ):
        self.url = url.rstrip("/")
        self.job_name = job_name
        self.auth = (creds.username, creds.token)
        self.session = requests.Session()

    def _get_crumb(self, log: Optional[Logger] = None) -> dict:
        """Fetch a CSRF crumb as a ``{field_name: value}`` form field.

        Returns an empty dict if the Jenkins instance has CSRF protection
        disabled (``crumbIssuer`` returns 404).
        """
        resp = self.session.get(
            f"{self.url}/crumbIssuer/api/json", auth=self.auth, timeout=15
        )
        if resp.status_code == 404:
            return {}
        if resp.status_code == 401:
            if log is not None:
                log(_describe_response(resp), "detail")
            raise JenkinsError(
                "Authentication failed fetching CSRF crumb - check your "
                "Jenkins username (login ID, not email) and token."
            )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            if log is not None:
                log(_describe_response(resp), "detail")
            raise JenkinsError(f"Failed to fetch CSRF crumb: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            if log is not None:
                log(_describe_response(resp), "detail")
            raise JenkinsError(
                "Crumb response was not JSON - Jenkins may be behind a "
                "login page/proxy that intercepted this request."
            ) from exc
        return {data["crumbRequestField"]: data["crumb"]}

    def _wait_for_build_number(
        self,
        queue_url: str,
        log: Optional[Logger] = None,
        timeout: float = 90.0,
        poll_interval: float = 2.0,
    ) -> "tuple[Optional[int], Optional[str]]":
        """Poll a Jenkins queue item until it turns into an actual build (or
        we give up/it's cancelled). Returns ``(build_number, build_url)``,
        both ``None`` if we timed out or the queued item was cancelled."""
        queue_api = queue_url.rstrip("/") + "/api/json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = self.session.get(queue_api, auth=self.auth, timeout=15)
            except requests.RequestException as exc:
                if log is not None:
                    log(f"  (queue poll failed, retrying: {exc})", "detail")
                time.sleep(poll_interval)
                continue

            if resp.status_code == 200:
                data = resp.json()
                if data.get("cancelled"):
                    if log is not None:
                        log("Queued build was cancelled.", "warning")
                    return None, None
                executable = data.get("executable")
                if executable:
                    return executable.get("number"), executable.get("url")
                why = data.get("why")
                if why and log is not None:
                    log(f"  Queued: {why}", "detail")
            time.sleep(poll_interval)

        if log is not None:
            log(
                "Timed out waiting for the queued build to start - it may "
                "still start later; check Jenkins directly.",
                "warning",
            )
        return None, None

    def trigger_build(
        self, log: Optional[Logger] = None, wait_for_number: bool = True
    ) -> BuildResult:
        """Trigger a new build of ``job_name`` using the fixed ``matrix`` /
        ``wrynose`` parameters (mirrors a manual build kicked off from the
        Jenkins UI). Returns a ``BuildResult`` with the build number/URL once
        Jenkins assigns one (best-effort; may be ``None`` if still queued)."""
        if log is not None:
            log("Requesting CSRF crumb from Jenkins...", "info")
        crumb_field = self._get_crumb(log)

        form_data = dict(BUILD_PARAMETERS)
        form_data.update(crumb_field)

        job_path = quote(self.job_name, safe="")
        if log is not None:
            log(f"Triggering build for '{self.job_name}' (matrix / wrynose)...", "info")

        resp = self.session.post(
            f"{self.url}/job/{job_path}/buildWithParameters",
            data=form_data,
            auth=self.auth,
            timeout=30,
            allow_redirects=False,
        )

        if resp.status_code not in (200, 201, 302, 303):
            if log is not None:
                log(_describe_response(resp), "detail")
            if resp.status_code == 401:
                raise JenkinsError(
                    "Authentication failed triggering the build - check your "
                    "Jenkins username (login ID, not email) and token."
                )
            if resp.status_code == 403:
                raise JenkinsError(
                    "Forbidden - check that this account has permission to build "
                    f"'{self.job_name}' on Jenkins."
                )
            if resp.status_code == 404:
                raise JenkinsError(
                    f"Job '{self.job_name}' not found - check the job name/URL."
                )
            raise JenkinsError(f"Unexpected response from Jenkins (HTTP {resp.status_code}).")

        queue_url = resp.headers.get("Location")
        build_number: Optional[int] = None
        build_url: Optional[str] = None
        if queue_url and wait_for_number:
            if log is not None:
                log("Build queued - waiting for a build number...", "info")
            build_number, build_url = self._wait_for_build_number(queue_url, log)

        if log is not None:
            if build_number is not None:
                log(f"Build #{build_number} started: {build_url}", "success")
            else:
                log("Build triggered successfully.", "success")

        return BuildResult(queue_url=queue_url, build_number=build_number, build_url=build_url)
