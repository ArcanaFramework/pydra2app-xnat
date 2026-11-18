"""Definitions and deployment of pipeline-specific XNAT assessor types."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import typing as ty
from copy import deepcopy
from textwrap import shorten
from urllib.parse import quote

import xnat
from frametree.axes.medimage import MedImage
from pydra.utils.typing import is_fileset_or_union, optional_type

if ty.TYPE_CHECKING:
    from .command import XnatCommand

logger = logging.getLogger(__name__)

METADATA_KEY = "pydra2app-assessor"
RESERVED_PREFIXES = {"cat", "prov", "icr", "scr", "wrk", "xnat", "xnat_a"}
ASSESSOR_BASES = {
    MedImage.session: "imageAssessorData",
    MedImage.subject: "subjectAssessorData",
    MedImage.dataset: "abstractProjectAsset",
}
PROVENANCE_FIELDS = {
    "pipeline": ("Pipeline", "textfield"),
    "version": ("Pipeline version", "textfield"),
    "image": ("Container image", "textfield"),
    "parameters": ("Launch parameters", "textarea"),
    "inputs": ("Input URIs", "textarea"),
    "workflow": ("Workflow ID", "textfield"),
}


class AssessorError(RuntimeError):
    pass


def _digest(value: ty.Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pipeline_display_name(pipeline: str) -> str:
    return " / ".join(pipeline.rsplit("/", 2)[-2:])


def output_resource_name(name: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("._-") or "output"
    if label != name or len(label) > 100 or label.upper() == "PROVENANCE":
        label = label[:80] + "_" + _digest(name)[:16]
    return label


def assessor_definition(command: XnatCommand) -> dict[str, ty.Any]:
    pipeline = "/".join(
        part
        for part in (
            command.image.registry,
            command.image.org,
            command.image.name,
            command.name,
        )
        if part
    )
    if command.operates_on not in ASSESSOR_BASES:
        raise AssessorError(
            "Assessor outputs require session, subject or dataset frequency"
        )
    identity = _digest([pipeline, command.operates_on.name])
    display = re.sub(r"[^a-zA-Z0-9 ]+", " ", pipeline_display_name(pipeline))
    display = " ".join(display.split())
    if not display or not display[0].isalpha():
        display = "Pipeline " + display
    display = display[0].upper() + display[1:]
    fields = []
    for sink in command.sinks:
        if is_fileset_or_union(sink.type):
            continue
        primitive = optional_type(sink.type)
        if getattr(primitive, "is_field", False):
            primitive = primitive.primitive
        try:
            component = {
                str: "textfield",
                int: "number",
                float: "number",
                bool: "checkbox",
            }[primitive]
        except KeyError:
            raise AssessorError(
                f"Assessor output {sink.name!r} must be a scalar string, number or "
                f"boolean, not {sink.type!r}"
            ) from None
        fields.append(
            {
                "key": "output" + _digest(sink.name)[:16],
                "label": sink.name,
                "type": component,
                **({"description": sink.help} if sink.help else {}),
            }
        )
    return {
        "pipeline": pipeline,
        "prefix": "p" + identity[:5],
        "complexType": "pipeline" + identity[:12] + "Data",
        "name": "Pipeline" + identity[:12],
        "singular": shorten(display, width=24, placeholder="") or display[:24],
        "plural": (shorten(display, width=16, placeholder="") or display[:16])
        + " results",
        "extends": ASSESSOR_BASES[command.operates_on],
        "fields": fields,
    }


def xsi_type(definition: dict[str, ty.Any]) -> str:
    return f"{definition['prefix']}:{definition['complexType']}"


def form_builder(definition: dict[str, ty.Any]) -> dict[str, ty.Any]:
    components = [
        {
            **field,
            "label": field["label"].replace("_", " ").replace("-", " "),
        }
        for field in definition["fields"]
    ] + [
        {"key": key, "label": label, "type": field_type}
        for key, (label, field_type) in PROVENANCE_FIELDS.items()
    ]
    return {
        "display": "form",
        "title": (
            pipeline_display_name(definition["pipeline"])
            + " results ("
            + _digest([definition, components])[:12]
            + ")"
        ),
        "settings": {},
        "components": [
            {**component, "input": True, "tableView": True} for component in components
        ],
    }


def supports_assessors(xlogin: xnat.XNATSession) -> bool:
    version = xlogin.get("/xapi/siteConfig/buildInfo").json().get("version")
    if not isinstance(version, str) or not (
        match := re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    ):
        raise AssessorError(f"Unrecognised XNAT version: {version!r}")
    return tuple(map(int, match.groups())) >= (1, 10)


def validate_definition(definition: dict[str, ty.Any]) -> None:
    prefix = definition.get("prefix")
    if (
        not isinstance(prefix, str)
        or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{1,5}", prefix)
        or prefix.lower() in RESERVED_PREFIXES
    ):
        raise AssessorError(f"Invalid or reserved assessor prefix: {prefix!r}")
    if definition.get("extends") not in ASSESSOR_BASES.values():
        raise AssessorError("Unsupported assessor inheritance")
    if not isinstance(definition.get("pipeline"), str) or not definition["pipeline"]:
        raise AssessorError("Assessor pipeline identity must be a nonempty string")
    for key in ("singular", "plural"):
        if not isinstance(definition.get(key), str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_ ]{1,23}", definition[key]
        ):
            raise AssessorError(f"Invalid assessor {key}: {definition.get(key)!r}")
    for key in ("complexType", "name"):
        if not isinstance(definition.get(key), str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_]{1,23}", definition[key]
        ):
            raise AssessorError(f"Invalid assessor {key}: {definition.get(key)!r}")
    fields = definition.get("fields")
    if not isinstance(fields, list):
        raise AssessorError("Assessor fields must be a list")
    keys = set(PROVENANCE_FIELDS)
    labels = set()
    for field in fields:
        if (
            not isinstance(field, dict)
            or not isinstance(field.get("key"), str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", field["key"])
            or not isinstance(field.get("label"), str)
            or not isinstance(field.get("description", ""), str)
            or field.get("type") not in {"textfield", "number", "checkbox"}
            or field["key"] in keys
            or field["label"] in labels
        ):
            raise AssessorError(f"Invalid or duplicate assessor field: {field!r}")
        keys.add(field["key"])
        labels.add(field["label"])


def find_form(xlogin: xnat.XNATSession, definition: dict[str, ty.Any]) -> str | None:
    builder = form_builder(definition)
    matches = [
        form
        for form in xlogin.get("/xapi/customforms").json()
        if form["path"] == "datatype/" + xsi_type(definition)
        and json.loads(form["contents"]).get("title") == builder["title"]
    ]
    if len(matches) > 1:
        raise AssessorError(f"Multiple forms match {builder['title']!r}")
    if not matches:
        return None
    form = matches[0]
    if json.loads(form["contents"]) != builder:
        raise AssessorError(f"Assessor form {builder['title']!r} has been modified")
    if form["scope"] != "Site" or not any(
        entry["entityId"] == "Site" and entry["status"] == "enabled"
        for entry in form["appliesToList"]
    ):
        raise AssessorError(
            f"Assessor form {builder['title']!r} is not enabled site-wide"
        )
    return ty.cast(str, form["formUUID"])


def ensure_assessor(xlogin: xnat.XNATSession, definition: dict[str, ty.Any]) -> str:
    """Reconcile definitions without modifying existing schemas or forms."""
    validate_definition(definition)
    target = xsi_type(definition)
    schemas = xlogin.get("/xapi/dbschemas").json()
    matches = [schema for schema in schemas if schema["name"] == target]
    for schema in schemas:
        if (
            schema["name"].split(":", 1)[0] == definition["prefix"]
            and schema["name"] != target
        ):
            raise AssessorError(f"Assessor prefix collision for {target!r}")
    if matches:
        from xml.etree import ElementTree

        if not matches[0]["enabled"]:
            raise AssessorError(f"Existing type {target!r} is disabled")
        schema = ElementTree.fromstring(matches[0]["content"])
        extension = schema.find(".//{http://www.w3.org/2001/XMLSchema}extension")
        if (
            extension is None
            or extension.get("base") != "xnat:" + definition["extends"]
        ):
            raise AssessorError(f"Existing type {target!r} has different inheritance")
        root = schema.find("{http://www.w3.org/2001/XMLSchema}element")
        if root is None or root.get("name") != definition["name"]:
            raise AssessorError(f"Existing type {target!r} has a different XML root")
    else:
        # Also detect collisions with types provided by plugins.
        response = xlogin.get(
            "/xapi/datatypes/" + quote(target, safe=""), accepted_status=[200, 404]
        )
        if response.status_code == 200:
            raise AssessorError(f"Existing type {target!r} is not managed dynamically")
        xlogin.post(
            "/xapi/datatypes/create",
            query={
                key: definition[key]
                for key in (
                    "prefix",
                    "complexType",
                    "name",
                    "singular",
                    "plural",
                    "extends",
                )
            },
            accepted_status=[201],
        )
    existing = find_form(xlogin, definition)
    if existing is not None:
        return existing
    response = xlogin.put(
        "/xapi/customforms/save",
        json={
            "submission": {
                "data": {
                    "zIndex": 10,
                    "xnatDatatype": {"label": definition["singular"], "value": target},
                    "isThisASiteWideConfiguration": "yes",
                    "xnatProject": [],
                }
            },
            "builder": form_builder(definition),
        },
        accepted_status=[201],
    )
    return ty.cast(str, response.text.strip())


def prepare_assessor_command(
    xlogin: xnat.XNATSession, document: dict[str, ty.Any]
) -> dict[str, ty.Any]:
    metadata = document.get("command-metadata", {})
    if not isinstance(metadata, dict):
        raise AssessorError("Command metadata must be a JSON object")
    definition = metadata.get(METADATA_KEY)
    if definition is None:
        return document
    if not isinstance(definition, dict):
        raise AssessorError("Assessor definition must be a JSON object")
    validate_definition(definition)
    prepared = deepcopy(document)
    environment = prepared.setdefault("environment-variables", {})
    environment["PYDRA2APP_IMAGE"] = document["image"]
    if supports_assessors(xlogin):
        environment["PYDRA2APP_ASSESSOR_FORM"] = ensure_assessor(xlogin, definition)
    else:
        logger.warning(
            "XNAT < 1.10: %s will use legacy output storage", document["name"]
        )
        environment["PYDRA2APP_ASSESSOR_FORM"] = ""
    return prepared
