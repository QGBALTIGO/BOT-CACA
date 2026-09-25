"""Atendimento ativo com diagnóstico sanitizado da fonte ao iniciar."""
import asyncio
import json
from .source_check import inspect_source
from .onboarding import main

if __name__ == "__main__":
    print(json.dumps(asyncio.run(inspect_source()), ensure_ascii=False), flush=True)
    main()
