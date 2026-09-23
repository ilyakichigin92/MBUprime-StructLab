"""Resource lookup works in source, installed and frozen layouts."""

from pathlib import Path
import sys

import app_assets
import pytest


def test_installed_package_assets(monkeypatch, tmp_path):
    monkeypatch.delenv("MBUPRIME_ASSET_ROOT", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(app_assets, "__file__", str(tmp_path / "app_assets.py"))
    assets = tmp_path / "mbuprime_structlab" / "assets"
    assets.mkdir(parents=True)
    icon = assets / app_assets.ICON_NAME
    icon.write_bytes(b"installed icon")
    assert app_assets.asset_path(app_assets.ICON_NAME).read_bytes() == b"installed icon"


def test_missing_installed_asset_does_not_fall_back_to_source(monkeypatch, tmp_path):
    monkeypatch.delenv("MBUPRIME_ASSET_ROOT", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(app_assets, "__file__", str(tmp_path / "app_assets.py"))
    (tmp_path / "mbuprime_structlab/assets").mkdir(parents=True)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / app_assets.ICON_NAME).write_bytes(b"checkout icon")
    with pytest.raises(FileNotFoundError):
        app_assets.asset_path(app_assets.ICON_NAME).read_bytes()


def test_source_assets(monkeypatch):
    monkeypatch.delenv("MBUPRIME_ASSET_ROOT", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert app_assets.asset_path(app_assets.ICON_NAME) == (
        Path(app_assets.__file__).resolve().parent / "assets" / app_assets.ICON_NAME)
    assert app_assets.asset_path(app_assets.ICON_NAME).is_file()


def test_explicit_and_frozen_asset_precedence(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("MBUPRIME_ASSET_ROOT", str(tmp_path / "override"))
    assert app_assets.asset_path("icon.ico") == tmp_path / "override/icon.ico"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    assert app_assets.asset_path("icon.ico") == tmp_path / "frozen/assets/icon.ico"
