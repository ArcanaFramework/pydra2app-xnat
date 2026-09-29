import click
from pathlib import Path
import typing as ty
from pydra2app.core.command import entrypoint_opts
from pydra2app.xnat import XnatApp
from .base import xnat_group


@xnat_group.command(
    name="cs-entrypoint",
    help="""Loads a dataset, or creates one it is not already present, then applies and
launches a pipeline in a single command. To be used within the command configuration
of an XNAT Container Service ready Docker image.

ADDRESS string containing the nickname of the data store, the ID of the
dataset (e.g. XNAT project ID or file-system directory) and the dataset's name
in the format <store-nickname>//<dataset-id>[@<dataset-name>]

""",
)  # type: ignore[misc]
@click.argument("address")
@entrypoint_opts.data_columns  # type: ignore[untyped-decorator]
@entrypoint_opts.parameterisation  # type: ignore[untyped-decorator]
@entrypoint_opts.execution  # type: ignore[untyped-decorator]
@entrypoint_opts.debugging  # type: ignore[untyped-decorator]
@entrypoint_opts.dataset_config  # type: ignore[untyped-decorator]
@click.option(
    "--internal-upload/--external-upload",
    type=bool,
    default=False,
    help=(
        "Whether to upload the output to the XNAT using the container service's "
        "internal upload mechanism instead of the XNAT REST API"
    ),
)
def cs_entrypoint(
    address: str,
    spec_path: Path,
    command: ty.Optional[str],
    **kwargs: ty.Any,
) -> None:

    image_spec = XnatApp.load(spec_path)

    image_spec.command(command).execute(
        address,
        **kwargs,
    )
