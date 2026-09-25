# Livros Baltigo 0.4.0

Bot de livros em português para Telegram. Esta versão substitui o atendimento provisório por um programa com catálogo, pesquisa, edições, favoritos, filtros e fila de arquivos. O sucesso da instalação não comprova a disponibilidade da fonte externa.

## Integração

`python -m livros_baltigo` inicia o aplicativo completo. `BOOK_SOURCE_MODE=html` (padrão) pesquisa e lê detalhes no HTML da origem configurada. Login, consulta da cota e solicitação do arquivo da edição usam a EAPI não oficial. `BOOK_SOURCE_MODE=api` é uma escolha explícita alternativa; não há fallback que contorne bloqueios.

As páginas do site são paginadas localmente, sem descartar edições após os primeiros oito resultados. O programa reconhece cartões `z-bookcard` e alguns cartões legados documentados pela comunidade. Layouts desconhecidos, CAPTCHA, 403, 429 e falhas de autenticação são informados, não tratados como uma busca vazia.

## Operação

Fonte GitHub: `QGBALTIGO/BOT-CACA`, branch `main`. Serviço Railway existente: `bot-caca`. Uma réplica, volume persistente `/app/data`, comando `python -m livros_baltigo`, healthcheck `/health`.

Variáveis privadas: `BOT_TOKEN`, `ADMIN_IDS`, `ZLIB_BASE_URL`, `ZLIB_EMAIL`, `ZLIB_PASSWORD`. Sessão `ZLIB_USER_ID` + `ZLIB_USER_KEY` é alternativa ao login. Não publique segredos. O domínio é o fornecido pelo proprietário, sem descoberta de espelhos.

`PUBLIC_ACCESS=true` atende usuários em conversas privadas. Todos compartilham a cota da conta da fonte. `DAILY_LIMIT_PER_USER` apenas restringe o consumo local. Nenhuma cota é ampliada. Hosts externos de arquivo precisam de autorização explícita em `ZLIB_FILE_HOSTS`.

No início há um diagnóstico de perfil e busca, sem solicitar arquivo. `/health` mede o processo e o Telegram; `/ready` só responde positivamente após consulta real ao catálogo. Um teste de consulta bem-sucedido não valida downloads. O administrador pode repetir o diagnóstico pelo botão Testar conexão.

## Validação

A suíte completa local desta entrega aprovou 333 casos, com dois testes externos desativados. Não confundir arquivos artificiais e HTTP de loopback com testes reais da conta. Os novos testes de regressão do scraper são publicados em `tests/test_scraper_production.py`.

Antes desta instalação, o diagnóstico executado no Railway falhou ao conectar à origem, antes do login. A validação da versão completa deve ser acompanhada nos logs (`catalog_probe`); nenhuma credencial ou resposta privada é impressa.

Pesquisa e detalhes podem ficar indisponíveis quando a fonte não responde. Favoritos e filtros permanecem locais e não dependem dessa conexão. Utilize somente acessos e conteúdos autorizados. Não há bypass de DRM, CAPTCHA, bloqueio ou cota.
