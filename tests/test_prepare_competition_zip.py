from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import prepare_competition_zip as prepare


def test_stream_copy_enforces_actual_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prepare, "MAX_EXTRACTED_BYTES", 4)

    with (
        (tmp_path / "out").open("wb") as target,
        pytest.raises(RuntimeError, match="extraction limit"),
    ):
        prepare._copy_member(BytesIO(b"12345"), target, claimed_size=5, extracted_before=0)

    assert (tmp_path / "out").stat().st_size <= 4


def _archive(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, name.encode())


def test_prepares_only_the_required_dataset_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "competition.zip"
    _archive(
        source,
        [
            "bundle/職缺.csv",
            "bundle/城市對照表.csv",
            "bundle/職務對照表.csv",
            "bundle/search-log.csv",
        ],
    )
    monkeypatch.setattr(prepare, "validate_source", lambda _: None)
    monkeypatch.setattr(prepare, "_taxonomy_csv", lambda *_: ({"1": {"x"}}, {"x"}))
    monkeypatch.setattr(
        prepare,
        "EXPECTED_FILES",
        {
            name: {
                "sha256": hashlib.sha256(f"bundle/{name}".encode()).hexdigest(),
                "size_bytes": len(f"bundle/{name}".encode()),
            }
            for name in prepare.REQUIRED_FILES
        },
    )

    output = tmp_path / "dataset"
    manifest = prepare.prepare(source, output)

    assert sorted(path.name for path in output.iterdir()) == [
        "manifest.json",
        "城市對照表.csv",
        "職務對照表.csv",
        "職缺.csv",
    ]
    assert manifest["complete"] is True
    assert set(manifest["files"]) == set(prepare.REQUIRED_FILES)
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_rejects_changed_taxonomy_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "competition.zip"
    _archive(source, list(prepare.REQUIRED_FILES))
    expected = {
        name: {
            "sha256": hashlib.sha256(name.encode()).hexdigest(),
            "size_bytes": len(name.encode()),
        }
        for name in prepare.REQUIRED_FILES
    }
    expected["城市對照表.csv"] = {"sha256": "0" * 64, "size_bytes": len("城市對照表.csv".encode())}
    monkeypatch.setattr(prepare, "EXPECTED_FILES", expected)

    with pytest.raises(RuntimeError, match=r"城市對照表\.csv bytes differ"):
        prepare.prepare(source, tmp_path / "dataset")


def test_rejects_duplicate_required_basenames(tmp_path: Path) -> None:
    source = tmp_path / "competition.zip"
    _archive(
        source,
        [
            "one/職缺.csv",
            "two/職缺.csv",
            "城市對照表.csv",
            "職務對照表.csv",
        ],
    )

    with pytest.raises(RuntimeError, match="exactly once"):
        prepare.prepare(source, tmp_path / "dataset")


def test_rejects_unsafe_required_member_path(tmp_path: Path) -> None:
    source = tmp_path / "competition.zip"
    _archive(
        source,
        ["../職缺.csv", "城市對照表.csv", "職務對照表.csv"],
    )

    with pytest.raises(RuntimeError, match="unsafe"):
        prepare.prepare(source, tmp_path / "dataset")
