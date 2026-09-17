from pathlib import Path
import random
from copy import deepcopy
import pytest
from conftest import (
    TEST_XNAT_DATASET_BLUEPRINTS,
    TestXnatDatasetBlueprint,
    ScanBP,
    FileBP,
    access_dataset,
)
from frametree.xnat import Xnat
from pydra2app.xnat.image import XnatApp
from pydra2app.xnat.command import XnatCommand
from pydra2app.xnat.deploy import (
    install_and_launch_xnat_cs_command,
)
from fileformats.medimage import NiftiGzX, NiftiGzXBvec
from fileformats.text import Plain as Text
from frametree.axes.medimage import MedImage


PIPELINE_NAME = "test-concatenate"


@pytest.fixture(
    params=["func_api", "bidsapp_api", "func_internal", "bidsapp_internal"],
    scope="session",
)
def run_spec(
    command_spec,
    bids_command_spec,
    xnat_repository,
    xnat_archive_dir,
    request,
    nifti_sample_dir,
    mock_bids_app_image,
    run_prefix,
):
    spec = {}
    task, upload_method = request.param.split("_")
    run_prefix += upload_method
    access_method = "cs" + ("_internal" if upload_method == "internal" else "")
    if task == "func":
        cmd_spec = deepcopy(command_spec)
        spec["build"] = {
            "org": "pydra2app-tests",
            "name": run_prefix + "-concatenate-xnat-cs",
            "version": "1.0",
            "title": "A pipeline to test Pydra2App's deployment tool",
            "commands": {"concatenate-test": cmd_spec},
            "authors": [{"name": "Some One", "email": "some.one@an.email.org"}],
            "docs": {
                "info_url": "http://concatenate.readthefakedocs.io",
            },
            "readme": "This is a test README",
            "registry": "a.docker.registry.io",
            "packages": {
                "system": ["git", "vim"],
                "pip": [
                    "fileformats",
                    "fileformats-extras",
                    "fileformats-medimage",
                    "fileformats-medimage-extras",
                    "frametree",
                    "frametree-xnat",
                    "pydra",
                    "pydra2app",
                    "pydra2app-xnat",
                ],
            },
        }
        blueprint = TEST_XNAT_DATASET_BLUEPRINTS["concatenate_test"]
        project_id = run_prefix + "concatenate_test"
        blueprint.make_dataset(
            store=xnat_repository,
            dataset_id=project_id,
        )
        spec["dataset"] = access_dataset(
            project_id, access_method, xnat_repository, xnat_archive_dir, run_prefix
        )
        spec["params"] = {"duplicates": 2}
    elif task == "bidsapp":
        cmd_spec = deepcopy(bids_command_spec)
        cmd_spec["configuration"] = {"app": "/launch.sh"}
        spec["build"] = {
            "org": "pydra2app-tests",
            "name": run_prefix + "-bids-app-xnat-cs",
            "version": "1.0",
            "title": "A pipeline to test wrapping of BIDS apps",
            "base_image": {
                "name": mock_bids_app_image,
                "package_manager": "apt",
            },
            "packages": {
                "system": ["git", "vim"],
                "pip": [
                    "fileformats",
                    "fileformats-extras",
                    "fileformats-medimage",
                    "fileformats-medimage-extras",
                    "frametree",
                    "frametree-bids",
                    "frametree-xnat",
                    "pydra",
                    "pydra2app",
                    "pydra2app-xnat",
                    "pydra-compose-bidsapp",
                ],
            },
            "commands": {"bids-test-command": cmd_spec},
            "authors": [
                {"name": "Some One Else", "email": "some.oneelse@an.email.org"}
            ],
            "docs": {
                "info_url": "http://a-bids-app.readthefakedocs.io",
            },
            "readme": "This is another test README for BIDS app image",
            "registry": "another.docker.registry.io",
        }
        blueprint = TestXnatDatasetBlueprint(
            dim_lengths=[1, 1, 1],
            scans=[
                ScanBP(
                    "anat/T1w",
                    [
                        FileBP(
                            path="NiftiGzX",
                            datatype=NiftiGzX,
                            filenames=["anat/T1w.nii.gz", "anat/T1w.json"],
                        )
                    ],
                ),
                ScanBP(
                    "anat/T2w",
                    [
                        FileBP(
                            path="NiftiGzX",
                            datatype=NiftiGzX,
                            filenames=["anat/T2w.nii.gz", "anat/T2w.json"],
                        )
                    ],
                ),
                ScanBP(
                    "dwi/dwi",
                    [
                        FileBP(
                            path="NiftiGzXBvec",
                            datatype=NiftiGzXBvec,
                            filenames=[
                                "dwi/dwi.nii.gz",
                                "dwi/dwi.json",
                                "dwi/dwi.bvec",
                                "dwi/dwi.bval",
                            ],
                        )
                    ],
                ),
            ],
            derivatives=[
                FileBP(
                    path="file1",
                    row_frequency=MedImage.session,
                    datatype=Text,
                    filenames=["file1_sink.txt"],
                ),
                FileBP(
                    path="file2",
                    row_frequency=MedImage.session,
                    datatype=Text,
                    filenames=["file2_sink.txt"],
                ),
            ],
        )
        project_id = run_prefix + "xnat_cs_bidsapp"
        blueprint.make_dataset(
            store=xnat_repository,
            dataset_id=project_id,
            source_data=nifti_sample_dir,
        )
        spec["dataset"] = access_dataset(
            project_id, access_method, xnat_repository, xnat_archive_dir, run_prefix
        )
        spec["params"] = {}
    else:
        assert False, f"unrecognised request param '{task}'"
    cmd_spec["internal_upload"] = upload_method == "internal"
    return spec


def test_xnat_cs_pipeline(xnat_repository: Xnat, run_spec: dict, work_dir: Path):
    """Tests the complete XNAT deployment pipeline by building and running a
    container"""

    # Retrieve test dataset and build and command specs from fixtures
    build_spec = run_spec["build"]
    dataset = run_spec["dataset"]
    params = run_spec["params"]
    blueprint = dataset.__annotations__["blueprint"]

    image_spec = XnatApp(**build_spec)

    image_spec.make(
        build_dir=work_dir,
        pydra2app_install_extras=["test"],
        use_local_packages=True,
        for_localhost=True,
    )

    # We manually set the command in the test XNAT instance as commands are
    # loaded from images when they are pulled from a registry and we use
    # the fact that the container service test XNAT instance shares the
    # outer Docker socket. Since we build the pipeline image with the same
    # socket there is no need to pull it.

    cmd = image_spec.command()
    xnat_command = cmd.make_json()

    launch_inputs = {}

    for inpt, scan in zip(xnat_command["inputs"], blueprint.scans):
        launch_inputs[XnatCommand.path2xnatname(inpt["name"])] = scan.name

    for pname, pval in params.items():
        launch_inputs[pname] = pval

    launch_inputs["pydra2app_flags"] = (
        "--worker cf "
        "--work /wl "  # noqa NB: work dir moved inside container due to file-locking issue on some mounted volumes (see https://github.com/tox-dev/py-filelock/issues/147)
        "--dataset-name default "
        "--export-work /work "
        "--logger pydra2app debug "
        "--logger frametree debug "
        "--logger frametree-xnat debug "
        "--logger pydra2app-xnat debug "
    )

    if cmd.internal_upload:
        # If using internal upload, the output names are fixed
        output_values = {s: s for s in cmd.sink_names}
    else:
        output_values = {s: s + "_sink" for s in cmd.sink_names}
        launch_inputs.update(output_values)

    with xnat_repository.connection:

        xlogin = xnat_repository.connection

        test_xsession = next(iter(xlogin.projects[dataset.id].experiments.values()))

        workflow_id, status, out_str = install_and_launch_xnat_cs_command(
            command_json=xnat_command,
            project_id=dataset.id,
            session_id=test_xsession.id,
            inputs=launch_inputs,
            xlogin=xlogin,
        )

        assert status == "Complete", f"Workflow {workflow_id} failed.\n{out_str}"

        access_type = "direct" if cmd.internal_upload else "api"

        assert f"via {access_type} access" in out_str.lower()

        assert sorted(r.label for r in test_xsession.resources.values()) == sorted(
            output_values.values()
        )

        for output_name, sinked_name in output_values.items():
            deriv = next(d for d in blueprint.derivatives if d.path == output_name)
            uploaded_files = sorted(
                f.name.lstrip("sub-DEFAULT_")
                for f in test_xsession.resources[sinked_name].files
            )
            if cmd.internal_upload:
                reference = sorted(
                    d.rstrip("_sink.txt") + ".txt" for d in deriv.filenames
                )
            else:
                reference = sorted(deriv.filenames)
            assert uploaded_files == reference


def test_multi_command(xnat_repository: Xnat, tmp_path: Path, run_prefix) -> None:

    bp = TestXnatDatasetBlueprint(
        dim_lengths=[1, 1, 1],
        scans=[
            ScanBP(
                name="scan1",
                resources=[FileBP(path="TEXT", datatype=Text, filenames=["file1.txt"])],
            ),
            ScanBP(
                name="scan2",
                resources=[FileBP(path="TEXT", datatype=Text, filenames=["file2.txt"])],
            ),
        ],
    )

    project_id = run_prefix + "multi_command" + str(hex(random.getrandbits(16)))[2:]

    dataset = bp.make_dataset(
        dataset_id=project_id,
        store=xnat_repository,
        name="",
    )

    two_dup_spec = dict(
        name="concatenate",
        task="frametree.testing.tasks:Concatenate",
        operates_on=MedImage.session.tostr(),
        configuration={"duplicates": 2},
    )

    three_dup_spec = deepcopy(two_dup_spec)
    three_dup_spec["configuration"]["duplicates"] = 3

    test_spec = {
        "name": run_prefix + "test_multi_commands",
        "title": "a test image for multi-image commands",
        "commands": {
            "two_duplicates": two_dup_spec,
            "three_duplicates": three_dup_spec,
        },
        "version": "1.0",
        "packages": {
            "system": ["vim", "git"],  # just to test it out
            "pip": [  # Ensure that development packages are installed if present
                "fileformats",
                "fileformats-extras",
                "fileformats-medimage",
                "fileformats-medimage-extras",
                "frametree",
                "frametree-xnat",
                "pydra",
                "pydra2app",
                "pydra2app-xnat",
            ],
        },
        "authors": [{"name": "Some One", "email": "some.one@an.email.org"}],
        "docs": {
            "info_url": "http://concatenate.readthefakedocs.io",
        },
    }

    app = XnatApp.load(test_spec)

    app.make(
        build_dir=tmp_path / "build-dir",
        pydra2app_install_extras=["test"],
        use_local_packages=True,
        for_localhost=True,
    )

    fnames = ["file1.txt", "file2.txt"]

    base_launch_inputs = {
        "in_file1": "scan1",
        "in_file2": "scan2",
        "pydra2app_flags": "--save-frameset",
    }

    command_names = ["two_duplicates", "three_duplicates"]

    with xnat_repository.connection as xlogin:

        test_xsession = next(iter(xlogin.projects[dataset.id].experiments.values()))
        for command_name in command_names:

            launch_inputs = deepcopy(base_launch_inputs)
            launch_inputs["out_file"] = command_name

            workflow_id, status, out_str = install_and_launch_xnat_cs_command(
                command_json=app.command(command_name).make_json(),
                project_id=project_id,
                session_id=test_xsession.id,
                inputs=launch_inputs,
                xlogin=xlogin,
            )

            assert status == "Complete", f"Workflow {workflow_id} failed.\n{out_str}"

        assert sorted(r.label for r in test_xsession.resources.values()) == sorted(
            command_names
        )

    # Add source column to saved dataset
    reloaded = dataset.reload()
    for command_name in command_names:
        sink = reloaded[command_name]
        duplicates = 2 if command_name == "two_duplicates" else 3
        expected_contents = "\n".join(fnames * duplicates)
        for item in sink:
            with open(item) as f:
                contents = f.read()
            assert contents == expected_contents
