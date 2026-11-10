"""EtherCAT Setup's own settings, kept between runs:
~/.config/ecatsetup/settings.yaml (or $ECATSETUP_SETTINGS). For now the
folders of ESI files the user has given."""

import os

import yaml


def path():
    return os.environ.get("ECATSETUP_SETTINGS") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "ecatsetup", "settings.yaml")


def load():
    try:
        with open(path()) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def save(data):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)
    os.replace(tmp, p)


def esi_folders():
    """The ESI folders given before that still exist."""
    return [d for d in load().get("esi-folders") or [] if isinstance(d, str) and os.path.isdir(d)]


def add_esi_folder(folder):
    data = load()
    folders = [d for d in data.get("esi-folders") or [] if d != folder]
    data["esi-folders"] = folders + [folder]
    save(data)
