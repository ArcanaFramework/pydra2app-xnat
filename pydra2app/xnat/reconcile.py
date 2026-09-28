import hashlib
import json
import re
import typing as ty
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import xnat

IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DOWNLOAD_TIMEOUT = 60
XNAT_ERRORS = (xnat.exceptions.XNATResponseError, OSError)


class ReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationResult:
    pipeline: str
    status: str
    installed: int = 0
    updated: int = 0
    unchanged: int = 0
    error: ty.Optional[str] = None

    def summary(self) -> str:
        counts = (
            f"installed={self.installed}, updated={self.updated}, "
            f"unchanged={self.unchanged}"
        )
        if self.error:
            return f"{self.pipeline}: {self.status} ({counts}; error={self.error})"
        return f"{self.pipeline}: {self.status} ({counts})"


@dataclass(frozen=True)
class _DesiredCommand:
    name: str
    image: str
    document: ty.Dict[str, ty.Any]


@dataclass(frozen=True)
class _DesiredPipeline:
    pipeline_id: str
    commands: ty.Tuple[_DesiredCommand, ...]


def _download(url: str) -> bytes:
    with urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
        return response.read()


def _read_catalogue(source: ty.Union[str, Path]) -> ty.Any:
    try:
        location = str(source)
        if location.startswith(("http://", "https://")):
            content = _download(location)
        else:
            content = Path(location).read_bytes()
        return json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReconciliationError(
            f"Could not load pipeline catalogue from {source!r}: {error}"
        ) from error


def _required_mapping(value: ty.Any, description: str) -> ty.Dict[str, ty.Any]:
    if not isinstance(value, dict):
        raise ReconciliationError(f"{description} must be a JSON object")
    return value


def _required_text(value: ty.Dict[str, ty.Any], key: str, description: str) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text:
        raise ReconciliationError(f"{description}.{key} must be a non-empty string")
    return text


def load_catalogue(
    source: ty.Union[str, Path],
    download: ty.Callable[[str], bytes] = _download,
) -> ty.Tuple[_DesiredPipeline, ...]:
    catalogue = _required_mapping(_read_catalogue(source), "catalogue")
    if catalogue.get("schema_version") != "1.0":
        raise ReconciliationError("catalogue.schema_version must be '1.0'")

    pipelines = catalogue.get("pipelines")
    if not isinstance(pipelines, list):
        raise ReconciliationError("catalogue.pipelines must be a JSON array")

    desired_names: ty.Set[str] = set()
    pipeline_ids: ty.Set[str] = set()
    desired_pipelines = []
    for pipeline_index, pipeline_value in enumerate(pipelines):
        description = f"catalogue.pipelines[{pipeline_index}]"
        pipeline = _required_mapping(pipeline_value, description)
        pipeline_id = _required_text(pipeline, "id", description)
        if pipeline_id in pipeline_ids:
            raise ReconciliationError(f"Duplicate pipeline id {pipeline_id!r}")
        pipeline_ids.add(pipeline_id)

        image = _required_text(pipeline, "image", description)
        if not IMAGE_DIGEST.fullmatch(image):
            raise ReconciliationError(
                f"{description}.image must be pinned by a SHA-256 digest"
            )

        commands = pipeline.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ReconciliationError(
                f"{description}.commands must be a non-empty JSON array"
            )

        desired_commands = []
        for command_index, command_value in enumerate(commands):
            command_description = f"{description}.commands[{command_index}]"
            command = _required_mapping(command_value, command_description)
            name = _required_text(command, "name", command_description)
            url = _required_text(command, "url", command_description)
            checksum = _required_text(command, "sha256", command_description)
            if not SHA256.fullmatch(checksum):
                raise ReconciliationError(
                    f"{command_description}.sha256 must be a SHA-256 checksum"
                )

            try:
                content = download(url)
            except OSError as error:
                raise ReconciliationError(
                    f"Could not download command {name!r} from {url!r}: {error}"
                ) from error
            actual_checksum = hashlib.sha256(content).hexdigest()
            if actual_checksum != checksum:
                raise ReconciliationError(
                    f"Checksum mismatch for command {name!r}: "
                    f"expected {checksum}, got {actual_checksum}"
                )
            try:
                document = _required_mapping(json.loads(content), f"command {name!r}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReconciliationError(
                    f"Command {name!r} is not valid JSON: {error}"
                ) from error
            # XNAT registers commands under the name in their JSON, which is the
            # catalogue name, optionally qualified by the pipeline, e.g. 'a.b.name'
            xnat_name = document.get("name")
            if not isinstance(xnat_name, str) or not (
                xnat_name == name or xnat_name.endswith("." + name)
            ):
                raise ReconciliationError(
                    f"Command asset name {xnat_name!r} "
                    f"does not match catalogue name {name!r}"
                )
            if xnat_name in desired_names:
                raise ReconciliationError(
                    f"Duplicate desired command name {xnat_name!r}"
                )
            desired_names.add(xnat_name)
            if document.get("image") != image:
                raise ReconciliationError(
                    f"Command {name!r} image {document.get('image')!r} "
                    f"does not match catalogue image {image!r}"
                )
            desired_commands.append(_DesiredCommand(xnat_name, image, document))

        desired_pipelines.append(
            _DesiredPipeline(
                pipeline_id,
                tuple(sorted(desired_commands, key=lambda command: command.name)),
            )
        )

    return tuple(sorted(desired_pipelines, key=lambda pipeline: pipeline.pipeline_id))


def reconcile_commands(
    xlogin: xnat.XNATSession,
    desired_pipelines: ty.Sequence[_DesiredPipeline],
) -> ty.Tuple[ReconciliationResult, ...]:
    try:
        installed_values = xlogin.get("/xapi/commands").json()
    except XNAT_ERRORS as error:
        message = f"Could not list installed commands: {error}"
        return tuple(
            ReconciliationResult(pipeline.pipeline_id, "failed", error=message)
            for pipeline in desired_pipelines
        )
    if not isinstance(installed_values, list):
        raise ReconciliationError("XNAT /xapi/commands response must be a JSON array")

    installed: ty.Dict[str, ty.Dict[str, ty.Any]] = {}
    duplicates = set()
    for index, value in enumerate(installed_values):
        command = _required_mapping(value, f"installed command {index}")
        name = _required_text(command, "name", f"installed command {index}")
        if name in installed:
            duplicates.add(name)
        installed[name] = command

    results = []
    for pipeline in desired_pipelines:
        counts = {"installed": 0, "updated": 0, "unchanged": 0}
        errors = []
        for desired in pipeline.commands:
            current = installed.get(desired.name)
            try:
                if desired.name in duplicates:
                    raise ReconciliationError(
                        "multiple installed commands share its name"
                    )
                if current is None:
                    xlogin.post("/xapi/commands", json=desired.document)
                    counts["installed"] += 1
                elif current.get("image") == desired.image:
                    counts["unchanged"] += 1
                else:
                    command_id = current.get("id")
                    if command_id is None:
                        raise ReconciliationError("installed command has no id")
                    # XNAT updates wrappers in place by name, keeping their IDs and
                    # hence their site and project enablement
                    xlogin.post(f"/xapi/commands/{command_id}", json=desired.document)
                    counts["updated"] += 1
            except (ReconciliationError, *XNAT_ERRORS) as error:
                errors.append(f"{desired.name}: {error}")

        if errors:
            status = "failed"
        elif counts["updated"]:
            status = "updated"
        elif counts["installed"]:
            status = "installed"
        else:
            status = "unchanged"
        results.append(
            ReconciliationResult(
                pipeline.pipeline_id,
                status,
                installed=counts["installed"],
                updated=counts["updated"],
                unchanged=counts["unchanged"],
                error="; ".join(errors) or None,
            )
        )

    return tuple(results)
