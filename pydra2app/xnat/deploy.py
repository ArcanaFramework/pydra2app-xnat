import typing as ty
import time
import logging
import json
import xnat
from pydra2app.core.exceptions import Pydra2AppError
from pydra2app.core.utils import extract_file_from_docker_image

logger = logging.getLogger("pydra2app-xnat")

INTERNAL_INPUTS = ("pydra2app_flags", "PROJECT_ID", "SUBJECT_LABEL", "SESSION_LABEL")


def install_cs_command(
    image_name_or_command_json: ty.Union[str, ty.Dict[str, ty.Any]],
    xlogin: xnat.XNATSession,
    enable: bool = False,
    projects_to_enable: ty.Sequence[str] = (),
    replace_existing: bool = False,
    command_name: ty.Optional[str] = None,
) -> int:
    """Installs a new command for the XNAT container service and lanches it on
    the specified session.

    Parameters
    ----------
    command_json : ty.Dict[str, Any]
        JSON that defines the XNAT command in the container service (see `generate_xnat_command`)
    xlogin : xnat.XNATSession
        XnatPy connection to the XNAT server
    enable : bool
        Enable the command globally
    projects_to_enable : ty.Sequence[str]
        ID of the project to enable the command for
    replace_existing : bool
        Whether to replace existing command with the same name
    command_name : str, optional
        the command to install, if an image name is provided instead of a command JSON

    Returns
    -------
    cmd_id : int
        the ID of the installed command
    """
    if isinstance(image_name_or_command_json, str):
        if not command_name:
            raise ValueError(
                "If the first argument of the install_cs_command is a string "
                f"('{image_name_or_command_json}) 'command_name' must be provided"
            )
        command_json_file = extract_file_from_docker_image(
            image_name_or_command_json, f"/xnat_commands/{command_name}.json"
        )
        if command_json_file is None:
            raise RuntimeError(
                f"Could not find {command_name!r} command JSON file in "
                f"{image_name_or_command_json!r}"
            )
        with open(command_json_file) as f:
            command_json = json.load(f)
    elif isinstance(image_name_or_command_json, dict):
        command_json = image_name_or_command_json
    else:
        raise RuntimeError(
            "Unrecognised type of 'image_name_or_command_json' arg: "
            f"{type(image_name_or_command_json)} expected str or dict"
        )

    cmd_name = command_json["name"]
    wrapper_name = command_json["xnat"][0]["name"]

    if replace_existing:
        for cmd in xlogin.get("/xapi/commands").json():
            if cmd["name"] == cmd_name:
                xlogin.delete(f"/xapi/commands/{cmd['id']}", accepted_status=[200, 204])
                logger.info(f"Deleted existing command '{cmd_name}'")

    try:
        cmd_id: int = xlogin.post("/xapi/commands", json=command_json).json()
    except xnat.exceptions.XNATResponseError as e:
        e.add_note(
            f"Attempting to install command '{cmd_name}' with JSON:\n"
            + json.dumps(command_json, indent=4)
        )
        raise e

    # Enable the command globally and in the project
    if enable:
        xlogin.put(f"/xapi/commands/{cmd_id}/wrappers/{wrapper_name}/enabled")
        for project_id in projects_to_enable:
            xlogin.put(
                f"/xapi/projects/{project_id}/commands/{cmd_id}/wrappers/{wrapper_name}/enabled"
            )
    elif projects_to_enable:
        raise RuntimeError(
            "'enable' must be set to True for individual projects to be enabled "
            f"({projects_to_enable})"
        )
    return cmd_id


def launch_cs_command(
    command_id_or_name: ty.Union[int, str],
    project_id: str,
    session_id: str,
    inputs: ty.Dict[str, str],
    xlogin: xnat.XNATSession,
    timeout: int = 1000,  # seconds
    poll_interval: int = 10,  # seconds
) -> ty.Tuple[int, str, str]:
    """Installs a new command for the XNAT container service and lanches it on
    the specified session.

    Parameters
    ----------
    command_id_or_name : ty.Union[int, str]
        the ID (int) or name of the command to launch, or the name of the image containing
        the command (assumed if name includes ':' or '/' characters)
    project_id : str
        ID of the project to enable the command for
    session_id : str
        ID of the session to launch the command on
    inputs : ty.Dict[str, str]
        Inputs passed to the pipeline at launch (i.e. typically through text fields in the CS launch UI)
    xlogin : xnat.XNATSession
        XnatPy connection to the XNAT server
    timeout : int
        the time to wait for the pipeline to complete (seconds)
    poll_interval : int
        the time interval between status polls (seconds)

    Returns
    -------
    workflow_id : int
        the auto-generated ID for the launched workflow
    status : str
        the status of the completed workflow
    out_str : str
        stdout and stderr from the workflow run
    """
    if isinstance(command_id_or_name, int):
        command_json = xlogin.get(f"/xapi/commands/{command_id_or_name}").json()
    else:
        assert isinstance(command_id_or_name, str)
        commands = xlogin.get("/xapi/commands").json()
        if ":" in command_id_or_name or "/" in command_id_or_name:
            # Assume it is the docker image name
            commands = [c for c in commands if c["image"] == command_id_or_name]
        else:
            commands = [c for c in commands if c["name"] == command_id_or_name]
        if not commands:
            raise RuntimeError(
                f"Did not find command corresponding to name or image '{command_id_or_name}'"
            )
        elif len(commands) > 1:
            raise RuntimeError(
                "Found multiple commands corresponding to name or image "
                f"'{command_id_or_name}': {commands}"
            )
        command_json = commands[0]
    cmd_id = command_json["id"]
    cmd_name = command_json["name"]

    launch_json = {
        "SESSION": f"/archive/projects/{project_id}/experiments/{session_id}"
    }

    provided_inputs = [k for k in inputs if k not in INTERNAL_INPUTS]
    input_names = [
        i["name"] for i in command_json["inputs"] if i["name"] not in INTERNAL_INPUTS
    ]
    required_inputs = [
        i["name"]
        for i in command_json["inputs"]
        if i["required"] and i["name"] not in INTERNAL_INPUTS
    ]

    missing_inputs = list(set(required_inputs) - set(provided_inputs))
    unexpected_inputs = list(set(provided_inputs) - set(input_names))
    if missing_inputs or unexpected_inputs:
        raise ValueError(
            f"Error launching '{cmd_name}' command:\n"  # noqa
            f"    Valid inputs: {input_names}\n"
            f"    Provided inputs: {provided_inputs}\n"
            f"    Missing required inputs: {missing_inputs}\n"
            f"    Unexpected inputs: {unexpected_inputs}\n"
        )

    launch_json.update(inputs)

    launch_result = xlogin.post(
        f"/xapi/projects/{project_id}/wrappers/{cmd_id}/root/SESSION/launch",
        json=launch_json,
    ).json()

    if launch_result["status"] != "success":
        raise Pydra2AppError(
            f"{cmd_name} workflow wasn't launched successfully ({launch_result['status']})"
        )
    workflow_id = launch_result["workflow-id"]
    assert workflow_id != "To be assigned"

    num_attempts = (timeout // poll_interval) + 1
    max_runtime = num_attempts * poll_interval

    for i in range(num_attempts):
        wf_result = xlogin.get(f"/xapi/workflows/{workflow_id}").json()
        if wf_result["status"] not in INCOMPLETE_CS_STATES:
            break
        time.sleep(poll_interval)

    if i == num_attempts - 1:
        status = f"NotCompletedAfter{max_runtime}Seconds"
    else:
        status = wf_result["status"]

    # Get logs
    out_str = ""
    container_id = wf_result["comments"]
    if container_id:
        # Get workflow stdout/stderr for error messages if required

        stdout_result = xlogin.get(
            f"/xapi/containers/{container_id}/logs/StdOut.log",
            accepted_status=[200, 204],
        )
        if stdout_result.status_code == 200:
            out_str += f"stdout:\n{stdout_result.text}\n"  # noqa

        stderr_result = xlogin.get(
            f"/xapi/containers/{container_id}/logs/StdErr.log",
            accepted_status=[200, 204],
        )
        if stderr_result.status_code == 200:
            out_str += f"\nstderr:\n{stderr_result.text}"  # noqa

    if status != "Complete":
        raise ValueError(
            f"Launching {cmd_name} in the XNAT CS failed with status {status} "
            f"for inputs={launch_json}:{out_str}"
        )

    return workflow_id, status, out_str


def install_and_launch_xnat_cs_command(
    command_json: ty.Dict[str, ty.Any],
    project_id: str,
    session_id: str,
    inputs: ty.Dict[str, str],
    xlogin: xnat.XNATSession,
    **kwargs: ty.Any,
) -> ty.Tuple[int, str, str]:
    """Installs a new command for the XNAT container service and lanches it on
    the specified session.

    Parameters
    ----------
    command_json : ty.Dict[str, Any]
        JSON that defines the XNAT command in the container service (see `generate_xnat_command`)
    project_id : str
        ID of the project to enable the command for
    session_id : str
        ID of the session to launch the command on
    inputs : ty.Dict[str, str]
        Inputs passed to the pipeline at launch (i.e. typically through text fields in the CS launch UI)
    xlogin : xnat.XNATSession
        XnatPy connection to the XNAT server
    **kwargs:
        Keyword arguments passed directly through to 'launch_cs_command'

    Returns
    -------
    workflow_id : int
        the auto-generated ID for the launched workflow
    status : str
        the status of the completed workflow
    out_str : str
        stdout and stderr from the workflow run
    """

    cmd_id = install_cs_command(
        command_json, xlogin=xlogin, enable=True, projects_to_enable=[project_id]
    )

    return launch_cs_command(
        cmd_id,
        project_id=project_id,
        session_id=session_id,
        inputs=inputs,
        xlogin=xlogin,
        **kwargs,
    )


# List of intermediatary states can pass through
# before completing successfully
INCOMPLETE_CS_STATES = (
    "Pending",
    "Running",
    "_Queued",
    "Queued",
    "Staging",
    "Finalizing",
    "Created",
    "_die",
    "die",
)
