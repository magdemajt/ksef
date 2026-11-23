from pathlib import Path

import pytest
import tomlkit

from app import configstore

BASE_CONFIG = """\
# komentarz naglowka - ma przetrwac zapis
[ksef]
environment = "test"   # inline comment
allow_prod_send = false

[optima]
server = "localhost,1433"
read_user = "ksef_reader"
write_user = "ksef_writer"

[companies.firma_a]
nip = "5265877635"
name = "Firma A"
database = "CDN_A"
"""


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    return path


class TestAddCompany:
    def test_adds_valid_company(self, cfg_file):
        configstore.add_company(
            "firma_b",
            {"nip": "526-104-01-58", "name": "Firma B", "database": "CDN_B"},
            path=cfg_file,
        )
        doc = tomlkit.parse(cfg_file.read_text())
        assert doc["companies"]["firma_b"]["name"] == "Firma B"
        assert doc["companies"]["firma_b"]["writes_enabled"] is False
        # komentarze zachowane
        assert "# komentarz naglowka" in cfg_file.read_text()

    def test_rejects_duplicate_id(self, cfg_file):
        with pytest.raises(configstore.ConfigStoreError, match="istnieje"):
            configstore.add_company(
                "firma_a", {"nip": "5261040158", "name": "X", "database": "Y"}, path=cfg_file
            )

    def test_rejects_bad_id(self, cfg_file):
        with pytest.raises(configstore.ConfigStoreError, match="Identyfikator"):
            configstore.add_company(
                "Firma B!", {"nip": "5261040158", "name": "X", "database": "Y"}, path=cfg_file
            )

    def test_rejects_bad_nip_and_leaves_file_untouched(self, cfg_file):
        before = cfg_file.read_text()
        with pytest.raises(configstore.ConfigStoreError, match="niepoprawna"):
            configstore.add_company(
                "firma_b", {"nip": "123", "name": "X", "database": "Y"}, path=cfg_file
            )
        assert cfg_file.read_text() == before

    def test_rejects_missing_required(self, cfg_file):
        with pytest.raises(configstore.ConfigStoreError, match="wymaganych"):
            configstore.add_company("firma_b", {"nip": "5261040158"}, path=cfg_file)


class TestUpdateGlobal:
    def test_updates_environment(self, cfg_file):
        configstore.update_global(
            ksef={"environment": "demo", "allow_prod_send": False},
            optima={"server": "10.0.0.5,1433"},
            path=cfg_file,
        )
        doc = tomlkit.parse(cfg_file.read_text())
        assert doc["ksef"]["environment"] == "demo"
        assert doc["optima"]["server"] == "10.0.0.5,1433"
        assert doc["optima"]["read_user"] == "ksef_reader"  # nieprzekazane bez zmian

    def test_rejects_invalid_environment(self, cfg_file):
        before = cfg_file.read_text()
        with pytest.raises(configstore.ConfigStoreError):
            configstore.update_global(
                ksef={"environment": "produkcja"}, optima={}, path=cfg_file
            )
        assert cfg_file.read_text() == before


class TestUpdateCompany:
    def test_updates_only_given_fields(self, cfg_file):
        configstore.update_company(
            "firma_a", {"purchase_register": "ZAKUPY_KRAJ"}, path=cfg_file
        )
        doc = tomlkit.parse(cfg_file.read_text())
        assert doc["companies"]["firma_a"]["purchase_register"] == "ZAKUPY_KRAJ"
        assert doc["companies"]["firma_a"]["name"] == "Firma A"

    def test_saves_offline_target_id(self, cfg_file):
        from app.config import load_config

        configstore.update_company("firma_a", {"offline_target_id": "KSIEG"}, path=cfg_file)
        assert load_config(cfg_file).companies["firma_a"].offline_target_id == "KSIEG"

    def test_rejects_unknown_company(self, cfg_file):
        with pytest.raises(configstore.ConfigStoreError, match="Nieznana"):
            configstore.update_company("firma_x", {"name": "X"}, path=cfg_file)


class TestClearingFields:
    def test_emptied_optional_field_is_removed_not_kept(self, cfg_file):
        configstore.update_company(
            "firma_a", {"regon": "123456785", "address_line2": "00-001 Warszawa",
                        "offline_target_id": "KSIEG"}, path=cfg_file,
        )
        configstore.update_company(
            "firma_a", {"regon": "", "address_line2": "", "offline_target_id": "NOWY",
                        "name": ""}, path=cfg_file,
        )
        company = tomlkit.parse(cfg_file.read_text())["companies"]["firma_a"]
        assert "regon" not in company and "address_line2" not in company
        assert company["offline_target_id"] == "NOWY"
        assert company["name"] == "Firma A"  # pole wymagane: puste znaczy "bez zmian"
