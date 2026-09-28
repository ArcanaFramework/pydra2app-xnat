import yaml
import xnat
from pydra2app.xnat.cli.deploy import (
    save_token,
)
from frametree.core.utils import show_cli_trace


def test_save_token(xnat_repository, work_dir, cli_runner):

    auth_path = work_dir / "auth.json"

    result = cli_runner(
        save_token,
        [
            "--auth-file",
            str(auth_path),
            "--server",
            xnat_repository.server,
            "--user",
            "admin",
            "--password",
            "admin",
        ],
    )

    assert result.exit_code == 0, show_cli_trace(result)

    with open(auth_path) as f:
        auth = yaml.load(f, Loader=yaml.Loader)

    assert len(auth["alias"]) > 20
    assert len(auth["secret"]) > 20

    assert xnat.connect(
        xnat_repository.server, user=auth["alias"], password=auth["secret"]
    )
