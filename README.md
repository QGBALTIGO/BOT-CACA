# Livros Baltigo 0.5.2

Bot Telegram em português com catálogos independentes, busca por título/autor, seleção de edições, filtros, favoritos, fila de arquivos e comprovantes de envio.

## Usar

Envie `/start` ou o nome de um livro. `/fontes` mostra os catálogos e seu estado de consulta. O botão **Buscar em outra fonte** repete o título e os filtros sem redigitação. Cada sessão mantém a fonte escolhida na paginação; favoritos e arquivos também mantêm sua origem.

`/testarpdf` solicita Dom Casmurro pelo fluxo normal da fila. `/comprovante` mostra a última resposta válida de entrega recebida do Telegram. Testes simulados, healthcheck e download no servidor não comprovam recebimento no chat.

## Catálogos integrados

| Fonte | Integração e disponibilidade de formatos |
| --- | --- |
| Project Gutenberg | OPDS/RDF oficiais; EPUB nativo e PDF diagramado do texto integral. O marcador de busca vazia é tratado como ausência de resultados, não como livro inválido. |
| Internet Archive | Busca/metadados públicos e arquivos de edições marcadas como domínio público. Não acessa empréstimos, arquivos privados ou EPUB criptografado. |
| Livros Abertos USP | Scraping das fichas e do catálogo OMP; arquivo integral nativo em PDF/EPUB quando anunciado, com licença verificada na ficha. |
| Editora UFPB | Scraping do catálogo OMP e links de PDF gratuito anunciados pela editora. Preserva direitos autorais; gratuito não significa domínio público. |
| InfoLivros | Consulta externa pelo índice de livros completos, com pesquisa local por título/autor e botão Abrir no site. O download pelo servidor foi recusado; não há envio de PDF nem consumo de quota nesta fonte. Índice em cache por 24 horas. |
| Z-Library | Consulta HTML pública separada do login EAPI. Download e quota continuam dependentes da conta. Bloqueios, CAPTCHA e redirecionamentos que não abrem o catálogo são falhas reais e não são contornados. |

Os catálogos não têm necessariamente os mesmos livros. Uma fonte indisponível não bloqueia as outras. No modo Automático, consultas independentes são feitas em grupos limitados, com prazos de resposta; a fonte usada aparece no resultado. Resultado vazio e falha de conexão são estados distintos.

## Configuração

`BOT_TOKEN`, `ADMIN_IDS`, `PUBLIC_ACCESS`, `DAILY_LIMIT_PER_USER`, `DATA_DIR` e `TIMEZONE` mantêm os significados anteriores.

`GUTENBERG_ENABLED=true` habilita o Gutenberg. `PUBLIC_BOOK_SOURCES=archive,usp,ufpb,infolivros` é o padrão para as fontes adicionais; uma lista vazia as desativa. Z-Library mantém suas variáveis de conta. Não coloque tokens, senhas ou sessões em commits, logs ou mensagens.

Uma réplica Railway, volume `/app/data` e comando `python -m livros_baltigo`. `/health` acompanha processo, polling e worker; `/ready` separa consulta completa, parcial e indisponível. A sincronização opcional dos menus e metadados do Telegram tem cache e respeita `Retry-After`, inclusive após reiniciar.

## Arquivos e dados

As fontes públicas não consultam a quota de outra biblioteca. O limite local diário continua valendo. PDFs e EPUBs são verificados antes do envio; páginas HTML de erro, arquivos truncados e formatos incompatíveis não são aceitos. Temporários são apagados ao encerrar o pedido. O pipeline só registra entrega concluída após validar mensagem, chat e documento na resposta do Telegram; envios incertos não são repetidos automaticamente.

O PDF Gutenberg não é um fac-símile: diagrama o texto integral sem tradução ou resumo, conservando os créditos e a licença. Archive, USP e UFPB fornecem arquivos nativos, sem alteração do conteúdo. InfoLivros é somente consulta externa.

## Verificações

`python -m pytest -ra` executa regressões locais com conexões externas bloqueadas. Os transportes Telegram nesses testes são simulados.

`python -m livros_baltigo.multisource_check --output public-verification` testa consultas positivas e vazias, PDFs reais de quatro fontes e o acesso por página externa do InfoLivros. O relatório distingue `passed` para fontes com PDF validado de `search_only` para a consulta externa. Não usa token Telegram nem credenciais de bibliotecas. Registra tamanho, SHA-256, título e origem. Os relatórios em `docs/PUBLIC_VERIFICATION_*.json` distinguem essas etapas da entrega no chat.

A revisão dos endereços enviados pelo usuário está em `docs/SOURCES_FROM_LIST.md`. Não foi assumido que abrir a página inicial equivale a disponibilizar download.
