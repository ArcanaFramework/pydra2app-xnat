from __future__ import annotations

import re
import typing as ty

import attrs
from fileformats.core import to_mime
from frametree.axes.medimage import MedImage
from frametree.core.axes import Axes
from frametree.core.utils import path2label
from frametree.xnat import XnatViaCS
from pydra.utils.typing import is_fileset_or_union, is_optional, optional_type

from pydra2app.core.command.base import ContainerCommand
from pydra2app.core.exceptions import Pydra2AppUnresolvedTaskError
from pydra2app.core.utils import logger

if ty.TYPE_CHECKING:
    from .image import XnatApp


@attrs.define(kw_only=True, auto_attribs=False)
class XnatCommand(ContainerCommand):  # type: ignore[misc]

    image: XnatApp = attrs.field(default=None)
    internal_upload: bool = attrs.field(default=False)

    # Hard-code the axes of XNAT commands to be medimage
    AXES: ty.Optional[ty.Type[Axes]] = MedImage

    def make_json(self) -> ty.Dict[str, ty.Any]:
        """Constructs the XNAT CS "command" JSON config, which specifies how XNAT
        should handle the containerised pipeline

        Returns
        -------
        dict
            XNAT container service command specification in JSON-like dict, which can be
            stored within the "org.nrg.commands" label of the container to allow the
            images to be automatically recognised.

        Raises
        ------
        Pydra2AppUnresolvedTaskError
            if the command's task can't be imported in the current environment, and
            therefore the inputs/outputs of the command can't be introspected
        """
        if self.deferred:
            raise Pydra2AppUnresolvedTaskError(
                f"Cannot generate the command JSON for '{self.name}' because its task, "
                f"{self.task}, can't be imported in the current environment"
            )

        cmd_json = self.init_command_json()

        input_args = self.add_input_fields(cmd_json)

        param_args = self.add_parameter_fields(cmd_json)

        output_args = self.add_output_fields(cmd_json)

        flag_arg = self.add_pydra2app_flags_field(cmd_json)

        xnat_input_args = self.add_inputs_from_xnat(cmd_json)

        cmd_json["command-line"] = " ".join(
            self.image.activate_conda()
            + ["pydra2app", "ext", "xnat", "cs-entrypoint", "xnat-cs//[PROJECT_ID]"]
            + input_args
            + output_args
            + param_args
            + xnat_input_args
            + ["--command", self.name, flag_arg],
        )

        return cmd_json

    def init_command_json(self) -> ty.Dict[str, ty.Any]:
        """Initialises the command JSON that specifies to the XNAT Cs how the command
        should be run

        Returns
        -------
        ty.Dict[str, *]
            the JSON-like dictionary to specify the command to the XNAT CS
        """

        cmd_json = {
            "name": f"{self.image.name}.{self.name}",
            "description": (f"{self.name} {self.image.version}: {self.image.title}"),
            "label": f"{self.name} ({self.image.version})",
            "schema-version": "1.0",
            "image": self.image.reference,
            "index": self.image.registry,
            "datatype": "docker",
            # "command-line": cmdline,
            "override-entrypoint": True,
            "mounts": [
                {"name": "in", "writable": False, "path": str(XnatViaCS.INPUT_MOUNT)},
                {"name": "out", "writable": True, "path": str(XnatViaCS.OUTPUT_MOUNT)},
                {  # Saves the Pydra-cache directory outside of the container for easier debugging
                    "name": "work",
                    "writable": True,
                    "path": str(XnatViaCS.WORK_MOUNT),
                },
            ],
            "ports": {},
            "inputs": [],  # inputs_json,
            "outputs": [],  # outputs_json,
            "xnat": [
                {
                    "name": self.name,
                    "description": (
                        f"{self.image.title} {self.image.version} ({self.name})"
                        if len(self.image.commands) > 1
                        else f"{self.image.title} {self.image.version}"
                    ),
                    "contexts": [],  # context,
                    "external-inputs": [],  # external_inputs,
                    "derived-inputs": [],  # derived_inputs,
                    "output-handlers": [],  # output_handlers,
                }
            ],
        }

        return cmd_json

    def add_input_fields(self, cmd_json: ty.Dict[str, ty.Any]) -> ty.List[str]:
        """Adds pipeline inputs to the command JSON

        Parameters
        ----------
        cmd_json : dict
            JSON-like dictionary to be passed to the XNAT container service to specify
            how to run a command

        Returns
        -------
        list[str]
            list of arguments to be appended to the command line
        """
        # Add task inputs to inputs JSON specification
        cmd_args = []
        for src in self.sources:
            replacement_key = f"[{src.name.upper()}_INPUT]"
            if is_fileset_or_union(src.type):
                column_datatype = src.type
                desc = (
                    f"Match resource ({to_mime(column_datatype, official=False)}) "
                    f"[SCAN-TYPE]: {src.help} "
                )
                input_type = "string"
            else:
                desc = f"Match field ({src.type}) [FIELD-NAME]: {src.help} "
                input_type = self.COMMAND_INPUT_TYPES.get(src.type, "string")
            cmd_json["inputs"].append(
                {
                    "name": self.path2xnatname(src.name),
                    "description": desc,
                    "type": input_type,
                    "default-value": f"<{src.name}>",  # the column of the same name
                    "required": src.mandatory,
                    "user-settable": True,
                    "replacement-key": replacement_key,
                }
            )
            cmd_args.append(f"--input {src.name} '{replacement_key}'")

        return cmd_args

    def add_parameter_fields(self, cmd_json: ty.Dict[str, ty.Any]) -> ty.List[str]:

        # Add parameters as additional inputs to inputs JSON specification
        cmd_args = []
        for param in self.parameters:

            desc = f"Parameter ({param.type}): " + param.help

            replacement_key = f"[{param.name.upper()}_PARAM]"

            cmd_json["inputs"].append(
                {
                    "name": param.name,
                    "description": desc,
                    "type": self.COMMAND_INPUT_TYPES.get(param.type, "string"),
                    "default-value": (
                        param._field_object.default
                        if param._field_object.default
                        and not hasattr(param._field_object.default, "factory")
                        else ""
                    ),
                    "required": param.mandatory,
                    "user-settable": True,
                    "replacement-key": replacement_key,
                }
            )
            cmd_args.append(f"--parameter {param.name} '{replacement_key}'")

        return cmd_args

    def add_output_fields(self, cmd_json: ty.Dict[str, ty.Any]) -> ty.List[str]:

        input_names = [i["name"] for i in cmd_json["inputs"]]
        # Set up output handlers and arguments
        cmd_args = []
        for sink in self.sinks:
            if not is_fileset_or_union(sink.type):
                logger.debug("Skipping sink %s, not a fileset", sink.name)
                continue
            sink_type = (
                optional_type(sink.type) if is_optional(sink.type) else sink.type
            )
            out_fname = sink.name + (sink_type.ext if sink_type.ext else "")

            desc = (
                f"Output ({to_mime(sink_type, official=False)}"
                + (", optional" if is_optional(sink.type) else "")
                + "): "
                + sink.help
            )
            # Set the path to the
            if self.internal_upload and not is_optional(sink.type):
                cmd_json["outputs"].append(
                    {
                        "name": sink.name,
                        "description": desc,
                        "required": True,
                        "mount": "out",
                        "path": out_fname,
                        "glob": None,
                    }
                )
                cmd_json["xnat"][0]["output-handlers"].append(
                    {
                        "name": f"{sink.name}-resource",
                        "accepts-command-output": sink.name,
                        "via-wrapup-command": None,
                        "as-a-child-of": "SESSION",
                        "type": "Resource",
                        # Shame that the "label" sink is fixed, would be good to append
                        # the "dataset_name" to it as we do in the API put. Might be worth
                        # just dropping XNAT outputs and just using API
                        "label": path2label(sink.name),
                        "format": sink_type.mime_like,
                    }
                )
                cmd_args.append(f"--output {sink.name} '{sink.name}'")
            else:
                if sink.name in input_names:
                    raise ValueError(
                        "Cannot create output field with the same name as an input "
                        f"{sink.name!r}, you can work around this problem by fixing "
                        "the input field in the command's configuration if it is ok to "
                        "fix."
                    )
                replacement_key = f"[{sink.name.upper()}_OUTPUT]"
                cmd_json["inputs"].append(
                    {
                        "name": sink.name,
                        "description": desc,
                        "type": self.COMMAND_INPUT_TYPES.get(sink_type, "string"),
                        "default-value": sink.name,
                        "required": False,
                        "user-settable": True,
                        "replacement-key": replacement_key,
                    }
                )
                cmd_args.append(f"--output {sink.name} '{replacement_key}'")

        if self.internal_upload:
            cmd_args.append("--internal-upload")

        return cmd_args

    def add_pydra2app_flags_field(self, cmd_json: ty.Dict[str, ty.Any]) -> str:

        # Add input for dataset name
        FLAGS_KEY = "#PYDRA2APP_FLAGS#"
        cmd_json["inputs"].append(
            {
                "name": "pydra2app_flags",
                "description": "Flags passed to `run-pydra2app-pipeline` command",
                "type": "string",
                "default-value": (
                    "--worker cf "
                    "--work /wl "  # noqa NB: work dir moved inside container due to file-locking issue on some mounted volumes (see https://github.com/tox-dev/py-filelock/issues/147)
                    "--dataset-name default "
                    f"--export-work {XnatViaCS.WORK_MOUNT} "
                    "--logger frametree info "
                    "--logger frametree-xnat info "
                    "--logger pydra2app info "
                    "--logger pydra2app-xnat info "
                ),
                "required": False,
                "user-settable": True,
                "replacement-key": FLAGS_KEY,
            }
        )

        return FLAGS_KEY

    def add_inputs_from_xnat(self, cmd_json: ty.Dict[str, ty.Any]) -> ty.List[str]:

        # Define the fixed subject>session dataset hierarchy of XNAT, i.e. the data
        # tree contains two levels, one for subjects and the other for sessions
        cmd_args = ["--dataset-hierarchy subject,session"]

        # Create Project input that can be passed to the command line, which will
        # be populated by inputs derived from the XNAT object passed to the pipeline
        cmd_json["inputs"].append(
            {
                "name": "PROJECT_ID",
                "description": "Project ID",
                "type": "string",
                "required": True,
                "user-settable": False,
                "replacement-key": "[PROJECT_ID]",
            }
        )

        # Access session via Container service args and derive
        if self.operates_on == MedImage.session:
            # Set the object the pipeline is to be run against
            cmd_json["xnat"][0]["contexts"] = ["xnat:imageSessionData"]
            # Create Session input that  can be passed to the command line, which
            # will be populated by inputs derived from the XNAT session object
            # passed to the pipeline.
            cmd_json["inputs"].extend(
                [
                    {
                        "name": "SESSION_LABEL",
                        "description": "Imaging session label",
                        "type": "string",
                        "required": True,
                        "user-settable": False,
                        "replacement-key": "[SESSION_LABEL]",
                    },
                    {
                        "name": "SUBJECT_LABEL",
                        "description": "Subject label",
                        "type": "string",
                        "required": True,
                        "user-settable": False,
                        "replacement-key": "[SUBJECT_LABEL]",
                    },
                ]
            )

            # Access the session XNAT object passed to the pipeline
            cmd_json["xnat"][0]["external-inputs"] = [
                {
                    "name": "SESSION",
                    "description": "Imaging session",
                    "type": "Session",
                    "source": None,
                    "default-value": None,
                    "required": True,
                    "replacement-key": None,
                    "sensitive": None,
                    "provides-value-for-command-input": None,
                    "provides-files-for-command-mount": "in",
                    "via-setup-command": None,
                    "user-settable": False,
                    "load-children": True,
                }
            ]
            # Access to project ID and session label from session XNAT object
            cmd_json["xnat"][0]["derived-inputs"] = [
                {
                    "name": "__SESSION_LABEL__",
                    "type": "string",
                    "derived-from-wrapper-input": "SESSION",
                    "derived-from-xnat-object-property": "label",
                    "provides-value-for-command-input": "SESSION_LABEL",
                    "user-settable": False,
                },
                {
                    "name": "__SUBJECT_ID__",
                    "type": "string",
                    "derived-from-wrapper-input": "SESSION",
                    "derived-from-xnat-object-property": "subject-id",
                    "provides-value-for-command-input": "SUBJECT_LABEL",
                    "user-settable": False,
                },
                {
                    "name": "__PROJECT_ID__",
                    "type": "string",
                    "derived-from-wrapper-input": "SESSION",
                    "derived-from-xnat-object-property": "project-id",
                    "provides-value-for-command-input": "PROJECT_ID",
                    "user-settable": False,
                },
            ]

            # Add specific session to process to command line args
            cmd_args.extend(
                [
                    "--ids [SESSION_LABEL]",
                    # "--single-row [SUBJECT_LABEL],[SESSION_LABEL]",
                ]
            )

        else:
            raise NotImplementedError(
                "Wrapper currently only supports session-level pipelines"
            )

        return cmd_args

    @classmethod
    def path2xnatname(cls, path: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]+", "_", path)

    COMMAND_INPUT_TYPES = {bool: "bool", str: "string", int: "number", float: "number"}
    VALID_FREQUENCIES = (MedImage.session, MedImage.dataset)
