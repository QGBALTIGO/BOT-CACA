class UserError(Exception):
    """Mensagem segura para o usuário; nunca inclua resposta HTTP ou credenciais."""

    def __init__(self, message: str, code: str = "operation_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


class TelegramError(Exception):
    def __init__(self, code: int, description: str = "", retry_after: int = 0):
        # A descrição é retida somente em memória; não deve ser registrada em logs.
        super().__init__(f"Telegram HTTP/API {code}")
        self.code = code
        self.description = description
        self.retry_after = retry_after
