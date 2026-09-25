# Livros Baltigo · BOT-CACA

**Estado: publicação incompleta. Este repositório ainda não contém todos os arquivos necessários para executar o bot. Não há deployment em produção.**

A versão 0.1.1 foi preparada e testada localmente, com 111 testes automatizados aprovados. Parte dos arquivos foi enviada; a integração bloqueou outros envios. Não trate este repositório como uma versão executável até a conclusão da publicação.

## Railway

O projeto **Livros Baltigo**, ambiente **production**, e o serviço **bot-caca** foram criados. Não há imagem implantada nem volume persistente configurado. A tentativa de definir o token pela integração foi bloqueada; ele não está no repositório.

## Implementação preparada

Busca, filtros, favoritos, histórico, fila de arquivos, cotas por usuário, acesso privado e diagnóstico. O modo de configuração informa explicitamente quando a conta da fonte não está conectada; não simula resultados nem downloads.

## Limites da validação

Os testes locais utilizam serviços simulados, arquivos artificiais e servidor HTTP local. A conta Z-Library não foi conectada por ausência de credenciais. O token do Telegram não foi validado: a tentativa de conexão a partir do ambiente de desenvolvimento falhou.

Credenciais e arquivos de sessão nunca devem ser publicados no GitHub. Utilize somente obras e acessos para os quais tenha autorização.
