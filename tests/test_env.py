"""Environment loading checks for server-side API keys."""

import os

from backend.env import load_project_env


def test_load_project_env_reads_dotenv_without_overriding_existing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPGRAM_API_KEY=fake-from-file\n"
        "ROUTER_MODE=llm\n"
        "LAB_DB_PATH=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("LAB_DB_PATH", raising=False)
    monkeypatch.setenv("ROUTER_MODE", "deterministic")

    load_project_env(env_file)

    assert "fake-from-file" == os.environ["DEEPGRAM_API_KEY"]
    assert "deterministic" == os.environ["ROUTER_MODE"]
    assert "LAB_DB_PATH" not in os.environ
