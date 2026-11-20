import os

import pytest


def test_accepts_supported_model(mm, comfy):
    url, directory, name, target = mm._validate(
        {"url": "http://127.0.0.1/files/a.safetensors", "directory": "vae", "name": "a.safetensors"}
    )
    assert target == os.path.join(comfy.models, "vae", "a.safetensors")


def test_nested_name_stays_inside_folder(mm, comfy):
    *_, target = mm._validate(
        {"url": "http://127.0.0.1/x.safetensors", "directory": "loras", "name": "sdxl/x.safetensors"}
    )
    assert target == os.path.join(comfy.models, "loras", "sdxl", "x.safetensors")


def test_legacy_folder_names_resolve(mm, comfy):
    *_, target = mm._validate({"url": "http://127.0.0.1/u.safetensors", "directory": "unet", "name": "u.safetensors"})
    assert target.startswith(str(comfy.models))


@pytest.mark.parametrize(
    ("model", "message"),
    [
        ({"url": "http://evil.example/a.safetensors", "directory": "vae", "name": "a.safetensors"}, "only https"),
        ({"url": "ftp://127.0.0.1/a.safetensors", "directory": "vae", "name": "a.safetensors"}, "only https"),
        ({"url": "http://127.0.0.1/a.exe", "directory": "vae", "name": "a.exe"}, "unsupported file type"),
        (
            {"url": "http://127.0.0.1/a.safetensors", "directory": "vae", "name": "../../a.safetensors"},
            "invalid file name",
        ),
        (
            {"url": "http://127.0.0.1/a.safetensors", "directory": "vae", "name": "/etc/a.safetensors"},
            "invalid file name",
        ),
        (
            {"url": "http://127.0.0.1/a.safetensors", "directory": "nope", "name": "a.safetensors"},
            "unknown model folder",
        ),
        (
            {"url": "http://127.0.0.1/a.safetensors", "directory": "custom_nodes", "name": "a.safetensors"},
            "not a model folder",
        ),
        ({"url": None, "directory": "vae", "name": "a.safetensors"}, "needs url"),
    ],
)
def test_rejects_unsafe_requests(mm, model, message):
    with pytest.raises(ValueError, match=message):
        mm._validate(model)


def test_production_allows_only_https_from_model_hosts(comfy):
    from conftest import load_extension

    mm = load_extension()
    ok = {
        "url": "https://huggingface.co/org/repo/resolve/main/a.safetensors",
        "directory": "vae",
        "name": "a.safetensors",
    }
    mm._validate(ok)
    mm._validate({**ok, "url": "https://civitai.com/api/download/models/1"})
    for url in (
        "http://huggingface.co/a.safetensors",
        "https://huggingface.co.evil.example/a.safetensors",
        "https://127.0.0.1/a.safetensors",
    ):
        with pytest.raises(ValueError, match="only https"):
            mm._validate({**ok, "url": url})


def test_tokens_are_sent_only_to_their_host(mm, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("CIVITAI_TOKEN", "civ_secret")
    assert mm._auth_headers("https://huggingface.co/x") == {"Authorization": "Bearer hf_secret"}
    assert mm._auth_headers("https://civitai.com/x") == {"Authorization": "Bearer civ_secret"}
    assert mm._auth_headers("http://127.0.0.1/x") == {}
