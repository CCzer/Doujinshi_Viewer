import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

DEFAULT_PATHS = {"pdf_dir": "books/pdf", "images_dir": "books/images", "zip_dir": "books/zip"}
FIELDS = ("pdf_dir", "images_dir", "zip_dir", "jm_username", "jm_password", "admin_password_hash")


def validate(values, base):
    if not isinstance(values, dict):
        raise ValueError("配置必须是 JSON 对象")
    config = {key: values.get(key, "") for key in FIELDS}
    if any(not isinstance(value, str) for value in config.values()):
        raise ValueError("配置字段必须为字符串")
    for field in ("pdf_dir", "images_dir", "zip_dir"):
        config[field] = config[field].strip()
        if config[field] and not (base / Path(config[field]).expanduser()).is_dir():
            raise ValueError("配置的文件夹不存在：" + field)
    config["jm_username"] = config["jm_username"].strip()
    if bool(config["jm_username"]) != bool(config["jm_password"]):
        raise ValueError("JM 账号和密码必须同时填写")
    return config


def load_config(path, base):
    if path.exists():
        return validate(json.loads(path.read_text(encoding="utf-8")), base)
    return validate(default_paths(base), base)


def default_paths(base):
    return {key: value for key, value in DEFAULT_PATHS.items() if (base / value).is_dir()}


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            json.dump(config, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.close()
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def roots(config, base):
    prefixes = {"pdf_dir": "", "images_dir": "images:", "zip_dir": "zips:"}
    return {
        prefix + str(root): root
        for field, prefix in prefixes.items()
        if config[field]
        for root in [(base / Path(config[field]).expanduser()).resolve()]
    }
