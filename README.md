# Livros Baltigo · BOT-CACA

**Estado: publicação do código incompleta. Este repositório ainda não contém todos os arquivos necessários para executar o bot. Não há deployment em produção.**

A versão 0.2.0 foi preparada no pacote entregue na conversa, com **147 testes automatizados aprovados localmente**. O envio dos arquivos de implementação foi bloqueado pela ferramenta. Não trate este repositório como uma versão executável até concluir a publicação do pacote completo.

## O que mudou no pacote 0.2.0

Menu, botões e descrições em português; uso sem decorar comandos; idiomas dos livros localizados; progresso visual; painel e teste de conexão exclusivos do administrador. Acesso multiusuário por conversas privadas, com filtros, favoritos, histórico e paginação isolados. Uma única conta da fonte mantém sua cota compartilhada, sem aumentar limites.

## Railway

Projeto **Livros Baltigo**, ambiente **production**, serviço **bot-caca**. O token e as configurações foram salvos nas variáveis privadas do serviço. `PUBLIC_ACCESS=true`, `SETUP_MODE=true` e limite local inicial de 3 pedidos por pessoa por dia. O token não foi publicado neste repositório.

Ainda faltam: publicar o código completo, configurar fonte GitHub e volume persistente, fornecer domínio e credenciais da conta Z-Library nas variáveis privadas e validar o deployment. O cadastro/conexão do bot pessoal no site não transfere essas credenciais ao Railway.

## Validação e limitações

Os 147 testes usam usuários artificiais, respostas controladas e HTTP local. Incluem dois usuários com dados isolados, rejeição de acesso ao painel administrativo, último download compartilhado e 20 usuários disputando 10 downloads sem exceder a cota simulada. Não houve teste com duas pessoas reais nem download real.

A consulta ao Telegram a partir do ambiente de desenvolvimento falhou na conexão; o token não foi validado. A integração com o Z-Library ainda não foi testada por ausência de credenciais. Arquivos de implantação e código completo estão no pacote 0.2.0 entregue na conversa.

Caso o mesmo token tenha sido entregue à integração pessoal do Z-Library, desvincule o bot daquele serviço, revogue a credencial pelo BotFather e substitua `BOT_TOKEN` no Railway antes da migração. O programa não remove webhooks existentes automaticamente.

Nunca publique senhas, tokens, arquivos de sessão ou dados dos usuários. Utilize somente contas, acessos e obras para os quais tenha autorização.
