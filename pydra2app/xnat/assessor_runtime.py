"""Finalise successful command outputs at their XNAT execution frequency."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import typing as ty
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import xnat
from frametree.axes.medimage import MedImage
from frametree.core.frameset import FrameSet
from frametree.core.store import Store
from pydra.utils.typing import is_optional

from .assessor_store import AssessorOutputStore
from .assessors import (
    AssessorError,
    _digest,
    ASSESSOR_BASES,
    assessor_definition,
    output_resource_name,
    pipeline_display_name,
    supports_assessors,
    xsi_type,
)

if ty.TYPE_CHECKING:
    from .command import XnatCommand

logger = logging.getLogger(__name__)


def _records(directory: Path) -> list[dict[str, ty.Any]]:
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def finalise_assessor(
    xlogin: xnat.XNATSession,
    definition: dict[str, ty.Any],
    project: str,
    parent_id: str,
    parent_label: str,
    staging: Path,
    outputs: dict[str, str],
    form_id: str,
    provenance: dict[str, ty.Any],
) -> str:
    records = {
        record["path"].split("@", 1)[0]: record
        for record in _records(staging / "outputs")
    }
    scalar_outputs = {field["label"] for field in definition["fields"]}
    file_outputs = set(outputs) - scalar_outputs
    resources = {name: output_resource_name(name) for name in file_outputs}
    if len(set(resources.values())) != len(resources):
        raise AssessorError("File output names produce colliding resource labels")
    desired_files = {name: set() for name in file_outputs}
    inputs = _records(staging / "inputs")
    provenance = {
        **provenance,
        "inputs": inputs,
        "outputs": outputs,
        "output_provenance": _records(staging / "provenance"),
    }
    identity = {
        "pipeline": definition["pipeline"],
        **{
            key: provenance[key]
            for key in ("version", "image", "parameters", "inputs", "outputs")
        },
    }

    def run_label(pipeline: str, run_identity: dict[str, ty.Any]) -> str:
        return (
            re.sub(
                r"[^a-zA-Z0-9_-]",
                "_",
                f"{parent_label}_{pipeline}_{provenance['version']}",
            )[:180]
            + "_"
            + _digest(run_identity)[:16]
        )

    label = run_label(
        pipeline_display_name(definition["pipeline"]).replace(" / ", "_"),
        {**identity, "type": xsi_type(definition)},
    )
    labels = {label, run_label(definition["name"], identity)}
    if definition["extends"] == "imageAssessorData":
        collection = f"/data/experiments/{quote(parent_id, safe='')}/assessors"
        resource_segment = "/out/resources/"
    elif definition["extends"] == "subjectAssessorData":
        collection = f"/data/projects/{quote(project, safe='')}/subjects/{quote(parent_id, safe='')}/experiments"
        resource_segment = "/resources/"
    else:
        collection = f"/data/projects/{quote(project, safe='')}/experiments"
        resource_segment = "/resources/"

    def matching_assessors() -> list[dict[str, ty.Any]]:
        return [
            item
            for item in xlogin.get(
                collection,
                format="json",
                query=(
                    {"xsiType": "xnat:experimentData"}
                    if definition["extends"] == "abstractProjectAsset"
                    else {}
                ),
            ).json()["ResultSet"]["Result"]
            if item["label"] in labels
        ]

    existing = matching_assessors()
    target = xsi_type(definition)
    if existing:
        if len(existing) != 1 or existing[0]["xsiType"] != target:
            raise AssessorError(f"Existing assessor {label!r} has a different type")
    else:
        xlogin.put(
            collection + "/" + quote(label, safe=""),
            query={"xsiType": target, "project": project},
            accepted_status=[200, 201],
        )
        existing = matching_assessors()
        if len(existing) != 1 or existing[0]["xsiType"] != target:
            raise AssessorError(f"XNAT did not create assessor {label!r} as {target!r}")
    assessor_id: str = existing[0]["ID"]
    uri = (
        collection + "/" + quote(assessor_id, safe="")
        if definition["extends"] == "imageAssessorData"
        else "/data/experiments/" + quote(assessor_id, safe="")
    )
    fields = {
        "pipeline": definition["pipeline"],
        "version": provenance["version"],
        "image": provenance["image"],
        "parameters": json.dumps(provenance["parameters"], sort_keys=True, indent=2),
        "inputs": json.dumps(inputs, sort_keys=True, indent=2),
        "workflow": provenance["workflow"],
    }
    scalar_keys = {field["label"]: field["key"] for field in definition["fields"]}
    for name, path in outputs.items():
        record = records.get(path)
        if record is None:  # An optional output was not produced.
            continue
        if "value" in record:
            fields[scalar_keys[name]] = record["value"]
        else:
            resource = quote(resources[name], safe="")
            for fspath in map(Path, record["files"]):
                paths = sorted(fspath.rglob("*")) if fspath.is_dir() else [fspath]
                for file_path in paths:
                    if not file_path.is_file():
                        continue
                    relative = file_path.relative_to(fspath.parent).as_posix()
                    desired_files[name].add(relative)
                    xlogin.upload_file(
                        uri
                        + resource_segment
                        + resource
                        + "/files/"
                        + quote(relative, safe="/"),
                        path=file_path,
                        overwrite=True,
                        verbose=False,
                    )
    if resources:
        resource_collection = xlogin.get(
            uri + resource_segment.rstrip("/"), format="json"
        ).json()["ResultSet"]["Result"]
        existing_resources = {resource["label"] for resource in resource_collection}
        for name, resource in resources.items():
            if resource not in existing_resources:
                continue
            files_uri = uri + resource_segment + quote(resource, safe="") + "/files"
            files = xlogin.get(files_uri, format="json").json()["ResultSet"]["Result"]
            for file in files:
                relative = unquote(urlsplit(file["URI"]).path.split("/files/", 1)[1])
                if relative not in desired_files[name]:
                    xlogin.delete(file["URI"], accepted_status=[200, 204])
    xlogin.put(
        f"/xapi/custom-fields/experiments/{quote(assessor_id, safe='')}/fields",
        json={form_id: fields},
    )
    xlogin.upload_string(
        uri + resource_segment + "PROVENANCE/files/provenance.json",
        data=json.dumps(provenance, sort_keys=True),
        overwrite=True,
        content_type="application/json",
        query={"inbody": "true"},
    )
    return assessor_id


def execute_with_assessor(command: XnatCommand, address: str, **kwargs: ty.Any) -> None:
    if command.operates_on not in ASSESSOR_BASES:
        raise AssessorError(
            "Assessor outputs require session, subject or dataset frequency"
        )
    if kwargs.get("internal_upload") or command.internal_upload:
        raise AssessorError("Assessor outputs cannot use internal_upload")
    raw_ids = kwargs.get("ids")
    ids: list[str]
    if raw_ids is None:
        ids = []
    elif isinstance(raw_ids, str):
        ids = raw_ids.split(",")
    elif isinstance(raw_ids, (list, tuple)) and all(
        isinstance(row_id, str) for row_id in raw_ids
    ):
        ids = list(raw_ids)
    else:
        raise AssessorError("Row IDs must be a string or sequence of strings")
    if command.operates_on != MedImage.dataset and (not ids or len(ids) != 1):
        raise AssessorError(
            "Assessor output mode requires exactly one session or subject ID"
        )
    if command.operates_on == MedImage.dataset and ids:
        raise AssessorError("Dataset-level commands must not specify row IDs")
    store_name, project, _ = FrameSet.parse_id_str(address)
    work = Path(
        kwargs.get("work_dir") or tempfile.mkdtemp(prefix="pydra2app-assessor-")
    )
    cache = work / "store-cache"
    cache.mkdir(parents=True, exist_ok=True)
    store = Store.load(store_name, cache_dir=cache)
    if not isinstance(store, AssessorOutputStore):
        raise AssessorError("The image must be rebuilt with assessor_outputs enabled")
    with store.connection:
        xlogin = store.connection
        if not supports_assessors(xlogin):
            logger.warning("XNAT < 1.10: using legacy output storage")
            command.execute(address, **{**kwargs, "row_frequency": command.operates_on})
            return
        form_id = os.environ.get("PYDRA2APP_ASSESSOR_FORM")
        if not form_id:
            raise AssessorError(
                "Assessor definitions are not deployed; reinstall this command "
                "with install-command or deploy-pipelines"
            )
        workflow_id = os.environ.get("XNAT_WORKFLOW_ID", "").strip()
        if not workflow_id:
            raise AssessorError(
                "Container Service did not supply XNAT_WORKFLOW_ID; the launch "
                "URL root must be the wrapper's external-input name "
                "(e.g. root/SESSION), not an xsi:type"
            )
        xproject = xlogin.projects[project]
        if command.operates_on == MedImage.dataset:
            parent_id = parent_label = project
        else:
            parent = (
                xproject.experiments[ids[0]]
                if command.operates_on == MedImage.session
                else xproject.subjects[ids[0]]
            )
            parent_id, parent_label = parent.id, parent.label
            kwargs["ids"] = [parent_label]
    requested = dict(kwargs.get("output_values") or {})
    outputs = {}
    for sink in command.sinks:
        if value := requested.get(sink.name):
            path, _ = command.extract_qualifiers_from_path(value)
            outputs[sink.name] = path.split("@", 1)[0]
    # A fresh directory avoids picking up files left by a previous failed invocation.
    staging = Path(tempfile.mkdtemp(prefix="assessor-", dir=work))
    command.execute(
        address,
        **{
            **kwargs,
            "work_dir": work,
            "assessor_staging_dir": staging,
            "assessor_output_paths": tuple(outputs.values()),
            "row_frequency": command.operates_on,
        },
    )
    records = {
        record["path"].split("@", 1)[0]: record
        for record in _records(staging / "outputs")
    }
    for sink in command.sinks:
        if (
            sink.name in outputs
            and not is_optional(sink.type)
            and outputs[sink.name] not in records
        ):
            raise AssessorError(f"Required output {sink.name!r} was not produced")
    with store.connection:
        assessor_id = finalise_assessor(
            store.connection,
            assessor_definition(command),
            project,
            parent_id,
            parent_label,
            staging,
            outputs,
            form_id,
            {
                "version": str(command.image.version),
                "image": os.environ.get("PYDRA2APP_IMAGE", command.image.reference),
                "parameters": {
                    "configuration": command.configuration,
                    "launch": dict(kwargs.get("parameter_values") or {}),
                },
                "workflow": workflow_id,
            },
        )
    logger.info("Finalised pipeline outputs in assessor %s", assessor_id)
