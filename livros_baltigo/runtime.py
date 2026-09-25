"""Prepara o volume Docker e abandona privilégios antes de executar o bot."""
import os
from pathlib import Path


def prepare_data(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.getenv("DROP_PRIVILEGES") == "true" and hasattr(os, "getuid") and os.getuid() == 0:
        # A imagem cria previamente o usuário/grupo 10001. Não percorre outros diretórios.
        os.chown(path, 10001, 10001)
        os.chmod(path, 0o700)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
