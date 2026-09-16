import json
import typing as ty
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path

import docker
import pytest
import yaml
from frametree.core.serialize import ClassResolver
from frametree.core.utils import show_cli_trace

from pydra2app.core.exceptions import Pydra2AppUnresolvedTaskError
from pydra2app.xnat.cli.deploy import make_command_json
from pydra2app.xnat.image import XnatApp

# A task from a package that is published on PyPI (and therefore installable within the
# image) but is unlikely to be installed in the environment the image is built from.
# Since the task can't be imported on the build host, the command JSON it describes can
# only be generated inside the image, once the package has been installed in it
UNRESOLVABLE_TASK_PACKAGE = "pydra-tasks-afni"
UNRESOLVABLE_TASK = "pydra.tasks.afni.v25.preprocess.automask:Automask"
# 'simplejson' is imported by the AFNI tasks but isn't declared as a dependency of the
# package, so it needs to be installed in the image alongside it
UNRESOLVABLE_TASK_PIP_PACKAGES = [UNRESOLVABLE_TASK_PACKAGE, "simplejson"]
# The sources, sinks and parameters of the unresolvable task, explicitly specified so
# that they can only be applied to the task within the image
UNRESOLVABLE_COMMAND_SPEC = {
    "task": UNRESOLVABLE_TASK,
    "operates_on": "medimage/session",
    # 'outputtype' is hard-coded as it doesn't map onto an XNAT command input type
    "configuration": {"outputtype": "NIFTI_GZ"},
    "sources": {"in_file": {"help": "the image to generate the mask from"}},
    "sinks": {"out_file": {"type": "medimage/nifti-gz"}},
    "parameters": ["clfrac", "dilate"],
}
ORG = "an-org"


def image_spec(
    command_spec: ty.Dict[str, ty.Any], **kwargs: ty.Any
) -> ty.Dict[str, ty.Any]:
    """Wrap a command spec in the specification of the image that contains it"""
    spec = {
        "title": "a command to test the generation of command JSONs",
        "version": "1.0",
        "commands": {"test-command": command_spec},
        "authors": [{"name": "Some One", "email": "some.one@an.email.org"}],
        "docs": {
            "info_url": "http://a-command.readthefakedocs.io",
        },
    }
    spec.update(kwargs)
    return spec


def save_spec(spec: ty.Dict[str, ty.Any], spec_path: Path) -> Path:
    with open(spec_path, "w") as f:
        yaml.dump(spec, f)
    return spec_path


def test_make_command_json(
    command_spec: ty.Dict[str, ty.Any],
    work_dir: Path,
    cli_runner: ty.Callable[..., ty.Any],
) -> None:
    """The JSON written by the CLI should match the one generated in-process"""

    spec_path = save_spec(image_spec(command_spec), work_dir / "test-image.yaml")
    # Nest the output within a directory that doesn't exist yet, as it won't within the
    # image being built either
    output_path = work_dir / "xnat_commands" / "test-command.json"

    result = cli_runner(
        make_command_json, [str(spec_path), "test-command", str(output_path)]
    )

    assert result.exit_code == 0, show_cli_trace(result)

    with open(output_path) as f:
        command_json = json.load(f)

    assert command_json == XnatApp.load(spec_path).command("test-command").make_json()
    assert command_json["name"] == "test-image.test-command"
    assert [i["name"] for i in command_json["inputs"]] == [
        "in_file1",
        "in_file2",
        "duplicates",
        "out_file",
        "pydra2app_flags",
        "PROJECT_ID",
        "SESSION_LABEL",
        "SUBJECT_LABEL",
    ]


def test_make_command_json_unrecognised_command(
    command_spec: ty.Dict[str, ty.Any],
    work_dir: Path,
    cli_runner: ty.Callable[..., ty.Any],
) -> None:
    """Referencing a command that isn't in the spec should fail, not write an empty JSON"""

    spec_path = save_spec(image_spec(command_spec), work_dir / "test-image.yaml")
    output_path = work_dir / "test-command.json"

    result = cli_runner(
        make_command_json, [str(spec_path), "not-a-command", str(output_path)]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, KeyError)
    assert not output_path.exists()


@pytest.mark.skipif(
    find_spec("pydra.tasks.afni") is not None,
    reason=(
        f"'{UNRESOLVABLE_TASK_PACKAGE}' is installed in the test environment, so its "
        "tasks can be resolved on the build host and the command JSON doesn't need to "
        "be generated within the image"
    ),
)
class TestUnresolvableTask:
    """Tests of images containing commands that wrap tasks that can't be imported on the
    build host, and therefore can only have their command JSON generated inside the
    image being built, where the package that provides the task is installed"""

    @pytest.fixture
    def unresolvable_image_spec(self) -> ty.Dict[str, ty.Any]:
        return image_spec(
            deepcopy(UNRESOLVABLE_COMMAND_SPEC),
            org=ORG,
            packages={"pip": UNRESOLVABLE_TASK_PIP_PACKAGES},
        )

    @pytest.fixture
    def unresolvable_app(
        self, unresolvable_image_spec: ty.Dict[str, ty.Any], run_prefix: str
    ) -> XnatApp:
        # `pydra2app make` loads specs within this context, so that tasks that can't be
        # imported are left as the address they were specified by instead of raising
        with ClassResolver.FALLBACK_TO_STR:
            return XnatApp.load(
                unresolvable_image_spec,
                name=run_prefix + "-unresolvable-task",
            )

    def test_command_json_cant_be_generated_on_build_host(
        self, unresolvable_app: XnatApp
    ) -> None:

        command = unresolvable_app.command("test-command")

        assert command.deferred
        assert command.task == UNRESOLVABLE_TASK
        with pytest.raises(Pydra2AppUnresolvedTaskError, match="can't be imported"):
            command.make_json()

    def test_definitions_are_saved_without_loss(
        self, unresolvable_app: XnatApp, work_dir: Path
    ) -> None:
        """The sources/sinks/parameters of an unresolvable task can't be matched against
        its fields on the build host, so they are held exactly as they were specified in
        order to be saved back into the spec that is inserted in the image"""

        command = unresolvable_app.command("test-command")

        assert command.sources == UNRESOLVABLE_COMMAND_SPEC["sources"]
        assert command.sinks == UNRESOLVABLE_COMMAND_SPEC["sinks"]
        assert command.parameters == UNRESOLVABLE_COMMAND_SPEC["parameters"]

        spec_path = work_dir / "saved-spec.yaml"
        unresolvable_app.save(spec_path)
        with open(spec_path) as f:
            saved_spec = yaml.load(f, Loader=yaml.SafeLoader)

        saved_command = saved_spec["commands"][0]
        for field in ("task", "configuration", "sources", "sinks", "parameters"):
            assert saved_command[field] == UNRESOLVABLE_COMMAND_SPEC[field]
        assert saved_spec["org"] == ORG

    def test_command_json_generation_added_to_dockerfile(
        self, unresolvable_app: XnatApp, work_dir: Path
    ) -> None:
        """Instead of being copied into the image, the command JSON should be generated
        by a step of the image build, after the spec and the task's package have been
        installed in it"""

        build_dir = work_dir / "build"
        build_dir.mkdir()

        dockerfile = unresolvable_app.construct_dockerfile(
            build_dir, use_local_packages=True, pypi_fallback=True
        )
        instructions = dockerfile.render().splitlines()

        def index_of(snippet: str) -> int:
            return next(i for i, ln in enumerate(instructions) if snippet in ln)

        make_json_line = index_of(
            "pydra2app ext xnat make-command-json "
            f"{unresolvable_app.IN_DOCKER_SPEC_PATH} test-command "
            "/xnat_commands/test-command.json"
        )
        # The task's package needs to be installed and the spec copied into the image
        # before the command JSON can be generated from them
        assert index_of(f'"{UNRESOLVABLE_TASK_PACKAGE}"') < make_json_line
        assert index_of(unresolvable_app.IN_DOCKER_SPEC_PATH + '"') < make_json_line

        # The org is passed explicitly so the image reference in the generated JSON
        # doesn't depend on how it is saved within the spec
        assert f"--org {ORG}" in instructions[make_json_line]

        # The command JSON isn't available to be copied in or added to the label
        assert not (build_dir / "xnat_commands" / "test-command.json").exists()
        assert 'org.nrg.commands="[]"' in instructions[index_of("org.nrg.commands")]

        # The task address is saved in the spec so it can be resolved within the image
        with open(build_dir / "pydra2app-spec.yaml") as f:
            saved_spec = yaml.load(f, Loader=yaml.SafeLoader)
        assert saved_spec["commands"][0]["task"] == UNRESOLVABLE_TASK


@pytest.mark.skipif(
    find_spec("pydra.tasks.afni") is not None,
    reason=f"'{UNRESOLVABLE_TASK_PACKAGE}' is installed in the test environment",
)
def test_command_json_generated_within_image(work_dir: Path, run_prefix: str) -> None:
    """Builds an image containing a command that wraps a task from a package that isn't
    installed on the build host, and checks that the command JSON, which could only be
    generated once the package was installed within the image, is present in it"""

    spec = image_spec(
        deepcopy(UNRESOLVABLE_COMMAND_SPEC),
        org=ORG,
        packages={"pip": UNRESOLVABLE_TASK_PIP_PACKAGES},
    )

    with ClassResolver.FALLBACK_TO_STR:
        app = XnatApp.load(spec, name=run_prefix + "-unresolvable-task-build")

    app.make(
        build_dir=work_dir / "build",
        pydra2app_install_extras=["test"],
        use_local_packages=True,
        pypi_fallback=True,  # the task's package needs to be pulled down from PyPI
    )

    dclient = docker.from_env()
    try:
        command_json = json.loads(
            dclient.containers.run(
                app.reference,
                ["cat", "/xnat_commands/test-command.json"],
                remove=True,
            )
        )
    finally:
        dclient.images.remove(app.reference, force=True)

    assert command_json["name"] == f"{app.name}.test-command"
    # The org is only referenced in the image the command runs, and is the one passed
    # to the CLI rather than one inferred from the location of the spec in the image
    assert command_json["image"] == app.reference == f"{ORG}/{app.name}:1.0"
    # The sources, sinks and parameters explicitly specified in the spec are the ones
    # applied to the task within the image, i.e. not all of the task's fields, which
    # shows they survived being saved into the spec inserted in the image
    assert [i["name"] for i in command_json["inputs"]] == [
        "in_file",
        "clfrac",
        "dilate",
        "out_file",
        "pydra2app_flags",
        "PROJECT_ID",
        "SESSION_LABEL",
        "SUBJECT_LABEL",
    ]
    in_file = next(i for i in command_json["inputs"] if i["name"] == "in_file")
    assert "the image to generate the mask from" in in_file["description"]
    assert "pydra2app ext xnat cs-entrypoint" in command_json["command-line"]
