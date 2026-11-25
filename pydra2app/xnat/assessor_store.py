"""Stage command outputs until the pipeline has completed successfully."""

from __future__ import annotations

import json
import typing as ty
from pathlib import Path

import attrs
from fileformats.core import FieldPrimitive, FileSet
from fileformats.field import Decimal
from frametree.core.entry import DataEntry
from frametree.core.row import DataRow
from frametree.core.tree import DataTree
from frametree.xnat import XnatViaCS
from pydra.utils.hash import Cache, register_serializer

from .assessors import _digest


@register_serializer(Decimal)  # type: ignore[untyped-decorator]
def _decimal_bytes(field: Decimal, cache: Cache) -> ty.Iterator[bytes]:
    # Decimal.value is decimal.Decimal, which Pydra's generic object hashing
    # cannot serialize. Preserve its exact representation for task caching.
    yield b"fileformats.field.Decimal:" + str(field.value).encode()


@attrs.define(repr=False)
class AssessorOutputStore(XnatViaCS):  # type: ignore[misc]
    assessor_staging_dir: Path | None = attrs.field(
        default=None, converter=attrs.converters.optional(Path)
    )
    assessor_output_paths: tuple[str, ...] = attrs.field(factory=tuple, converter=tuple)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, row_frequency={self.row_frequency!r})"

    def populate_row(self, row: DataRow) -> None:
        super().populate_row(row)
        if (
            self.assessor_staging_dir is not None
            and row.frequency == self.row_frequency
        ):
            # Loose legacy resources must not make a new assessor run look complete.
            for key, entry in list(row.entries_dict.items()):
                if entry.path.split("@", 1)[0] in self.assessor_output_paths:
                    del row.entries_dict[key]

    def populate_tree(self, tree: DataTree) -> None:
        if self.assessor_staging_dir is None:
            super().populate_tree(tree)
            return
        with self.connection:
            project = self.connection.projects[tree.dataset_id]
            if included := tree.frameset.include.get("session"):
                candidates = [project.experiments[row_id] for row_id in included]
            elif included := tree.frameset.include.get("subject"):
                candidates = [
                    session
                    for row_id in included
                    for session in project.subjects[row_id].experiments.values()
                ]
            else:
                candidates = list(project.experiments.values())
            # Subject assessors are experiments too, but are not imaging rows.
            sessions = sorted(
                (
                    session
                    for session in candidates
                    if isinstance(session, self.connection.classes.ImageSessionData)
                ),
                key=lambda session: (
                    session.parent.label or "",
                    str(session.date or ""),
                    str(session.time or ""),
                    session.label or "",
                ),
            )
            for session in sessions:
                tree.add_leaf(
                    [session.parent.label, session.label],
                    metadata={
                        "session": {
                            "date": (
                                session.date.strftime("%Y%m%d")
                                if session.date
                                else None
                            ),
                            "visit_id": session.visit_id,
                            "age": session.age,
                            "modality": session.modality,
                        }
                    },
                )

    def _record_path(self, entry: DataEntry, category: str) -> Path:
        if self.assessor_staging_dir is None:
            raise RuntimeError("Assessor output staging is not configured")
        # A CS invocation processes exactly one row. Namespace suffixes are
        # assigned by FrameTree, whereas the output names come from the command.
        identity = (
            [entry.row.frequency.name, entry.row.id, entry.path, entry.uri]
            if category == "inputs"
            else entry.path.split("@", 1)[0]
        )
        path = self.assessor_staging_dir / category / (_digest(identity) + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def create_fileset_entry(
        self,
        path: str,
        datatype: type[FileSet],
        row: DataRow,
        order_key: int | str | None = None,
    ) -> DataEntry:
        if self.assessor_staging_dir is None or not DataEntry.path_is_derivative(path):
            return super().create_fileset_entry(path, datatype, row, order_key)
        return row.found_entry(
            path=path, datatype=datatype, uri="staged-assessor/" + _digest(path)
        )

    def post_fileset(
        self, fileset: FileSet, path: str, datatype: type, row: DataRow
    ) -> DataEntry:
        if self.assessor_staging_dir is None:
            return super().post_fileset(fileset, path, datatype, row)
        entry = self.create_fileset_entry(path, datatype, row)
        self.put_fileset(fileset, entry)
        return entry

    def put_fileset(self, fileset: FileSet, entry: DataEntry) -> FileSet:
        if self.assessor_staging_dir is None or not entry.is_derivative:
            return super().put_fileset(fileset, entry)  # type: ignore[no-any-return]
        record = self._record_path(entry, "outputs")
        copied = fileset.copy(
            self.assessor_staging_dir / "files" / record.stem,
            make_dirs=True,
            overwrite=True,
        )
        record.write_text(
            json.dumps(
                {"path": entry.path, "files": [str(path) for path in copied.fspaths]}
            )
        )
        return copied

    def upload_value(self, value: FieldPrimitive, entry: DataEntry) -> None:
        if self.assessor_staging_dir is None or not entry.is_derivative:
            super().upload_value(value, entry)
            return
        self._record_path(entry, "outputs").write_text(
            json.dumps({"path": entry.path, "value": entry.datatype.primitive(value)})
        )

    def get_fileset(self, entry: DataEntry, datatype: type[FileSet]) -> list[Path]:
        if self.assessor_staging_dir is not None:
            record = self._record_path(entry, "outputs")
            if entry.is_derivative and record.exists():
                return [Path(path) for path in json.loads(record.read_text())["files"]]
            self._capture_input(entry)
        return super().get_fileset(entry, datatype)  # type: ignore[no-any-return]

    def download_value(self, entry: DataEntry) -> FieldPrimitive:
        if self.assessor_staging_dir is not None:
            record = self._record_path(entry, "outputs")
            if entry.is_derivative and record.exists():
                return ty.cast(FieldPrimitive, json.loads(record.read_text())["value"])
            self._capture_input(entry)
        return super().download_value(entry)  # type: ignore[no-any-return]

    def _capture_input(self, entry: DataEntry) -> None:
        self._record_path(entry, "inputs").write_text(
            json.dumps({"path": entry.path, "uri": entry.uri})
        )

    def put_provenance(self, provenance: dict[str, ty.Any], entry: DataEntry) -> None:
        if self.assessor_staging_dir is None or not entry.is_derivative:
            super().put_provenance(provenance, entry)
            return
        self._record_path(entry, "provenance").write_text(json.dumps(provenance))

    def get_provenance(self, entry: DataEntry) -> dict[str, ty.Any]:
        if self.assessor_staging_dir is not None and entry.is_derivative:
            path = self._record_path(entry, "provenance")
            if path.exists():
                return ty.cast(dict[str, ty.Any], json.loads(path.read_text()))
            if entry.path.split("@", 1)[0] in self.assessor_output_paths:
                return {}
        return super().get_provenance(entry)  # type: ignore[no-any-return]
