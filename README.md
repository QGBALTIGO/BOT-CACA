# Livros Baltigo — estado operacional

## Atendimento provisório

Esta branch contém um atendimento Telegram executável, em português, com preferências de idioma/formato persistidas por usuário e endpoints de saúde. **Não contém a integração completa do catálogo: buscas e downloads estão indisponíveis.** O atendimento avisa isso em todas as telas pertinentes e não simula resultados.

A publicação do módulo do catálogo pela ferramenta foi bloqueada. O código completo da versão 0.3.0 permanece no pacote entregue ao proprietário. Não confundir o atendimento provisório com a versão completa.

## Execução

`python -m livros_baltigo` inicia somente `livros_baltigo.onboarding`. Variáveis necessárias: `BOT_TOKEN`, `ADMIN_IDS`, `DATA_DIR` e `PORT`. Nenhuma credencial de livros é utilizada pelo atendimento provisório. Tokens e senhas não devem ser publicados neste repositório.

O Dockerfile configura o diretório `/app/data` e um usuário sem privilégios. Use uma única réplica e volume persistente em `/app/data`. Não remova um webhook existente sem concluir a migração da integração anterior.

`/health` informa a saúde do atendimento Telegram. `/ready` retorna 503 e `catalog_available=false` enquanto esta versão não tiver catálogo. Um deployment saudável não indica buscas ou downloads funcionando.

## Validação

312 testes locais do pacote completo foram repetidos e passaram; dois testes externos ficaram desativados. O atendimento provisório possui 39 testes locais adicionais. Nenhum desses resultados comprova login ou download real na fonte. A conexão Telegram e o deployment devem ser verificados pelos logs depois da instalação.
