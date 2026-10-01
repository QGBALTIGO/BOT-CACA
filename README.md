# Livros Baltigo 0.5.0

Bot Telegram em português com fontes independentes, busca, seleção de edições, filtros, favoritos, fila e histórico. Instalação ou healthcheck aprovados não comprovam entrega de um arquivo.

## Uso

`/start` abre o menu. Envie título ou autor, escolha a obra e a edição e toque em Receber PDF/EPUB. `/fontes` escolhe Automático, Project Gutenberg ou Z-Library. `/filtros` guarda idioma e formato. `/testarpdf` solicita **Dom Casmurro** no fluxo normal de download e envio, usando o limite diário do usuário que enviou o comando. `/comprovante` mostra apenas a última entrega dessa conta que teve resposta válida do Telegram. Não há mensagem forçada ao reiniciar.

## Fontes e limites

**Project Gutenberg** usa os feeds oficiais OPDS e RDF, sem credenciais. EPUB é o arquivo da fonte; PDF é uma diagramação do texto integral UTF-8, conservando palavras, parágrafos, créditos e licença, com reorganização das quebras de linha da prosa. Não é fac-símile da edição impressa. Não há tradução, resumo ou geração de conteúdo literário. Caracteres não suportados interrompem a conversão e orientam a escolher EPUB. O catálogo não contém necessariamente os títulos de outros provedores.

**Z-Library** mantém a integração HTML/API anterior, a conta e a cota compartilhada. HTTP 513/403, CAPTCHA, autenticação, mudanças de formato e limites continuam sendo erros reais: esta versão não contorna bloqueios, não descobre espelhos, não troca IP e não amplia cotas. A falha da fonte não impede consultar o catálogo independente. A fonte efetivamente escolhida aparece nos resultados e é fixada na sessão de paginação.

O limite local `DAILY_LIMIT_PER_USER` vale para as duas fontes. Só o Z-Library depende da consulta de saldo da conta. Arquivos têm limite de 49 MB por padrão e são apagados ao final. Conteúdo HTML não é aceito como livro; EPUB precisa ter estrutura válida. A fila não repete envios de resultado incerto.

## Operação

Repositório `QGBALTIGO/BOT-CACA`, serviço Railway `bot-caca`, uma réplica e volume `/app/data`. Comando `python -m livros_baltigo`. `/health` valida o processo, polling e worker. `/ready` exige que ao menos um catálogo tenha passado na consulta; `partial` mostra explicitamente disponibilidade parcial, não funcionamento completo de todas as fontes.

Variáveis: `BOT_TOKEN`, `ADMIN_IDS`, `PUBLIC_ACCESS`, `DAILY_LIMIT_PER_USER`, `DATA_DIR`. `GUTENBERG_ENABLED=true` é o padrão. A fonte pública funciona sem credenciais de Z-Library. Credenciais parciais/inválidas dessa fonte continuam sendo erro de configuração. Não envie tokens, senhas ou sessões em mensagens, commits ou logs.

O banco SQLite é migrado preservando usuários, favoritos, livros e histórico. Envios passam por `queued`, `running`, `sending` e `done` apenas com comprovante válido. Interrupção durante `sending` torna o pedido `uncertain`; não há reenvio automático. Comprovantes registram ID da mensagem, arquivo, destino e SHA-256 local. Contagens antigas de conclusão são separadas das entregas com comprovante. O documento original não precisa ser armazenado após o envio.

## Verificação reproduzível

`python -m pytest -ra` executa a suíte local, com conexões externas bloqueadas. Os testes de fila e Telegram usam transportes simulados e não devem ser apresentados como entregas reais.

`python -m livros_baltigo.public_check --output public-verification` executa consultas reais limitadas ao catálogo público, pesquisa por título/autor, verifica paginação, baixa um EPUB e gera um PDF integral. **Não usa Telegram, BOT_TOKEN, credenciais ou conta de usuário.** O relatório registra hashes e tamanhos e declara `telegram_delivery_tested=false`. A comprovação final de entrega exige um pedido real no bot e o registro da resposta `sendDocument`.

Referências primárias: https://www.gutenberg.org/ebooks/offline_catalogs.html ; https://www.gutenberg.org/policy/robot_access.html ; https://core.telegram.org/bots/api#senddocument . O feed OPDS XML está previsto para ser descontinuado em 2027: revisar a integração antes disso. Links, conteúdo e disponibilidade de terceiros podem mudar.
