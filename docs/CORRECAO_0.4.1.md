# Correção 0.4.1 — conexão não é cota

A versão anterior reutilizava `retry_at` para falhas de rede e respostas HTTP 429, mas sempre dizia que a fonte pediu uma pausa. Agora o motivo é preservado: rede, DNS, TLS, timeout, autenticação, bloqueio ou limitação HTTP. A espera local não é apresentada como solicitação da fonte.

A tela Meus limites distingue pedidos locais do saldo da conta do catálogo. Saldo não consultado não é renderizado como zero nem como limite atingido. Administradores e usuários recebem explicações em português.

Um diagnóstico faz um GET normal à origem configurada, sem login, sem redirecionamentos e sem baixar arquivos. O rastreamento registra somente etapas e categorias de erro, nunca URLs de pedidos, senhas, cookies, tokens ou conteúdos de resposta. Certificados, controles de origem e DNS público continuam validados.

Os 40 novos testes de regressão passaram localmente. O Dockerfile também executa a suíte publicada no repositório antes de criar a imagem de execução. A implantação deve ser confirmada no Railway. Testes artificiais não comprovam que o Z-Library está acessível.

Nenhum segredo foi alterado por esta correção. Ela não aumenta cotas, não modifica a origem configurada e não tenta contornar bloqueios.
