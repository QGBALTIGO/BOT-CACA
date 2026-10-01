# Revisão da lista enviada — 1 de outubro de 2026

Base: arquivo `Markdown colado(1).md`, fornecido pelo usuário. A lista contém bibliotecas, indexadores, quadrinhos, audiolivros, ferramentas e fontes tipográficas. Foram priorizados os catálogos de livros em PDF/EPUB; as outras categorias não foram apresentadas como fontes de livros do bot.

## Integrados a partir da lista

Project Gutenberg e Internet Archive: pesquisa e download de PDF foram verificados com os adaptadores reais. InfoLivros: a busca pelo índice público foi validada, mas o download de PDF foi recusado no teste real (execução 36882386478). Foi habilitado somente como consulta externa, com botão Abrir no site, sem envio de arquivo pelo bot e sem consumo da quota local. Não foi contado como quinta fonte de PDF.

Z-Library: integração existente mantida; a consulta HTML não exige que o login EAPI funcione antes. Isso não remove a proteção do site nem confirma downloads da conta. Na verificação Railway da versão 0.5.1, o catálogo permaneceu indisponível por redirecionamento circular.

## Avaliados, mas não habilitados como downloads

- Open Library: a API de busca respondeu; é descoberta/metadados e pode apontar empréstimos. Não foi contada como uma nova fonte de PDF apenas por ser pesquisável.
- eLivros: início e busca responderam; a consulta de Dom Casmurro também trouxe muitos títulos não correspondentes. O download não foi validado.
- dLivros: início e busca responderam e incluíram Dom Casmurro. O download de arquivo não foi validado.
- PDF Room: início e busca responderam. O arquivo final não foi validado.
- Datassette: página inicial respondeu. Ainda não há adaptador validado de pesquisa e download neste bot.
- Library Genesis: o domínio fornecido não pôde ser acessado no ambiente de teste. Isso não prova indisponibilidade mundial.
- Baixe Livros: o teste anterior de catálogo retornou HTTP 403 no ambiente de verificação. Não foram usados proxies para contornar a recusa.

Os demais endereços do arquivo não foram certificados nem habilitados automaticamente. Nenhuma conclusão sobre segurança ou direito de acesso decorre apenas de constarem na lista. Uma integração só deve ser apresentada como disponível quando a sua própria consulta e seu próprio arquivo forem verificáveis.

USP e UFPB são fontes adicionais pesquisadas fora da lista, não sugestões atribuídas ao arquivo fornecido.
